import pytest

from hwbp.design import Design, Part
from hwbp.models import ElectricalConnection
from hwbp.power import design_power, group_peak_ma, pick_rail_voltage
from hwbp.requirements import Requirements, extract_requirements
from hwbp.selection import (
    Need,
    effective_logic_v,
    keyword_hits,
    logic_compatible,
    plan_needs,
    score_candidates,
    select_components,
    suggest,
    weights_for,
)
from hwbp.wiring import (
    PinAllocationError,
    aggregate_edges,
    allocate_pins,
    build_data_connections,
    infer_rails,
    validate,
)


def _design(prompt, catalog):
    d = Design(extract_requirements(prompt))
    select_components(d, catalog)
    build_data_connections(d)
    design_power(d, catalog)
    validate(d)
    return d


def test_requirements_extraction():
    r = extract_requirements("Build a 4-axis robotic arm with a joystick, under $60")
    assert r.capabilities == ["actuate.servo", "input.joystick"] and r.counts["actuate.servo"] == 4
    assert r.budget_usd == 60 and r.power_source == "wall"
    r = extract_requirements(
        "Solar powered garden soil moisture monitor that waters plants, runs 2 weeks, titanium, cheap"
    )
    assert r.power_source == "solar" and "actuate.pump" in r.capabilities and r.runtime_hours == 336
    assert r.materials == ["titanium"] and "cost" in r.priorities
    assert extract_requirements("weather station with pressure sensor").counts == {}  # 'pressure' is not a button
    assert len(r.design_hash) == 16


def test_named_technology_is_a_hard_preference(catalog):
    d = _design("A portable tactical radar with a touchscreen display", catalog)
    radar = next(p for p in d.parts if p.role == "sense.presence")
    assert radar.catalog.id == "hlk-ld2410c"
    disp = next(p for p in d.parts if p.role == "ui.display")
    assert "display.touch" in disp.catalog.capabilities


def test_scoring_weights_and_reasons(catalog):
    req = Requirements(prompt="x", priorities=["cost", "battery_life"], size_class="tiny", power_source="solar")
    w = weights_for(req)
    assert sum(w.values()) == pytest.approx(1.0) and w["power"] > w["popularity"]
    ranked = score_candidates(
        catalog.with_capability("ui.display"), Need("ui.display", prefer={"low_power", "kw:epaper"}), req
    )
    assert ranked[0][1] >= ranked[-1][1] and any("$" in r for r in ranked[0][2])
    assert score_candidates([], None, req) == []
    assert keyword_hits(catalog.get("hlk-ld2410c"), {"radar", "banana"}) == {"radar"}


def test_logic_level_rules(catalog):
    s3 = catalog.get("esp32-s3-devkitc-1")
    nano = catalog.get("arduino-nano-every")
    assert not logic_compatible(catalog.get("hc-sr04"), s3)  # 5 V echo into 3.3 V pins
    assert logic_compatible(catalog.get("hc-sr04"), nano)
    assert effective_logic_v(catalog.get("rfm95w-lora")) == 3.3  # inferred from a 3.7 V max supply
    assert not logic_compatible(catalog.get("rfm95w-lora"), nano)
    assert logic_compatible(catalog.get("bme280"), None)


def test_plan_needs_display_constraints():
    needs = {n.capability: n for n in plan_needs(extract_requirements("tiny wearable with touch screen and e-paper"))}
    assert needs["ui.display"].require == {"display.touch", "display.epaper"}
    forced = plan_needs(
        Requirements(prompt="x", capabilities=["ui.display", "display.touch"], force={"ui.display": "epaper-29"})
    )
    assert forced[0].require == set()


def test_pin_allocation_respects_capabilities_and_reserved(catalog):
    d = _design(
        "Weather station with temperature, humidity, pressure, CO2, e-paper display and LoRa, solar powered", catalog
    )
    mcu = d.mcu.catalog.mcu
    reserved = set(mcu.reserved)
    caps = {p.name: set(p.caps) for p in mcu.pins}
    used = {}
    for pid, mapping in d.pin_map.items():
        for _pin, mcu_pin in mapping.items():
            assert mcu_pin not in reserved
            used.setdefault(mcu_pin, set()).add(pid)
    shared = {pin for b in d.buses if b["protocol"] in ("i2c", "spi") for pin in b["pins"].values()}
    assert all(len(owners) == 1 for pin, owners in used.items() if pin not in shared)
    assert not [i for i in d.issues if i.severity == "error"]
    for itf_part in d.parts:
        for itf in itf_part.catalog.interfaces:
            if itf.protocol == "adc":
                for pin in itf.pins:
                    assert "adc" in caps[d.pin_map[itf_part.id][pin]]


def test_pin_allocation_failure(catalog):
    c3 = catalog.get("esp32-c3-supermini")
    parts = [Part(id=f"b{i}", catalog=catalog.get("push-button"), role="input.button", name="b") for i in range(20)]
    with pytest.raises(PinAllocationError):
        allocate_pins(c3, parts)
    with pytest.raises(PinAllocationError):
        allocate_pins(catalog.get("bme280"), parts)
    uart_parts = [Part(id=f"g{i}", catalog=catalog.get("neo-6m"), role="sense.gps", name="g") for i in range(3)]
    with pytest.raises(PinAllocationError, match="UART"):
        allocate_pins(c3, uart_parts)


def test_uart_crossover_and_edge_labels(catalog):
    d = _design("A portable tactical radar with a touchscreen display", catalog)
    tx = next(c for c in d.connections if c.type == "data" and c.source_pin == "TX")
    bus = next(b for b in d.buses if b["protocol"] == "uart")
    assert tx.target_pin == bus["pins"]["rx"]
    labels = {(e["type"], e["source"], e["target"]): e["label"] for e in aggregate_edges(d.connections)}
    assert labels[("power", "battery", "charger")] == "3.7V"
    assert any("spi ×" in v for v in labels.values())
    assert any(e["type"] == "ground" for e in aggregate_edges(d.connections, include_ground=True))


def test_level_shifters_inserted_in_signal_path(catalog):
    d = _design("4WD obstacle-avoiding robot car with ultrasonic sensors front and back and LED lights", catalog)
    shifters = [p.id for p in d.parts if p.role == "logic.level_shift"]
    assert len(shifters) == 2  # 5 lines -> two 4-channel shifters
    mcu = d.mcu.id
    for c in d.connections:
        if c.type == "data" and mcu in (c.source, c.target):
            other = c.target if c.source == mcu else c.source
            assert other not in ("distance_sensor_1", "distance_sensor_2", "led_strip")
    hv = [c for c in d.connections if c.type == "power" and c.target in shifters and c.target_pin == "HV"]
    assert len(hv) == 2


def test_power_architectures(catalog):
    radar = _design("A portable tactical radar with a touchscreen display", catalog)
    assert {p.catalog.id for p in radar.parts} >= {"tp4056-usbc-protected", "mt3608-boost"}
    assert radar.power_summary["runtimeHours"] >= 4.0
    rails = {r.name: r for r in radar.rails}
    assert rails["3.3V"].source_id == "main_controller" and rails["5V"].typ_ma > rails["3.3V"].typ_ma
    dog = _design("Robot dog quadruped with 12 servos, IMU and camera, battery powered", catalog)
    assert dog.part("battery").catalog.battery.cells == 2
    assert any(p.catalog.id == "ubec-5v-5a" for p in dog.parts)
    assert any(i.code == "external_charger" for i in dog.issues)
    garden = _design("Solar powered garden soil moisture monitor that waters plants and sends data over wifi", catalog)
    assert garden.power_summary["solar"]["energyNeutral"] is True
    car = _design("4WD obstacle-avoiding robot car with ultrasonic sensors front and back and LED lights", catalog)
    assert any(i.code == "rail_overcurrent" and i.severity == "error" for i in car.issues)  # real limit, reported
    lamp = _design("Smart desk lamp with LED strip, rotary knob, light sensor and wifi, USB powered", catalog)
    assert lamp.power_summary["source"] == "wall" and "battery" not in lamp.power_summary


def test_rail_selection_rules(catalog):
    motor = Part(id="m", catalog=catalog.get("n20-gear-motor"), role="actuate.drive", name="m")
    assert pick_rail_voltage(motor, 3.3, 3.7, None, (3.0, 4.2)) == (3.7, "M+")
    assert pick_rail_voltage(motor, 3.3, 7.4, None, (6.0, 8.4)) == (5.0, "M+")  # 8.4 V > 6 V max
    lora = Part(id="l", catalog=catalog.get("rfm95w-lora"), role="comm.lora", name="l")
    assert (
        pick_rail_voltage(lora, 5.0, 3.7, None, (3.0, 4.2)) is None
        or pick_rail_voltage(lora, 5.0, 3.7, None, (3.0, 4.2))[0] != 3.7
    )
    servos = [Part(id=f"s{i}", catalog=catalog.get("sg90-servo"), role="actuate.servo", name="s") for i in range(6)]
    assert group_peak_ma(servos) == pytest.approx(6 * 650 * 0.5)


def test_validate_flags_bad_supply_and_conflicts(catalog):
    d = Design(Requirements(prompt="x"))
    mcu = d.add(Part(id="mcu", catalog=catalog.get("esp32-s3-devkitc-1"), role="mcu", name="MCU"))
    st = d.add(Part(id="disp", catalog=catalog.get("st7789-154"), role="ui.display", name="Disp"))
    d.add(Part(id="motor", catalog=catalog.get("tt-gear-motor"), role="actuate.drive", name="Motor"))
    d.add(Part(id="o1", catalog=catalog.get("ssd1306-096"), role="ui.display", name="O1"))
    d.add(Part(id="o2", catalog=catalog.get("ssd1306-096"), role="ui.display", name="O2"))
    d.add(Part(id="o3", catalog=catalog.get("ssd1306-096"), role="ui.display", name="O3"))
    d.connections = [ElectricalConnection(type="power", source="mcu", target="disp", voltage="5V", target_pin="VCC")]
    d.pin_map = {
        "o1": {"SDA": "GPIO8"},
        "o2": {"SDA": "GPIO8"},
        "o3": {"SDA": "GPIO8"},
        "disp": {"CS": "GPIO4"},
        "x": {"A": "GPIO4"},
        "y": {"B": "GPIO0"},
    }
    d.buses = [{"protocol": "i2c", "pins": {"sda": "GPIO8", "scl": "GPIO9"}, "devices": ["o1", "o2", "o3"]}]
    validate(d)
    codes = {i.code for i in d.issues}
    assert {
        "supply_voltage",
        "pin_conflict",
        "missing_driver",
        "i2c_address_changed",
        "i2c_address_conflict",
        "reserved_pin",
        "orphan",
    } <= codes
    assert st and mcu


def test_suggestions_offer_alternatives_and_additions(catalog):
    d = _design("A portable tactical radar with a touchscreen display", catalog)
    sugg = suggest(d, catalog)
    assert any(s.kind == "alternative" and s.price_delta_usd is not None for s in sugg)
    assert any(s.kind == "addition" and s.catalog_id == "slide-switch" for s in sugg)
    only = suggest(d, catalog, node_id="display")
    assert only and all(s.node_id == "display" for s in only)


def test_budget_downgrade(catalog):
    d = _design("robotic arm with 4 high torque servos and touchscreen, budget of $20", catalog)
    assert any("downgraded" in r for p in d.parts for r in p.reasons) or any(
        i.code == "budget_exceeded" for i in d.issues
    )


def test_infer_rails_for_foreign_documents():
    nodes = [{"id": "b", "pins": ["B+", "B-"]}, {"id": "reg", "pins": ["VIN", "VOUT", "GND"]},
             {"id": "mcu", "pins": ["5V", "GND"]}, {"id": "s", "pins": ["3V3", "GND"]}, {"id": "t", "pins": ["VCC", "GND"]}]  # fmt: skip
    conns = [{"type": "power", "source": "b", "target": "reg", "voltage": "3.7V"},
             {"type": "power", "source": "reg", "target": "mcu", "voltage": "5V"},
             {"type": "ground", "source": "b", "target": "mcu"}]  # fmt: skip
    out = infer_rails(nodes, conns)
    grounds = {c.target for c in out if c.type == "ground"}
    assert grounds == {"reg", "s", "t"}
    supplies = {c.target: c for c in out if c.type == "power"}
    assert supplies["t"].source == "reg" and supplies["t"].voltage == "5V"
    assert supplies["s"].voltage is None or supplies["s"].voltage.startswith("3.3") or supplies["s"].source == "reg"
    assert infer_rails([], []) == []
