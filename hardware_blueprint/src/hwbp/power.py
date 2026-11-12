"""Power architecture synthesis: rail planning, source/battery/regulator selection, current budget.

Algorithm
---------
1. **Rail assignment** — every load picks a rail voltage from its acceptable input ranges with preference
   MCU-logic voltage (3.3 V keeps I2C pull-ups at logic level) > 5 V > VBAT > 12 V. Drives use their
   ``drive`` range; motors run from VBAT when the battery voltage fits, servos from a 5 V high-current rail.
2. **Source** — wall: USB-C 5 V adapter, or 12 V adapter + 5 V buck when a load needs > 5.5 V.
   battery/solar: choose a battery from an energy budget:
       P_avg = Σ_rails V·I_typ / η ;  E = P_avg · runtime / 0.85 (usable)  ->  capacity_mAh = E / V_nom
   hard constraints: battery max discharge ≥ peak current; 2S when loads need > 5.5 V or the 5 V peak
   is > 3 A. Score: capacity adequacy, size, price.
3. **Regulators** — topology by Vin range vs Vout: Vin_min > Vout+dropout -> buck (LDO if I < 300 mA
   and Vin_max−Vout < 1.5 V), Vin_max < Vout -> boost, straddling -> buck-boost; sized by peak current.
   The MCU dev-board's on-board 3V3 LDO is reused for small 3.3 V loads (300 mA budget).
4. **Current propagation** (bottom-up): switcher I_in = V_out·I_out / (η·V_in); LDO I_in = I_out + I_q.
5. **Estimates** — runtime = capacity·V_nom·0.85 / P_avg; solar energy balance per day.
"""

from __future__ import annotations

from hwbp.catalog import Catalog
from hwbp.design import Design, Part, Rail
from hwbp.models import CatalogPart, ElectricalConnection
from hwbp.selection import Need, score_candidates
from hwbp.wiring import ground_connections

MCU_3V3_BUDGET_MA = 300.0
# Fraction of time an electromechanical load is energised (for energy, not for peak sizing).
DUTY = {"pump": 0.02, "servo": 0.15, "dc_motor": 0.5, "stepper": 0.3, "relay": 0.3, "solenoid": 0.05}
MOTOR_SUPPLY_PINS = ("VM", "VMOT", "12V", "VIN", "V+")
USABLE_FRACTION = 0.85
PEAK_SUN_HOURS = 4.0


def _ma(part: Part, key: str) -> float:
    """Current in mA: key = typ (while active) | peak (stall/transmit) | avg (duty-cycled, for energy)."""
    cat = part.catalog
    if key == "avg":
        return _avg_ma(part)
    if key == "peak" and cat.drive and cat.drive.kind == "dc_motor" and _LOW_POWER_CTX.get("diversity", True):
        # Diversity: firmware soft-starts motors one at a time, so at most one motor is at stall while
        # the others run. The motor with the highest stall is accounted at stall by ``_rail_peak``.
        return cat.drive.run_ma
    if cat.drive:
        return cat.drive.run_ma if key == "typ" else cat.drive.stall_ma
    c = cat.current_ma or {}
    return float(c.get(key, c.get("typ", 0.0))) * part.quantity


_LOW_POWER_CTX: dict[str, bool] = {"on": False}


def _avg_ma(part: Part) -> float:
    cat = part.catalog
    if cat.drive:
        return cat.drive.run_ma * DUTY.get(cat.drive.kind, 0.5)
    c = cat.current_ma or {}
    typ = float(c.get("typ", 0.0))
    sleep = c.get("sleep")
    if _LOW_POWER_CTX["on"] and sleep is not None and cat.type in ("mcu", "communication", "sensor"):
        return (0.15 * typ + 0.85 * float(sleep)) * part.quantity
    if cat.type == "lighting":
        return 0.5 * typ * part.quantity
    return typ * part.quantity


def _motor_pin(driver: Part) -> str:
    pins = [pi.pin for pi in driver.catalog.power_inputs]
    return next((p for p in MOTOR_SUPPLY_PINS if p in pins), "VM")


def _ranges(part: Part) -> list[tuple[str, float, float]]:
    cat = part.catalog
    if cat.drive and cat.drive.kind in ("dc_motor", "pump"):
        return [("M+", cat.drive.vmin, cat.drive.vmax)]
    if cat.drive and cat.drive.kind == "servo":
        return [(cat.power_inputs[0].pin if cat.power_inputs else "V+", cat.drive.vmin, cat.drive.vmax)]
    return [(pi.pin, pi.vmin, pi.vmax) for pi in cat.power_inputs]


def is_load(part: Part) -> bool:
    cat = part.catalog
    if cat.category != "electrical" or cat.type in ("battery",) or cat.power_output or part.drives:
        return False  # drivers are supplied through their loads (motor pin) + logic-rail loop
    return bool(cat.power_inputs or cat.drive or cat.mcu)


def pick_rail_voltage(part: Part, logic_v: float, vbat: float | None, high_v: float | None,
                      vbat_range: tuple[float, float] | None = None) -> tuple[float, str] | None:  # fmt: skip
    """Return (rail voltage, supply pin) for a load; None if no standard rail fits.

    A raw battery rail is only allowed when the battery's FULL range (empty..charged) fits the load.
    """
    ranges = _ranges(part)
    vb_lo, vb_hi = vbat_range or ((vbat, vbat) if vbat else (0.0, 0.0))

    def fits_vbat(lo: float, hi: float) -> bool:
        return vbat is not None and lo - 0.05 <= vb_lo and vb_hi <= hi + 0.05

    drive = part.catalog.drive
    if drive and drive.kind in ("dc_motor", "pump") and vbat:
        # motors only need the UPPER bound respected (a flat battery just slows them down)
        if vb_hi <= ranges[0][2] + 0.05:
            return vbat, ranges[0][0]
    if drive:  # actuators never run from the MCU's logic rail
        prefs = [5.0, 6.0] + ([high_v] if high_v else []) + [12.0]
        for v in prefs:
            for pin, lo, hi in ranges:
                if lo - 0.05 <= v <= hi + 0.05:
                    return v, pin
        return None
    prefs = [logic_v, 5.0] + ([vbat] if vbat else []) + ([high_v] if high_v else []) + [12.0]
    if part.catalog.type == "mcu":
        prefs = [5.0, 3.3] + prefs
    for v in prefs:
        for pin, lo, hi in ranges:
            if vbat is not None and abs(v - vbat) < 1e-9:
                if fits_vbat(lo, hi):
                    return v, pin
            elif lo - 0.05 <= v <= hi + 0.05:
                return v, pin
    return None


def rail_name(v: float, vbat: float | None) -> str:
    if vbat is not None and abs(v - vbat) < 1e-6:
        return "VBAT"
    return f"{v:g}V"


# ------------------------------------------------------------------------------ main entry
def design_power(design: Design, catalog: Catalog) -> Design:
    req = design.requirements
    _LOW_POWER_CTX["on"] = "battery_life" in req.priorities or req.power_source == "solar" or req.size_class == "tiny"
    mcu = design.mcu
    logic_v = mcu.catalog.mcu.logic_v if mcu and mcu.catalog.mcu else 3.3
    loads = [p for p in design.parts if is_load(p)]
    needs_high_v = any(min(lo for _, lo, _ in _ranges(p) or [("", 0, 0)]) > 5.6 for p in loads)
    servo_list = [p for p in loads if p.catalog.drive and p.catalog.drive.kind == "servo"]
    servo_peak = sum(_ma(p, "peak") for p in servo_list) * (0.5 if len(servo_list) > 4 else 1.0)

    # ---- source / battery
    battery = charger = panel = source = protection = None
    vbat = None
    if req.power_source == "wall":
        adapter_id = "dc-12v2a-adapter" if needs_high_v else "usbc-5v3a-adapter"
        source = design.add(Part(id="power_adapter", catalog=catalog.get(adapter_id), role="power.source", name="Wall Adapter",
                                 reasons=["wall-powered design" + (", 12 V for high-voltage loads" if needs_high_v else "")]))  # fmt: skip
        primary_v = source.catalog.power_output.vout
    else:
        two_cell = needs_high_v or servo_peak > 3000
        avg_w, _peak_w = _estimate_power(loads)
        battery = _choose_battery(design, catalog, avg_w, two_cell)
        vbat = battery.catalog.battery.nominal_v
        bspec = battery.catalog.battery
        if bspec.cells == 1 and (bspec.needs_charger or bspec.needs_protection):
            if req.power_source == "solar":
                charger = design.add(Part(id="solar_charger", catalog=catalog.get("cn3791-mppt"), role="power.charger", name="MPPT Solar Charger",
                                          reasons=["MPPT charging of a 1S Li-ion from a 6 V panel"]))  # fmt: skip
                panel = design.add(Part(id="solar_panel", catalog=catalog.get("solar-6v-2w"), role="power.solar", name="Solar Panel",
                                        reasons=["trickle charging"]))  # fmt: skip
                if bspec.needs_protection:
                    protection = design.add(Part(id="battery_protection", catalog=catalog.get("dw01-protection"), role="power.protection",
                                                 name="Battery Protection", reasons=["cell has no built-in PCM"]))  # fmt: skip
            else:
                charger = design.add(Part(id="charger", catalog=catalog.get("tp4056-usbc-protected"), role="power.charger",
                                          name="USB-C Charger + Protection", reasons=["1S Li-ion needs CC/CV charging and over-discharge protection"]))  # fmt: skip
        elif bspec.cells > 1 and bspec.chemistry.startswith("li"):
            design.issue(
                "info", "external_charger", f"{battery.catalog.name} needs an external balance charger.", [battery.id]
            )
        source = charger or battery
        primary_v = vbat

    # ---- rail assignment
    high_v = 12.0 if req.power_source == "wall" and needs_high_v else None
    assignments: dict[str, tuple[float, str]] = {}
    extra_supplies: list[tuple[Part, float, str]] = []  # second supply pins (level shifter HV side)
    for p in loads:
        if p.role == "logic.level_shift":
            assignments[p.id] = (logic_v, "LV")
            extra_supplies.append((p, 5.0, "HV"))
            continue
        choice = pick_rail_voltage(
            p, logic_v, vbat, high_v, (battery.catalog.battery.vmin, battery.catalog.battery.vmax) if battery else None
        )
        if choice is None:
            design.issue("error", "no_rail", f"No standard rail fits {p.name}'s supply range.", [p.id])
            continue
        assignments[p.id] = choice
    voltages = sorted({v for v, _ in assignments.values()} | {v for _, v, _ in extra_supplies}, reverse=True)

    # ---- regulators per rail (highest voltage first, so lower rails can be fed from higher ones)
    rails: list[Rail] = []
    src_out_pin = (source.catalog.power_output.pin_out if source.catalog.power_output else "B+") if source else "VOUT"
    primary_name = rail_name(primary_v, vbat)
    rail_src: dict[float, tuple[Part, Rail | None]] = {}
    for v in voltages:
        if abs(v - primary_v) < 1e-6:
            rails.append(Rail(primary_name, v, source.id, src_out_pin, _source_capacity(source, battery)))
            continue
        loads_v = [p for p in loads if assignments.get(p.id, (0,))[0] == v] + [
            p for p, ev, _ in extra_supplies if ev == v
        ]
        peak_ma = group_peak_ma(loads_v)
        if (
            abs(v - 3.3) < 1e-6
            and mcu
            and "3V3" in {pi.pin for pi in mcu.catalog.power_inputs}
            and assignments.get(mcu.id, (0,))[0] == 5.0
        ):
            if peak_ma <= MCU_3V3_BUDGET_MA:
                rails.append(Rail("3.3V", 3.3, mcu.id, "3V3", MCU_3V3_BUDGET_MA, parent=None))
                continue
        # candidate feeds: every existing higher rail and the primary source; rank by end-to-end efficiency
        primary_rail = next((r for r in rails if r.name == primary_name), None)
        feeds: list[Rail | None] = [r for r in rails if r.voltage > v + 0.2]
        if primary_rail is None:
            feeds.append(None)
        best = None
        for f in feeds:
            fv = f.voltage if f else primary_v
            found = _find_regulator(design, catalog, vin=_vin_range(battery, fv), vout=v, peak_ma=peak_ma)
            if found is None:
                continue
            cat_reg, s_reg, why, kind = found
            eff = cat_reg.power_output.efficiency * (_chain_eff(f, rails, design) if f else 1.0)
            key = (round(eff, 3), s_reg)
            if best is None or key > best[0]:
                best = (key, f, cat_reg, s_reg, why, kind, eff)
        if best is None:
            design.issue("error", "no_regulator", f"No regulator in the catalog can make {v:g} V from {primary_v:g} V.",
                         [p.id for p in loads_v], fix="Add a suitable regulator to the catalog.")  # fmt: skip
            continue
        _, feed, cat_reg, s_reg, why, kind, eff = best
        vs = f"{v:g}"
        reg = design.add(Part(id="reg_" + (vs.replace(".", "v") if "." in vs else vs + "v"), catalog=cat_reg, role=f"power.{kind}",
                              name=f"{v:g}V {'Boost' if kind == 'boost' else 'Regulator'}", score=s_reg,
                              reasons=why + [f"end-to-end efficiency {eff * 100:.0f}% from {feed.name if feed else primary_name}"]))  # fmt: skip
        rail_src[v] = (reg, feed)
        rails.append(Rail(rail_name(v, vbat), v, reg.id, reg.catalog.power_output.pin_out, reg.catalog.power_output.imax_ma,
                          parent=feed.name if feed else primary_name))  # fmt: skip

    # ---- connections
    conns: list[ElectricalConnection] = []
    if battery:
        chain = [battery] + [x for x in (protection, charger) if x is not None]
        for a, b in zip(chain, chain[1:]):
            src_pin = "B+" if a is battery else a.catalog.power_output.pin_out
            tgt_pin = (
                "B+" if b.catalog.power_output.kind in ("protection",) or b.catalog.id.startswith("tp4056") else "BAT+"
            )
            conns.append(ElectricalConnection(type="power", source=a.id, target=b.id, voltage=f"{vbat:g}V", source_pin=src_pin,
                                              target_pin=tgt_pin, net="VBAT"))  # fmt: skip
    if panel and charger:
        conns.append(ElectricalConnection(type="power", source=panel.id, target=charger.id, voltage=f"{panel.catalog.power_output.vout:g}V",
                                          source_pin="PV+", target_pin=charger.catalog.power_output.pin_in))  # fmt: skip
    for _v, (reg, feed) in rail_src.items():
        fv = feed.voltage if feed else primary_v
        conns.append(ElectricalConnection(type="power", source=feed.source_id if feed else source.id, target=reg.id,
                                          voltage=f"{fv:g}V", source_pin=feed.source_pin if feed else src_out_pin,
                                          target_pin=reg.catalog.power_output.pin_in, net=feed.name if feed else primary_name))  # fmt: skip
        if feed is not None:
            feed.loads.append(reg.id)
    for p in loads:
        if p.id not in assignments:
            continue
        v, pin = assignments[p.id]
        rail = next((r for r in rails if abs(r.voltage - v) < 1e-6), None)
        if rail is None:  # regulator missing: already reported as no_regulator
            continue
        target = (
            p.driven_by if (p.catalog.drive and p.catalog.drive.kind in ("dc_motor", "pump") and p.driven_by) else p.id
        )
        tgt_pin = _motor_pin(design.part(target)) if target != p.id else pin
        if target != p.id and any(c.target == target and c.target_pin == tgt_pin for c in conns):
            rail.loads.append(p.id)
            p.rail, p.supply_pin = rail.name, pin
            continue
        rail.loads.append(p.id)
        p.rail, p.supply_pin = rail.name, pin
        conns.append(ElectricalConnection(type="power", source=rail.source_id, target=target, voltage=f"{v:g}V",
                                          current=f"{_ma(p, 'typ'):.0f}mA", source_pin=rail.source_pin, target_pin=tgt_pin, net=rail.name))  # fmt: skip
    for p, v, pin in extra_supplies:
        rail = next((r for r in rails if abs(r.voltage - v) < 1e-6), None)
        if rail:
            conns.append(ElectricalConnection(type="power", source=rail.source_id, target=p.id, voltage=f"{v:g}V",
                                              source_pin=rail.source_pin, target_pin=pin, net=rail.name))  # fmt: skip
    # driver logic supplies (e.g. TB6612 VCC, PCA9685 VCC) come from the logic rail
    for drv in [p for p in design.parts if p.drives]:
        for pi in drv.catalog.power_inputs:
            if pi.pin in ("VCC", "VDD") and pi.vmin <= logic_v <= pi.vmax:
                logic_rail = next((r for r in rails if abs(r.voltage - logic_v) < 1e-6), None) or next(
                    (r for r in rails if abs(r.voltage - 5.0) < 1e-6), None
                )
                if logic_rail:
                    conns.append(ElectricalConnection(type="power", source=logic_rail.source_id, target=drv.id, voltage=f"{logic_rail.voltage:g}V",
                                                      source_pin=logic_rail.source_pin, target_pin=pi.pin, net=logic_rail.name))  # fmt: skip
    gnd_src = (charger or battery or source).id if (charger or battery or source) else design.parts[0].id
    design.connections = [
        c for c in design.connections if c.type not in ("power", "ground") or c.protocol in ("motor", "switched")
    ]
    design.connections += conns + ground_connections(design, gnd_src)
    design.rails = rails
    _propagate_currents(design, assignments, vbat)
    design.power_summary = _summary(design, battery, panel, vbat)
    return design


def group_peak_ma(loads: list[Part]) -> float:
    """Peak current of a set of loads with actuator diversity applied (same rules as rail propagation)."""
    peak = sum(_ma(p, "peak") for p in loads)
    servos = [p for p in loads if p.catalog.drive and p.catalog.drive.kind == "servo"]
    if len(servos) > 4:
        peak -= 0.5 * sum(sv.catalog.drive.stall_ma for sv in servos)
    motors = [p for p in loads if p.catalog.drive and p.catalog.drive.kind == "dc_motor"]
    if motors:
        worst = max(motors, key=lambda m: m.catalog.drive.stall_ma)
        peak += worst.catalog.drive.stall_ma - worst.catalog.drive.run_ma
    return peak


def _vin_range(battery: Part | None, parent_v: float) -> tuple[float, float]:
    if battery and abs(parent_v - battery.catalog.battery.nominal_v) < 1e-6:
        return battery.catalog.battery.vmin, battery.catalog.battery.vmax
    return parent_v * 0.95, parent_v * 1.05


def _source_capacity(source: Part | None, battery: Part | None) -> float:
    if source is None:
        return 0.0
    cap = source.catalog.power_output.imax_ma if source.catalog.power_output else 1e9
    if battery:
        cap = min(cap, battery.catalog.battery.max_discharge_ma)
    return cap


def _estimate_power(loads: list[Part]) -> tuple[float, float]:
    avg = sum(5.0 * _ma(p, "avg") for p in loads) / 1000 / 0.85
    peak = sum(5.0 * _ma(p, "peak") for p in loads) / 1000 / 0.85
    return avg, peak


def _choose_battery(design: Design, catalog: Catalog, avg_w: float, two_cell: bool) -> Part:
    req = design.requirements
    runtime = req.runtime_hours or 4.0
    need_wh = avg_w * runtime / USABLE_FRACTION
    loads = [p for p in design.parts if is_load(p)]
    peak_ma_at_5v = sum(_ma(p, "peak") for p in loads)
    cands = []
    for b in catalog.by_type("battery"):
        spec = b.battery
        if two_cell != (spec.cells > 1 and spec.nominal_v > 6.5):
            continue
        peak_at_batt = peak_ma_at_5v * 5.0 / spec.nominal_v / 0.88
        if spec.max_discharge_ma < 0.8 * peak_at_batt:
            continue
        cands.append(b)
    if not cands:
        cands = catalog.by_type("battery")
    need = Need("power.battery", prefer={"rechargeable"} if req.power_source == "solar" or runtime < 200 else set())
    # runtime is a requirement, not a preference: keep only packs that meet it (if any do)
    adequate = [b for b in cands if b.battery.capacity_mah * b.battery.nominal_v / 1000 >= need_wh]
    if adequate:
        cands = adequate
    else:
        biggest = max(b.battery.capacity_mah * b.battery.nominal_v for b in cands)
        cands = [b for b in cands if b.battery.capacity_mah * b.battery.nominal_v >= 0.999 * biggest]
    ranked = score_candidates(cands, need, req)
    best = None
    for b, s, reasons in ranked:
        wh = b.battery.capacity_mah * b.battery.nominal_v / 1000
        adequacy = min(1.0, wh / max(need_wh, 1e-6))
        total = 0.6 * adequacy + 0.4 * s
        if best is None or total > best[0]:
            best = (total, b, s, reasons + [f"{wh:.1f} Wh vs {need_wh:.1f} Wh needed for {runtime:g} h"])
    _, b, s, reasons = best
    part = design.add(Part(id="battery", catalog=b, role="power.battery", name="Battery", score=s, reasons=reasons))
    wh = b.battery.capacity_mah * b.battery.nominal_v / 1000
    if wh < need_wh:
        design.issue("warning", "battery_capacity", f"{b.name} gives ≈{wh * USABLE_FRACTION / max(avg_w, 1e-6):.1f} h, below the {runtime:g} h target.",
                     [part.id], fix="Use a larger pack or parallel cells.")  # fmt: skip
    return part


def _find_regulator(design: Design, catalog: Catalog, vin: tuple[float, float], vout: float, peak_ma: float):
    """Best regulator for vin range -> vout at peak current. Returns (part, score, reasons, kind) or None."""
    lo, hi = vin
    if lo > vout + 0.4:
        kinds = ["ldo", "buck"] if (peak_ma < 300 and hi - vout < 1.5) else ["buck", "ldo"]
    elif hi < vout:
        kinds = ["boost"]
    else:
        kinds = ["buck_boost", "boost"]
    for kind in kinds:
        cands: list[CatalogPart] = []
        for c in catalog.all():
            po = c.power_output
            if not po or po.kind != kind or po.vout is None:
                continue
            if abs(po.vout - vout) > 0.05 or po.vin_min > lo + 0.05 or po.vin_max < hi - 0.05 or po.imax_ma < peak_ma:
                continue
            cands.append(c)
        if cands:
            c, s, reasons = score_candidates(cands, None, design.requirements)[0]
            return (
                c,
                s,
                reasons
                + [f"{kind}: {lo:g}–{hi:g} V → {vout:g} V, {c.power_output.imax_ma:.0f} mA ≥ peak {peak_ma:.0f} mA"],
                kind,
            )
    return None


def _chain_eff(rail: Rail, rails: list[Rail], design: Design) -> float:
    """Product of converter efficiencies from the primary source down to ``rail``."""
    eff, cur, guard = 1.0, rail, 0
    names = {r.name: r for r in rails}
    while cur is not None and cur.parent is not None and guard < 10:
        src = design.part(cur.source_id) if design.has(cur.source_id) else None
        if src is not None and src.catalog.power_output:
            eff *= src.catalog.power_output.efficiency
        cur = names.get(cur.parent)
        guard += 1
    return eff


def _propagate_currents(design: Design, assignments: dict[str, tuple[float, str]], vbat: float | None) -> None:
    """Fill rail typ/peak/avg mA bottom-up: each regulated rail's input current is added to its parent rail.

    switcher: I_in = V_out * I_out / (eta * V_in) + I_q ;  LDO: I_in = I_out + I_q ;  MCU on-board LDO -> MCU's rail.
    """
    parts = {p.id: p for p in design.parts}
    by_name = {r.name: r for r in design.rails}
    reg_ids = {r.source_id for r in design.rails if parts.get(r.source_id) and parts[r.source_id].catalog.power_output}
    for r in design.rails:
        loads = [parts[i] for i in r.loads if i in parts and i not in reg_ids]
        r.typ_ma = sum(_ma(p, "typ") for p in loads)
        r.peak_ma = group_peak_ma(loads)  # servo/motor diversity applied
        r.avg_ma = sum(_ma(p, "avg") for p in loads)
    for r in sorted(design.rails, key=lambda r: r.voltage):  # children (lower voltage) first
        src = parts.get(r.source_id)
        if src is None:
            continue
        if src.catalog.type == "mcu":
            parent, k, iq = (by_name.get(src.rail) if src.rail else None), 1.0, 0.0
        else:
            po = src.catalog.power_output
            if po is None or po.kind in ("adapter", "charger", "protection", "solar") or r.parent is None:
                continue
            parent = by_name.get(r.parent)
            if parent is None:
                continue
            k = 1.0 if po.kind == "ldo" else r.voltage / (po.efficiency * parent.voltage)
            iq = po.quiescent_ma
        if parent is None:
            continue
        parent.typ_ma += r.typ_ma * k + iq
        parent.peak_ma += r.peak_ma * k + iq
        parent.avg_ma += r.avg_ma * k + iq
    for r in design.rails:
        r.typ_ma, r.peak_ma, r.avg_ma = round(r.typ_ma, 1), round(r.peak_ma, 1), round(r.avg_ma, 2)


def _summary(design: Design, battery: Part | None, panel: Part | None, vbat: float | None) -> dict:
    top = max(design.rails, key=lambda r: (r.name == "VBAT", r.voltage), default=None)
    root = [r for r in design.rails if design.has(r.source_id) and design.part(r.source_id).catalog.type != "mcu"]
    primary = next((r for r in root if r.name == "VBAT"), None) or (max(root, key=lambda r: r.voltage) if root else top)
    avg_w = (primary.voltage * primary.avg_ma / 1000) if primary else 0.0
    peak_w = (primary.voltage * primary.peak_ma / 1000) if primary else 0.0
    out = {
        "source": design.requirements.power_source,
        "rails": [
            {
                "name": r.name,
                "voltage": r.voltage,
                "source": r.source_id,
                "capacityMa": r.capacity_ma,
                "typMa": r.typ_ma,
                "peakMa": r.peak_ma,
                "avgMa": r.avg_ma,
                "parent": r.parent,
                "loads": r.loads,
            }  # fmt: skip
            for r in design.rails
        ],
        "avgPowerW": round(avg_w, 3),
        "peakPowerW": round(peak_w, 3),
    }
    if battery:
        b = battery.catalog.battery
        wh = b.capacity_mah * b.nominal_v / 1000
        out["battery"] = {"id": battery.id, "wh": round(wh, 2), "chemistry": b.chemistry, "cells": b.cells}
        out["runtimeHours"] = round(wh * USABLE_FRACTION / avg_w, 1) if avg_w > 0 else None
        if battery and primary and primary.peak_ma > b.max_discharge_ma:
            design.issue(
                "error",
                "battery_discharge",
                f"Peak draw {primary.peak_ma:.0f} mA exceeds {b.max_discharge_ma:.0f} mA battery rating.",
                [battery.id],
            )
    if panel:
        harvest = panel.catalog.power_output.vout * panel.catalog.power_output.imax_ma / 1000 * PEAK_SUN_HOURS * 0.75
        need = avg_w * 24
        out["solar"] = {
            "harvestWhPerDay": round(harvest, 2),
            "consumptionWhPerDay": round(need, 2),
            "energyNeutral": harvest >= need,
        }
        if harvest < need:
            design.issue("warning", "solar_deficit", f"Panel harvests ≈{harvest:.1f} Wh/day but the device uses ≈{need:.1f} Wh/day.",
                         [panel.id], fix="Use deep sleep / duty cycling or a larger panel.")  # fmt: skip
    return out
