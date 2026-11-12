"""Wiring engine: MCU pin allocation, netlist synthesis, electrical rule check (ERC), edge aggregation.

Pin allocation (constraint satisfaction, greedy with ordering heuristics)
-----------------------------------------------------------------------
* Shared buses are bound first to the MCU's default bus pins: I2C (SDA/SCL shared by all devices),
  SPI (MOSI/MISO/SCK shared, one CS per device), UART (one hardware port per device).
* Every remaining signal needs a pin with a capability (gpio | pwm | adc). Signals are processed
  most-constrained-first (fewest candidate pins), and each takes the candidate with the FEWEST
  capabilities, so scarce ADC/PWM pins are kept for signals that need them.
* Reserved/strapping pins are never allocated.  Failure raises ``PinAllocationError`` (used by the MCU
  feasibility filter).

Netlist
-------
Data connections carry exact pins (sourcePin/targetPin). Direction follows information flow:
sensors/inputs -> MCU; MCU -> displays/actuators/drivers. Power and ground connections are added by
``power.py``; ``ground_connections`` builds the explicit GND net (the reference product draws none).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hwbp.design import Design, Part
from hwbp.models import CatalogPart, ElectricalConnection

INPUT_TYPES = {"sensor", "input", "camera"}


class PinAllocationError(RuntimeError):
    pass


@dataclass
class Signal:
    part_id: str
    part_pin: str
    cap: str  # gpio | pwm | adc
    protocol: str


def _needs_for_part(part: Part) -> tuple[list[tuple[str, dict]], list[Signal]]:
    """Return (bus attachments [(protocol, {part_pin: role})], free signals) for one part."""
    buses: list[tuple[str, dict]] = []
    signals: list[Signal] = []
    for itf in part.catalog.interfaces:
        if itf.protocol in ("i2c", "spi", "uart"):
            buses.append((itf.protocol, dict(itf.pins)))
        elif itf.protocol == "csi":
            continue
        else:
            cap = {"adc": "adc", "pwm": "pwm"}.get(itf.protocol, "gpio")
            for pin in itf.pins:
                signals.append(Signal(part.id, pin, cap, itf.protocol))
        for pin, cap in itf.extra.items():
            signals.append(Signal(part.id, pin, cap if cap in ("gpio", "pwm", "adc") else "gpio", itf.protocol))
    return buses, signals


def _dry_run_parts(parts: list[Part]) -> list[Part]:
    """During MCU selection drivers don't exist yet: model the drivers they will need."""
    from hwbp.models import Interface

    out = [p for p in parts if not p.driven_by]
    servos = [p for p in out if p.catalog.drive and p.catalog.drive.kind == "servo"]
    motors = [p for p in out if p.catalog.drive and p.catalog.drive.kind == "dc_motor"]
    pumps = [p for p in out if p.catalog.drive and p.catalog.drive.kind == "pump"]
    if len(servos) > 2:
        out = [p for p in out if p not in servos]
        fake = CatalogPart(
            id="_pca",
            type="driver",
            name="pca",
            interfaces=[Interface(protocol="i2c", pins={"SDA": "sda", "SCL": "scl"}, address="0x40")],
        )
        out.append(Part(id="_servo_driver", catalog=fake, role="driver.servo", name="servo driver"))
    for i in range(0, len(motors), 2):
        pins = {f"IN{k}": "out" for k in range(1, 5 if len(motors[i : i + 2]) == 2 else 3)}
        fake = CatalogPart(id=f"_drv{i}", type="driver", name="drv", interfaces=[Interface(protocol="pwm", pins=pins)])
        out.append(Part(id=f"_motor_driver{i}", catalog=fake, role="driver.dc_motor", name="motor driver"))
    for i, _ in enumerate(pumps):
        fake = CatalogPart(
            id=f"_mos{i}", type="driver", name="mos", interfaces=[Interface(protocol="pwm", pins={"SIG": "out"})]
        )
        out.append(Part(id=f"_pump_switch{i}", catalog=fake, role="driver.pump", name="pump switch"))
    return out


def allocate_pins(mcu: CatalogPart, parts: list[Part], dry_run: bool = False) -> dict:
    """Allocate MCU pins for all parts wired directly to the MCU.

    Returns ``{"pin_map": {part_id: {part_pin: mcu_pin}}, "buses": [...], "used": {mcu_pin: [part_id, ...]}}``.
    """
    spec = mcu.mcu
    if spec is None:
        raise PinAllocationError(f"{mcu.name} is not an MCU")
    candidates = (
        _dry_run_parts(parts) if dry_run else [p for p in parts if p.catalog.type != "mcu" and _wired_to_mcu(p)]
    )
    reserved = set(spec.reserved)
    free = {p.name: set(p.caps) for p in spec.pins if p.name not in reserved}
    pin_map: dict[str, dict[str, str]] = {}
    used: dict[str, list[str]] = {}
    buses: list[dict] = []

    def take(pin: str, owner: str) -> None:
        free.pop(pin, None)
        used.setdefault(pin, []).append(owner)

    signals: list[Signal] = []
    uart_idx = 0
    bus_inst: dict[str, dict] = {}
    for part in candidates:
        attachments, sigs = _needs_for_part(part)
        signals += sigs
        for proto, roles in attachments:
            if proto in ("i2c", "spi"):
                if proto not in bus_inst:
                    insts = spec.buses.get(proto) or []
                    if not insts:
                        raise PinAllocationError(f"{mcu.name} has no {proto.upper()} bus")
                    inst = dict(insts[0])
                    for _role, pin in inst.items():
                        if pin in reserved:
                            raise PinAllocationError(f"{proto} pin {pin} is reserved")
                        take(pin, f"{proto}0")
                    bus_inst[proto] = inst
                    buses.append({"protocol": proto, "index": 0, "pins": inst, "devices": []})
                inst = bus_inst[proto]
                bus = next(b for b in buses if b["protocol"] == proto)
                bus["devices"].append(part.id)
                for part_pin, role in roles.items():
                    if role == "cs":
                        signals.append(Signal(part.id, part_pin, "gpio", "spi"))
                    elif role in inst:
                        pin_map.setdefault(part.id, {})[part_pin] = inst[role]
                    elif role == "miso" and proto == "spi":
                        continue  # bus without MISO (write-only displays)
            elif proto == "uart":
                insts = [u for u in spec.buses.get("uart", []) if not set(u.values()) & reserved]
                if uart_idx >= len(insts):
                    raise PinAllocationError(f"{mcu.name}: not enough UART ports ({len(insts)})")
                inst = insts[uart_idx]
                uart_idx += 1
                buses.append({"protocol": "uart", "index": uart_idx - 1, "pins": inst, "devices": [part.id]})
                for part_pin, role in roles.items():
                    # crossover: part TX -> MCU RX, part RX -> MCU TX
                    mcu_pin = inst["rx"] if role == "tx" else inst["tx"]
                    take(mcu_pin, part.id)
                    pin_map.setdefault(part.id, {})[part_pin] = mcu_pin

    # most-constrained-first greedy assignment
    def options(sig: Signal) -> list[str]:
        return [p for p, caps in free.items() if sig.cap in caps]

    pending = list(signals)
    while pending:
        pending.sort(key=lambda s: (len(options(s)), s.part_id, s.part_pin))
        sig = pending.pop(0)
        opts = options(sig)
        if not opts:
            raise PinAllocationError(f"{mcu.name}: no free {sig.cap.upper()} pin for {sig.part_id}.{sig.part_pin}")
        pin = min(opts, key=lambda p: (len(free[p]), _pin_order(p)))
        take(pin, sig.part_id)
        pin_map.setdefault(sig.part_id, {})[sig.part_pin] = pin
    return {"pin_map": pin_map, "buses": buses, "used": used, "free": sorted(free, key=_pin_order)}


def _pin_order(name: str) -> tuple[str, int]:
    digits = "".join(ch for ch in name if ch.isdigit())
    return name.rstrip("0123456789"), int(digits) if digits else -1


def _wired_to_mcu(part: Part) -> bool:
    if part.driven_by:
        return False  # loads behind a driver are wired to the driver
    return bool(part.catalog.interfaces)


# ------------------------------------------------------------------------------ netlist
def build_data_connections(design: Design) -> None:
    mcu = design.mcu
    if mcu is None:
        return
    result = allocate_pins(mcu.catalog, design.parts)
    design.pin_map = result["pin_map"]
    design.buses = result["buses"]
    conns: list[ElectricalConnection] = []
    for part in design.parts:
        mapping = design.pin_map.get(part.id)
        if not mapping:
            continue
        proto_of = {}
        for itf in part.catalog.interfaces:
            for pin in list(itf.pins) + list(itf.extra):
                proto_of[pin] = itf.protocol if pin in itf.pins else ("gpio" if itf.extra.get(pin) != "pwm" else "pwm")
        inbound = part.catalog.type in INPUT_TYPES
        for part_pin, mcu_pin in mapping.items():
            proto = proto_of.get(part_pin, "gpio")
            if proto == "uart":
                is_tx = any(itf.pins.get(part_pin) == "tx" for itf in part.catalog.interfaces)
                src, tgt, sp, tp = (
                    (part.id, mcu.id, part_pin, mcu_pin) if is_tx else (mcu.id, part.id, mcu_pin, part_pin)
                )
            elif inbound:
                src, tgt, sp, tp = part.id, mcu.id, part_pin, mcu_pin
            else:
                src, tgt, sp, tp = mcu.id, part.id, mcu_pin, part_pin
            net = (
                f"{proto.upper()}_{part_pin}"
                if proto in ("i2c", "spi") and part_pin in ("SDA", "SCL", "MOSI", "MISO", "SCK")
                else None
            )
            conns.append(
                ElectricalConnection(
                    type="data", source=src, target=tgt, protocol=proto, source_pin=sp, target_pin=tp, net=net
                )
            )
    conns += _driver_load_connections(design)
    conns = insert_level_shifters(design, conns)
    design.connections = [c for c in design.connections if c.type != "data"] + conns


def insert_level_shifters(
    design: Design, conns: list[ElectricalConnection], catalog_id: str = "level-shifter-bss138"
) -> list[ElectricalConnection]:
    """Route every signal of a logic-incompatible part through 4-channel BSS138 shifters (LVn <-> HVn)."""
    from hwbp.catalog import default_catalog
    from hwbp.selection import logic_compatible

    mcu = design.mcu
    if mcu is None:
        return conns
    bad = {p.id for p in design.parts if p.id != mcu.id and not logic_compatible(p.catalog, mcu.catalog)}
    lines = [
        c for c in conns if c.type == "data" and (c.source in bad or c.target in bad) and mcu.id in (c.source, c.target)
    ]
    if not lines:
        return conns
    catalog = default_catalog()
    shifters: list[Part] = [p for p in design.parts if p.role == "logic.level_shift"]
    needed = (len(lines) + 3) // 4
    while len(shifters) < needed:
        shifters.append(design.add(Part(id="level_shifter", catalog=catalog.get(catalog_id), role="logic.level_shift",
                                        name="Level Shifter" if needed == 1 else f"Level Shifter {len(shifters) + 1}",
                                        reasons=["protects 3.3 V GPIO from 5 V signals / meets 5 V input thresholds"])))  # fmt: skip
    out = [c for c in conns if c not in lines]
    for i, c in enumerate(lines):
        ls, ch = shifters[i // 4], i % 4 + 1
        mcu_is_src = c.source == mcu.id
        lv = ElectricalConnection(type="data", protocol=c.protocol, source=mcu.id if mcu_is_src else ls.id, target=ls.id if mcu_is_src else mcu.id,
                                  source_pin=c.source_pin if mcu_is_src else f"LV{ch}", target_pin=f"LV{ch}" if mcu_is_src else c.target_pin)  # fmt: skip
        hv = ElectricalConnection(type="data", protocol=c.protocol, source=ls.id if mcu_is_src else c.source, target=c.target if mcu_is_src else ls.id,
                                  source_pin=f"HV{ch}" if mcu_is_src else c.source_pin, target_pin=c.target_pin if mcu_is_src else f"HV{ch}")  # fmt: skip
        out += [lv, hv]
    return out


def _driver_load_connections(design: Design) -> list[ElectricalConnection]:
    out = []
    for drv in design.parts:
        for i, load_id in enumerate(drv.drives):
            load = design.part(load_id)
            kind = load.catalog.drive.kind if load.catalog.drive else ""
            if kind == "servo":
                out.append(ElectricalConnection(type="data", source=drv.id, target=load.id, protocol="pwm",
                                                source_pin=f"PWM{i}", target_pin="SIG"))  # fmt: skip
            elif kind == "dc_motor":
                ch = "A" if i % 2 == 0 else "B"
                out.append(ElectricalConnection(type="power", source=drv.id, target=load.id, protocol="motor",
                                                source_pin=f"{ch}OUT1", target_pin="M+", voltage=None, current=f"{load.catalog.drive.stall_ma:.0f}mA stall"))  # fmt: skip
            else:
                out.append(ElectricalConnection(type="power", source=drv.id, target=load.id, protocol="switched",
                                                source_pin="OUT-", target_pin="-"))  # fmt: skip
    return out


def ground_connections(design: Design, ground_source: str) -> list[ElectricalConnection]:
    """Star ground from the primary source to every electrical part with a power input."""
    out = []
    for p in design.parts:
        if p.id == ground_source or p.catalog.category != "electrical":
            continue
        if not (
            p.catalog.power_inputs or p.catalog.power_output or p.catalog.mcu or p.catalog.drive or p.catalog.battery
        ):
            continue
        out.append(
            ElectricalConnection(
                type="ground", source=ground_source, target=p.id, source_pin="GND", target_pin="GND", net="GND"
            )
        )
    return out


# ------------------------------------------------------------------------------ ERC
def validate(design: Design) -> None:
    """Electrical rule check. Adds ``Issue`` objects to the design (never raises)."""
    parts = {p.id: p for p in design.parts}
    # 1. supply voltage within range
    for c in design.connections:
        if c.type != "power" or c.voltage is None or c.target not in parts:
            continue
        load = parts[c.target]
        v = _volts(c.voltage)
        rng = [pi for pi in load.catalog.power_inputs if pi.pin == c.target_pin] or load.catalog.power_inputs
        if v is not None and rng and not any(pi.vmin - 0.05 <= v <= pi.vmax + 0.05 for pi in rng):
            design.issue(
                "error",
                "supply_voltage",
                f"{load.name} gets {v:g} V on {c.target_pin}, rated {rng[0].vmin:g}–{rng[0].vmax:g} V.",
                [load.id],
            )
    # 2. rail current budget
    for rail in design.rails:
        if rail.peak_ma > rail.capacity_ma:
            design.issue("error", "rail_overcurrent", f"{rail.name} rail peak {rail.peak_ma:.0f} mA exceeds {rail.capacity_ma:.0f} mA source rating.",
                         [rail.source_id], fix="Choose a higher-current regulator or split the rail.")  # fmt: skip
        elif rail.typ_ma > 0.8 * rail.capacity_ma:
            design.issue(
                "warning",
                "rail_headroom",
                f"{rail.name} rail runs at {100 * rail.typ_ma / rail.capacity_ma:.0f}% of its rating.",
                [rail.source_id],
            )
    # 3. I2C address conflicts (resolve via alternate addresses when possible)
    _resolve_i2c_addresses(design)
    # 4. MCU pin conflicts (non-bus pins used twice)
    used: dict[str, list[str]] = {}
    shared = {pin for b in design.buses if b["protocol"] in ("i2c", "spi") for pin in b["pins"].values()}
    for pid, mapping in design.pin_map.items():
        for mcu_pin in mapping.values():
            if mcu_pin not in shared:
                used.setdefault(mcu_pin, []).append(pid)
    for pin, owners in used.items():
        if len(set(owners)) > 1:
            design.issue(
                "error",
                "pin_conflict",
                f"MCU pin {pin} assigned to {', '.join(sorted(set(owners)))}.",
                sorted(set(owners)),
            )
    # 5. orphans / unpowered (topology, like the reference) + loads without drivers
    touched = {c.source for c in design.connections} | {c.target for c in design.connections}
    powered = {c.target for c in design.connections if c.type == "power"} | {
        c.source for c in design.connections if c.type == "power"
    }
    for p in design.parts:
        if p.catalog.category != "electrical" or p.catalog.type in ("passive",):
            continue
        needs_supply = bool(p.catalog.power_inputs or p.catalog.drive or p.catalog.mcu)
        if p.id not in touched:
            design.issue("warning", "orphan", f"{p.name} is not wired to anything.", [p.id])
        elif needs_supply and p.id not in powered and p.catalog.type not in ("camera",):
            design.issue("warning", "unpowered", f"{p.name} has no power connection.", [p.id])
        if p.catalog.drive and p.catalog.drive.kind in ("dc_motor", "pump", "solenoid") and not p.driven_by:
            design.issue(
                "error",
                "missing_driver",
                f"{p.name} must not be driven from an MCU pin directly.",
                [p.id],
                fix="Add a driver.",
            )
    # 6. reserved pins
    if design.mcu and design.mcu.catalog.mcu:
        reserved = set(design.mcu.catalog.mcu.reserved)
        for pid, mapping in design.pin_map.items():
            for part_pin, mcu_pin in mapping.items():
                if mcu_pin in reserved:
                    design.issue(
                        "warning", "reserved_pin", f"{pid}.{part_pin} uses reserved/strapping pin {mcu_pin}.", [pid]
                    )


def _resolve_i2c_addresses(design: Design) -> None:
    taken: dict[str, str] = {}
    for p in design.parts:
        for itf in p.catalog.interfaces:
            if itf.protocol != "i2c" or not itf.address or not design.pin_map.get(p.id):
                continue
            addr = itf.address.lower()
            if addr not in taken:
                taken[addr] = p.id
                continue
            alt = next((a.lower() for a in itf.alt_addresses if a.lower() not in taken), None)
            if alt:
                taken[alt] = p.id
                design.issue(
                    "info",
                    "i2c_address_changed",
                    f"{p.name} moved to I2C address {alt} (solder/strap ADDR) to avoid {taken[addr]}.",
                    [p.id],
                )
            else:
                design.issue("error", "i2c_address_conflict", f"{p.name} and {taken[addr]} both use I2C address {addr}.",
                             [p.id, taken[addr]], fix="Add a TCA9548A I2C multiplexer.")  # fmt: skip


def _volts(text: str | None) -> float | None:
    if not text:
        return None
    t = text.strip().upper().replace("V", "")
    try:
        return float(t)
    except ValueError:
        return None


# ------------------------------------------------------------------------------ edge aggregation (diagram)
def aggregate_edges(connections: list[ElectricalConnection], include_ground: bool = False) -> list[dict]:
    """Collapse pin-level connections into diagram edges with labels.

    power: one edge per unordered node pair, label = voltage (first seen) — as in the reference product.
    data : one edge per directed pair, label = "proto ×n, proto ×m" (protocol counts, lower-case).
    ground: one edge per unordered pair (hidden unless ``include_ground``).
    """
    edges: dict[tuple, dict] = {}
    for c in connections:
        if c.type == "ground" and not include_ground:
            continue
        if c.type in ("power", "ground"):
            key = (c.type, *sorted((c.source, c.target)))
            if key not in edges:
                edges[key] = {
                    "type": c.type,
                    "source": c.source,
                    "target": c.target,
                    "label": c.voltage or ("GND" if c.type == "ground" else ""),
                    "count": 1,
                }
            else:
                edges[key]["count"] += 1
        else:
            key = ("data", c.source, c.target)
            e = edges.setdefault(
                key, {"type": "data", "source": c.source, "target": c.target, "protocols": {}, "count": 0}
            )
            proto = (c.protocol or "data").lower()[:12]
            e["protocols"][proto] = e["protocols"].get(proto, 0) + 1
            e["count"] += 1
    out = []
    for e in edges.values():
        if e["type"] == "data":
            e["label"] = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in e.pop("protocols").items())
        out.append(e)
    return out


# ------------------------------------------------------------------------------ rail inference (foreign documents)
POWER_PIN_RE = re.compile(r"^(VCC|VIN|VDD|V\+|5V|3V3|3\.3V|VBAT|VSYS|\+5V|\+3V3|\+)$", re.I)
GND_PIN_RE = re.compile(r"^(GND|VSS|0V|-|GND\d*)$", re.I)


def _pin_volts(pin: str) -> float | None:
    p = pin.upper()
    if "3V3" in p or "3.3" in p:
        return 3.3
    if p in ("5V", "+5V"):
        return 5.0
    return None


def infer_rails(nodes: list[dict], connections: list[dict]) -> list[ElectricalConnection]:
    """Ground/rail inference for documents that only carry signal + power edges (e.g. from the reference product).

    * ground: star from the power root (a node that sources power but is never powered) to every node that
      exposes a ground-like pin and has no ground connection yet;
    * supply: nodes with a power-like pin but no incoming power edge get one from an existing source with a
      matching voltage (pin name hints: 3V3 -> 3.3 V, 5V -> 5 V), else from the most-used rail source.
    """
    conns = [ElectricalConnection.model_validate(c) for c in connections]
    ids = [n["id"] for n in nodes]
    pins = {n["id"]: list(n.get("pins") or []) for n in nodes}
    power = [c for c in conns if c.type == "power"]
    sources = {c.source for c in power}
    targets = {c.target for c in power}
    roots = [i for i in ids if i in sources and i not in targets] or [i for i in ids if i in sources]
    out: list[ElectricalConnection] = []
    if roots:
        root = roots[0]
        grounded = {c.target for c in conns if c.type == "ground"} | {c.source for c in conns if c.type == "ground"}
        for i in ids:
            gpin = next((p for p in pins[i] if GND_PIN_RE.match(p)), None)
            if i != root and gpin and i not in grounded:
                out.append(
                    ElectricalConnection(
                        type="ground", source=root, target=i, source_pin="GND", target_pin=gpin, net="GND"
                    )
                )
    by_volt: dict[float, str] = {}
    usage: dict[str, int] = {}
    for c in power:
        usage[c.source] = usage.get(c.source, 0) + 1
        v = _volts(c.voltage)
        if v is not None:
            by_volt.setdefault(v, c.source)
    for i in ids:
        if i in targets or i in sources:
            continue
        ppin = next((p for p in pins[i] if POWER_PIN_RE.match(p)), None)
        if not ppin or not usage:
            continue
        v = _pin_volts(ppin)
        src = by_volt.get(v) if v is not None else None
        src = src or max(usage, key=lambda k: (usage[k], k))
        volt = v if v is not None else next((vv for vv, s in by_volt.items() if s == src), None)
        out.append(
            ElectricalConnection(
                type="power", source=src, target=i, target_pin=ppin, voltage=f"{volt:g}V" if volt else None
            )
        )
    return out
