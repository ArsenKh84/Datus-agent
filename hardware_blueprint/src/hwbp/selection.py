"""Component selection decision engine + component offering (alternatives / additions).

Pipeline
--------
1. ``plan_needs``      requirements -> list of ``Need`` (capability, quantity, hard constraints, preferences)
2. ``select_peripherals`` greedy weighted set-cover: each candidate part covers 1..n needed capabilities;
   pick the part with the best ``score / remaining-cost`` ratio, repeat until covered.
3. ``select_mcu``      filter MCUs by hard feasibility (radios, UART/ADC/I2S counts, pin-allocation dry-run,
   camera), then rank by weighted multi-criteria score.
4. re-score peripherals against the chosen MCU (logic-level compatibility), add drivers for loads,
   enforce the budget (downgrade loop), and attach ranked alternatives to every slot.

Scoring (all criteria normalised to 0..1 inside the candidate set, higher is better)
------------------------------------------------------------------------------------
  score = Σ w_k * c_k  with criteria  cost, popularity, power, size, fit, compat
  default weights  cost .30  popularity .20  power .15  size .10  fit .15  compat .10
  priorities shift weights: cost x2, battery_life -> power x2.5, precision -> fit x2, tiny/small -> size x2.5
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hwbp.catalog import Catalog
from hwbp.design import Alternative, Design, Part, slug
from hwbp.models import CatalogPart, Suggestion
from hwbp.pricing import best_offer
from hwbp.requirements import Requirements

# Capabilities that the MCU board itself provides (no extra part needed).
MCU_PROVIDED = {"comm.wifi": "wifi", "comm.ble": "ble"}

ROLE_NAMES = {
    "sense.temperature": "Environment Sensor",
    "sense.humidity": "Environment Sensor",
    "sense.distance": "Distance Sensor",
    "sense.presence": "Presence Radar",
    "sense.motion": "Motion Sensor",
    "sense.imu": "Motion IMU",
    "sense.pulse": "Pulse Oximeter",
    "sense.light": "Light Sensor",
    "sense.soil_moisture": "Soil Moisture Probe",
    "sense.leak": "Leak Sensor",
    "sense.co2": "CO2 Sensor",
    "sense.gas": "Gas Sensor",
    "sense.gps": "GPS Receiver",
    "sense.camera": "Camera",
    "sense.audio": "Microphone",
    "sense.current": "Power Monitor",
    "ui.display": "Display",
    "comm.lora": "LoRa Radio",
    "comm.cellular": "Cellular Modem",
    "actuate.servo": "Servo",
    "actuate.drive": "Drive Motor",
    "actuate.stepper": "Stepper",
    "actuate.pump": "Water Pump",
    "actuate.relay": "Relay",
    "output.light": "LED Strip",
    "output.audio": "Speaker Amp",
    "output.buzzer": "Buzzer",
    "input.button": "Button",
    "input.encoder": "Rotary Knob",
    "input.joystick": "Joystick",
}

# capabilities that become one node per unit (each needs its own MCU pin)
PER_UNIT = {"actuate.servo", "actuate.drive", "actuate.stepper", "input.button", "sense.distance"}


@dataclass
class Need:
    capability: str
    quantity: int = 1
    require: set[str] = field(default_factory=set)  # extra capabilities the part MUST have
    prefer: set[str] = field(default_factory=set)  # capabilities that earn a fit bonus
    avoid: set[str] = field(default_factory=set)  # capabilities to rule out while any other candidate remains


def plan_needs(req: Requirements) -> list[Need]:
    caps = set(req.capabilities)
    needs: list[Need] = []
    for cap in sorted(caps):
        if cap in MCU_PROVIDED or cap.startswith("display."):
            continue
        if cap == "sense.camera":
            continue  # satisfied by MCU choice (camera-capable board) or a CSI camera, see select_mcu
        need = Need(cap, req.counts.get(cap, 1))
        if cap == "ui.display":
            if "display.touch" in caps:
                need.require.add("display.touch")
            if "display.epaper" in caps:
                need.require.add("display.epaper")
            if req.size_class in ("tiny", "small") and "display.touch" not in caps:
                need.prefer.add("display.small")
            if "battery_life" in req.priorities:
                need.prefer.add("low_power")
            if req.size_class == "large" or "display.touch" in caps:
                need.prefer.add("display.large")
            frac = (req.form_factor or {}).get("displayFraction")  # drawn display window / front area
            if frac and frac >= 0.2:  # a window covering a fifth of the front cannot hold a 0.96" OLED
                need.prefer.add("display.large")
                need.avoid.add("display.small")
            elif frac and frac <= 0.08:
                need.prefer.add("display.small")
                need.avoid.add("display.large")
        if cap == "actuate.servo" and ("robustness" in req.priorities or req.size_class == "large"):
            need.prefer.add("high_torque")
        if cap == "actuate.stepper" and ("precision" in req.priorities or req.size_class == "large"):
            need.prefer.add("high_torque")
        if cap == "actuate.drive" and req.size_class in ("tiny", "small"):
            need.prefer.add("small")
        if "precision" in req.priorities and cap.startswith("sense."):
            need.prefer.add("precision")
        # explicit technology words in the prompt ("radar", "epaper", "oled") become preferences
        for kw in req.keywords:
            need.prefer.add(f"kw:{kw}")
        if cap in req.force:  # an explicit user choice overrides constraints inherited from the prompt
            need.require = set()
        needs.append(need)
    return needs


def keyword_hits(part: CatalogPart, keywords: set[str]) -> set[str]:
    """Prompt keywords that literally appear in the part's name / capability leaves / tags."""
    import re as _re

    words = set(_re.findall(r"[a-z0-9]+", f"{part.name} {part.mpn or ''}".lower()))
    words |= {c.split(".")[-1] for c in part.capabilities} | set(part.tags)
    return {k for k in keywords if k in words}


# ------------------------------------------------------------------------------ scoring
def weights_for(req: Requirements) -> dict[str, float]:
    w = {"cost": 0.30, "popularity": 0.20, "power": 0.15, "size": 0.10, "fit": 0.15, "compat": 0.10}
    if "cost" in req.priorities or req.budget_usd is not None:
        w["cost"] *= 2
    if "battery_life" in req.priorities or req.power_source == "solar":
        w["power"] *= 2.5
    if "precision" in req.priorities:
        w["fit"] *= 2
    if req.size_class in ("tiny", "small"):
        w["size"] *= 2.5
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def unit_price(part: CatalogPart, qty: int = 1) -> float:
    q = best_offer(part.offers, qty)
    if q is not None:
        return q.unit_usd
    return float(part.est_price_usd or 0.0)


def _volume(p: CatalogPart) -> float:
    if not p.dims_mm:
        return 1000.0
    w, h, d = p.dims_mm
    return max(1.0, w * h * d)


def _current(p: CatalogPart, battery_life: bool) -> float:
    c = p.current_ma or {}
    if battery_life and "sleep" in c:
        return max(0.001, 0.7 * c.get("sleep", 0) + 0.3 * c.get("typ", 0))
    return max(0.001, c.get("typ", c.get("peak", 1.0)))


def effective_logic_v(part: CatalogPart) -> float | None:
    """Declared logic level, else 3.3 V for parts whose every supply input tops out below 3.7 V."""
    if part.logic_v is not None:
        return part.logic_v
    if part.power_inputs and all(pi.vmax <= 3.7 for pi in part.power_inputs) and not part.io_5v_tolerant:
        return 3.3
    return None


def logic_compatible(part: CatalogPart, mcu: CatalogPart | None) -> bool:
    """False when a 5 V-logic part would drive a non-5V-tolerant 3.3 V MCU (or vice-versa threshold risk)."""
    if mcu is None or mcu.mcu is None:
        return True
    mv = mcu.mcu.logic_v
    pv = effective_logic_v(part)
    if pv is None or abs(pv - mv) < 0.5:
        return True
    if pv > mv:  # part outputs 5 V into 3.3 V pins
        return mcu.io_5v_tolerant
    # 3.3 V part driven by 5 V MCU: ok if the part is 5 V tolerant
    return part.io_5v_tolerant


def score_candidates(
    cands: list[CatalogPart], need: Need | None, req: Requirements, mcu: CatalogPart | None = None, qty: int = 1
) -> list[tuple[CatalogPart, float, list[str]]]:
    """Return candidates sorted by score (desc) with human-readable reasons."""
    if not cands:
        return []
    w = weights_for(req)
    bl = "battery_life" in req.priorities or req.power_source == "solar"
    prices = {p.id: max(0.01, unit_price(p, qty)) for p in cands}
    min_price = min(prices.values())
    min_cur = min(_current(p, bl) for p in cands)
    min_vol = min(_volume(p) for p in cands)
    out = []
    for p in cands:
        c_cost = min_price / prices[p.id]
        c_pop = p.popularity
        c_pow = min_cur / _current(p, bl)
        c_size = min_vol / _volume(p)
        prefer = {x for x in (need.prefer if need else set()) if not x.startswith("kw:")}
        kws = {x[3:] for x in (need.prefer if need else set()) if x.startswith("kw:")}
        kw_hit = keyword_hits(p, kws)
        c_fit = (len(prefer & set(p.capabilities)) / len(prefer)) if prefer else 0.5
        if kw_hit:
            c_fit = min(1.5, c_fit + 0.5 * len(kw_hit))
        compat = logic_compatible(p, mcu)
        c_compat = 1.0 if compat else 0.0
        score = (
            w["cost"] * c_cost
            + w["popularity"] * c_pop
            + w["power"] * c_pow
            + w["size"] * c_size
            + w["fit"] * c_fit
            + w["compat"] * c_compat
        )
        reasons = [f"${prices[p.id]:.2f}/unit"]
        if c_cost == 1.0 and len(cands) > 1:
            reasons.append("lowest price among candidates")
        if prefer & set(p.capabilities):
            reasons.append("matches " + ", ".join(sorted(prefer & set(p.capabilities))))
        if kw_hit:
            reasons.append("named in the prompt: " + ", ".join(sorted(kw_hit)))
        if not compat:
            reasons.append(f"{effective_logic_v(p):g} V logic needs a level shifter with this MCU")
        if c_pop >= 0.85:
            reasons.append("very common part (libraries & tutorials)")
        out.append((p, round(score, 4), reasons))
    out.sort(key=lambda t: (-t[1], t[0].id))
    return out


# ------------------------------------------------------------------------------ peripherals
def candidates_for(need: Need, catalog: Catalog, req: Requirements | None = None) -> list[CatalogPart]:
    if req is not None and need.capability in req.force and req.force[need.capability] in catalog:
        return [catalog.get(req.force[need.capability])]
    cands = [p for p in catalog.with_capability(need.capability) if p.type != "mcu"]
    if req is not None and req.exclude:
        cands = [p for p in cands if p.id not in req.exclude]
    if need.require:
        cands = [p for p in cands if need.require <= set(p.capabilities)]
    if need.avoid:
        kept = [p for p in cands if not need.avoid & set(p.capabilities)]
        cands = kept or cands
    # A technology the user named explicitly ("radar", "e-paper", "oled") is a hard filter when some
    # candidates match it and others don't — the user asked for that technology, not just the function.
    kws = {x[3:] for x in need.prefer if x.startswith("kw:")}
    hits = {p.id: keyword_hits(p, kws) for p in cands}
    if any(hits.values()) and not all(hits.values()):
        named = [p for p in cands if hits[p.id] - _generic_words(need.capability)]
        if named:
            cands = named
    return cands


def _generic_words(capability: str) -> set[str]:
    """Words that merely restate the need (matching them says nothing about the technology)."""
    leaf = capability.split(".")[-1]
    return {leaf, leaf.rstrip("s"), "sensor", "module", "display", "screen", "motor"}


def select_peripherals(needs: list[Need], req: Requirements, catalog: Catalog, mcu: CatalogPart | None) -> list[Part]:
    """Greedy weighted set cover over capabilities; quantity>1 needs become per-unit nodes."""
    remaining = {n.capability: n for n in needs}
    parts: list[Part] = []
    used_ids: set[str] = set()
    while remaining:
        best = None
        for _cap, need in sorted(remaining.items()):
            for p, s, reasons in score_candidates(candidates_for(need, catalog, req), need, req, mcu, need.quantity):
                covers = [c for c in remaining if c in p.capabilities and remaining[c].quantity == need.quantity]
                covers = [c for c in covers if remaining[c].require <= set(p.capabilities)]
                # value = coverage bonus * score; multi-capability parts win when they cover more needs
                value = s * (1 + 0.6 * (len(covers) - 1))
                if best is None or value > best[0]:
                    best = (value, p, s, reasons, covers, need)
        if best is None:
            break
        _, p, s, reasons, covers, need = best
        for c in covers:
            remaining.pop(c, None)
        if not covers:  # need with no candidate at all
            remaining.pop(need.capability, None)
            continue
        alts = [
            Alternative(ap, asc, ar)
            for ap, asc, ar in score_candidates(candidates_for(need, catalog, req), need, req, mcu, need.quantity)
            if ap.id != p.id
        ][:3]
        base_name = ROLE_NAMES.get(need.capability, p.name)
        units = need.quantity if need.capability in PER_UNIT else 1
        for i in range(units):
            nm = base_name if units == 1 else f"{base_name} {i + 1}"
            parts.append(
                Part(
                    id=slug(nm),
                    catalog=p,
                    role=need.capability if units == 1 else f"{need.capability}#{i + 1}",
                    name=nm,
                    quantity=1 if need.capability in PER_UNIT else need.quantity,
                    score=s,
                    reasons=reasons + ([f"covers {', '.join(covers)}"] if len(covers) > 1 else []),
                    alternatives=alts,
                    capability=need.capability,
                )
            )
        used_ids.add(p.id)
    # report needs nothing in the catalog could satisfy
    return parts


# ------------------------------------------------------------------------------ MCU
@dataclass
class Demand:
    i2c: list[tuple[str, str | None]] = field(default_factory=list)  # (part id, address)
    spi: int = 0  # devices (each needs a CS)
    uart: int = 0
    i2s: int = 0
    adc: int = 0
    pwm: int = 0
    gpio: int = 0
    csi: int = 0
    radios: set[str] = field(default_factory=set)
    needs_camera: bool = False


def compute_demand(parts: list[Part], req: Requirements) -> Demand:
    d = Demand()
    for cap, radio in MCU_PROVIDED.items():
        if cap in req.capabilities:
            d.radios.add(radio)
    d.needs_camera = "sense.camera" in req.capabilities
    for part in parts:
        for itf in part.catalog.interfaces:
            proto = itf.protocol
            if proto == "i2c":
                d.i2c.append((part.id, itf.address))
            elif proto == "spi":
                d.spi += 1
            elif proto == "uart":
                d.uart += 1
            elif proto == "i2s":
                d.i2s += 1
            elif proto == "adc":
                d.adc += len(itf.pins)
            elif proto == "pwm":
                d.pwm += len(itf.pins)
            elif proto == "csi":
                d.csi += 1
            else:
                d.gpio += len(itf.pins)
            for cap in itf.extra.values():
                if cap == "pwm":
                    d.pwm += 1
                elif cap == "adc":
                    d.adc += 1
                else:
                    d.gpio += 1
    return d


def mcu_feasible(mcu: CatalogPart, demand: Demand, parts: list[Part]) -> tuple[bool, list[str]]:
    from hwbp.wiring import PinAllocationError, allocate_pins  # local import: wiring depends on selection types

    spec = mcu.mcu
    if spec is None:
        return False, ["not an MCU"]
    problems = []
    if not demand.radios <= set(spec.radios):
        problems.append(f"missing radio(s) {sorted(demand.radios - set(spec.radios))}")
    if demand.uart > len(spec.buses.get("uart", [])):
        problems.append(f"needs {demand.uart} UARTs, has {len(spec.buses.get('uart', []))}")
    adc_pins = sum(1 for p in spec.pins if "adc" in p.caps and p.name not in spec.reserved)
    if demand.adc > adc_pins:
        problems.append(f"needs {demand.adc} ADC inputs, has {adc_pins}")
    if demand.csi and "camera_interface" not in mcu.capabilities:
        problems.append("no CSI camera interface")
    if demand.needs_camera and not demand.csi and "sense.camera" not in mcu.capabilities:
        problems.append("camera required but board has no camera")
    if spec.framework.startswith("linux") and demand.adc:
        problems.append("Linux SBC has no ADC")
    if not problems:
        try:
            allocate_pins(mcu, parts, dry_run=True)
        except PinAllocationError as e:
            problems.append(str(e))
    return not problems, problems


def select_mcu(
    parts: list[Part], req: Requirements, catalog: Catalog
) -> tuple[CatalogPart, float, list[str], list[Alternative]]:
    demand = compute_demand(parts, req)
    feasible, rejected = [], {}
    pool = catalog.by_type("mcu")
    if req.force.get("mcu") in catalog:
        pool = [catalog.get(req.force["mcu"])]
    pool = [m for m in pool if m.id not in req.exclude] or pool
    for m in pool:
        ok, why = mcu_feasible(m, demand, parts)
        (feasible.append(m) if ok else rejected.__setitem__(m.id, why))
    if not feasible:
        raise SelectionError(f"no MCU in the catalog satisfies the design: {rejected}")
    need = Need("mcu", prefer={f"kw:{k}" for k in req.keywords})
    if "performance" in req.priorities or any(
        p.catalog.type == "display" and "display.large" in p.catalog.capabilities for p in parts
    ):
        need.prefer.add("compute.high")
    if req.size_class in ("tiny", "small"):
        need.prefer.add("small")
    if "sense.camera" in req.capabilities and "ai.vision" in " ".join(req.capabilities + [req.prompt.lower()]):
        need.prefer.add("ai.vision")
    ranked = score_candidates(feasible, need, req, None)
    # compat criterion for MCU: fraction of peripherals that are logic-compatible
    rescored = []
    from hwbp.wiring import allocate_pins

    for m, s, reasons in ranked:
        incompatible = [p.id for p in parts if not logic_compatible(p.catalog, m)]
        s2 = s - 0.08 * len(incompatible)
        if incompatible:
            reasons = reasons + [f"{len(incompatible)} peripheral(s) need level shifting"]
        alloc = allocate_pins(m, parts, dry_run=True)
        n_used = len(alloc["used"])
        spare = len(alloc["free"]) / max(1, len(alloc["free"]) + n_used)
        s2 += 0.1 * min(1.0, spare / 0.3)  # headroom criterion: prefer ≥30 % spare pins for future edits
        reasons = reasons + [f"{len(alloc['free'])} spare GPIO after allocation"]
        rescored.append((m, round(s2, 4), reasons))
    rescored.sort(key=lambda t: (-t[1], t[0].id))
    best, score, reasons = rescored[0]
    alts = [Alternative(m, s, r) for m, s, r in rescored[1:4]]
    reasons = reasons + [f"rejected: {k} ({'; '.join(v)})" for k, v in sorted(rejected.items())][:3]
    return best, score, reasons, alts


class SelectionError(RuntimeError):
    pass


# ------------------------------------------------------------------------------ drivers for loads
def add_drivers(design: Design, catalog: Catalog) -> None:
    """Add motor/servo/pump drivers sized for the loads (stall current, voltage, channel count)."""
    req = design.requirements
    motors = [p for p in design.parts if p.catalog.drive and p.catalog.drive.kind == "dc_motor"]
    servos = [p for p in design.parts if p.catalog.drive and p.catalog.drive.kind == "servo"]
    pumps = [p for p in design.parts if p.catalog.drive and p.catalog.drive.kind == "pump"]
    if motors:
        stall = max(m.catalog.drive.stall_ma for m in motors)
        vmax = max(m.catalog.drive.vmax for m in motors)
        cands = []
        for d in catalog.with_capability("drive.dc_motor"):
            imax = float(d.specs.get("imax_per_channel_ma", 0))
            vm_min, vm_max = float(d.specs.get("vm_min", 0)), float(d.specs.get("vm_max", 99))
            if vm_min <= vmax <= vm_max + 0.01 and imax >= 0.8 * stall:
                cands.append(d)
        cands = cands or catalog.with_capability("drive.dc_motor")
        ranked = score_candidates(cands, Need("drive.dc_motor"), req, design.mcu.catalog if design.mcu else None)
        drv, s, reasons = ranked[0]
        channels = int(drv.specs.get("channels", "2"))
        for i in range(0, len(motors), channels):
            group = motors[i : i + channels]
            part = design.add(
                Part(
                    id="motor_driver",
                    catalog=drv,
                    role="driver.dc_motor",
                    name="Motor Driver" if len(motors) <= channels else f"Motor Driver {i // channels + 1}",
                    score=s,
                    reasons=reasons + [f"{drv.specs.get('imax_per_channel_ma')} mA/ch ≥ 0.8×stall {stall:.0f} mA"],
                    alternatives=[Alternative(a, sc, r) for a, sc, r in ranked[1:4]],
                    drives=[m.id for m in group],
                )
            )
            for m in group:
                m.driven_by = part.id
        if len(motors) == 2 and not catalog_has_part(design, "ball-caster") and "ball-caster" in catalog:
            design.add(Part(id="caster", catalog=catalog.get("ball-caster"), role="mech.caster", name="Ball Caster"))
    if len(servos) > 2 or (servos and design.mcu and len(servos) > _free_pwm_estimate(design)):
        pca = catalog.get("pca9685")
        part = design.add(
            Part(
                id="servo_driver",
                catalog=pca,
                role="driver.servo",
                name="Servo Driver",
                score=1.0,
                reasons=[f"{len(servos)} servos: offload PWM generation to a 16-channel I2C driver"],
                drives=[s.id for s in servos],
            )
        )
        for s in servos:
            s.driven_by = part.id
    for pump in pumps:
        mos = catalog.get("mosfet-module")
        part = design.add(
            Part(
                id=f"{pump.id}_switch",
                catalog=mos,
                role="driver.pump",
                name=f"{pump.name} Switch",
                reasons=["MCU pins cannot source pump current; logic-level MOSFET low-side switch"],
                drives=[pump.id],
            )
        )
        pump.driven_by = part.id


def _free_pwm_estimate(design: Design) -> int:
    mcu = design.mcu.catalog.mcu
    return sum(1 for p in mcu.pins if "pwm" in p.caps and p.name not in mcu.reserved) // 2


def catalog_has_part(design: Design, catalog_id: str) -> bool:
    return any(p.catalog.id == catalog_id for p in design.parts)


# ------------------------------------------------------------------------------ budget enforcement
def enforce_budget(design: Design) -> None:
    """Downgrade loop: while over budget, swap the slot with the largest saving-per-score-loss."""
    budget = design.requirements.budget_usd
    if budget is None:
        return
    for _ in range(20):
        total = sum(unit_price(p.catalog, p.quantity) * p.quantity for p in design.parts)
        if total <= budget:
            return
        best = None
        for p in design.parts:
            cur = unit_price(p.catalog, p.quantity)
            for alt in p.alternatives:
                saving = cur - unit_price(alt.part, p.quantity)
                if saving <= 0.01:
                    continue
                if not logic_compatible(alt.part, design.mcu.catalog if design.mcu else None):
                    continue
                ratio = saving / max(0.01, p.score - alt.score + 0.05)
                if best is None or ratio > best[0]:
                    best = (ratio, p, alt, saving)
        if best is None:
            break
        _, p, alt, saving = best
        old = p.catalog
        p.alternatives = [a for a in p.alternatives if a.part.id != alt.part.id] + [
            Alternative(old, p.score, ["previous choice"])
        ]
        p.catalog, p.score = alt.part, alt.score
        p.reasons = [f"downgraded from {old.name} to meet ${budget:.0f} budget (saves ${saving:.2f})"] + alt.reasons
    total = sum(unit_price(p.catalog, p.quantity) * p.quantity for p in design.parts)
    if total > budget:
        design.issue(
            "warning",
            "budget_exceeded",
            f"Electronics cost ≈ ${total:.2f} exceeds the ${budget:.2f} budget even after downgrades.",
            fix="Remove optional features or raise the budget.",
        )


# ------------------------------------------------------------------------------ public entry
def select_components(design: Design, catalog: Catalog) -> Design:
    req = design.requirements
    needs = plan_needs(req)
    provisional = select_peripherals(needs, req, catalog, None)
    mcu, s, reasons, alts = select_mcu(provisional, req, catalog)
    peripherals = select_peripherals(needs, req, catalog, mcu)  # re-score with MCU logic level known
    design.parts.append(
        Part(
            id="main_controller",
            catalog=mcu,
            role="mcu",
            name="Main Controller",
            score=s,
            reasons=reasons,
            alternatives=alts,
        )
    )
    for p in peripherals:
        design.add(p)
    covered = {p.capability for p in peripherals} | {c for p in peripherals for c in p.catalog.capabilities}
    for n in needs:
        if n.capability not in covered:
            design.issue(
                "warning", "unsatisfied_need", f"No catalog part provides '{n.capability}'.", fix="Extend the catalog."
            )
    if "sense.camera" in req.capabilities and "sense.camera" not in mcu.capabilities:
        cam = next((c for c in catalog.with_capability("sense.camera") if c.type == "camera"), None)
        if cam:
            design.add(
                Part(id="camera", catalog=cam, role="sense.camera", name="Camera", reasons=["CSI camera for the SBC"])
            )
    add_drivers(design, catalog)
    for p in design.parts:  # level shifting
        if not logic_compatible(p.catalog, mcu):
            design.issue(
                "warning",
                "logic_level",
                f"{p.name} uses {effective_logic_v(p.catalog):g} V logic but {mcu.name} is {mcu.mcu.logic_v:g} V"
                + (" and not 5 V tolerant." if mcu.mcu.logic_v < 5 else "."),
                [p.id],
                fix="A BSS138 level shifter is inserted in these signal lines by the wiring stage.",
            )
    enforce_budget(design)
    return design


# ------------------------------------------------------------------------------ component offering
def suggest(design: Design, catalog: Catalog, node_id: str | None = None, limit: int = 3) -> list[Suggestion]:
    """Ranked alternatives for one node (or all) + additions derived from open issues."""
    out: list[Suggestion] = []
    targets = [design.part(node_id)] if node_id else design.parts
    for p in targets:
        cur_price = unit_price(p.catalog, p.quantity)
        for alt in p.alternatives[:limit]:
            delta = round(unit_price(alt.part, p.quantity) - cur_price, 2)
            reasons = list(alt.reasons)
            if delta < 0:
                reasons.insert(0, f"${-delta:.2f} cheaper")
            elif delta > 0:
                reasons.insert(0, f"${delta:.2f} more expensive")
            out.append(
                Suggestion(kind="alternative", node_id=p.id, catalog_id=alt.part.id, name=alt.part.name, score=alt.score,
                           price_delta_usd=delta, reasons=reasons)
            )  # fmt: skip
    if node_id is None:
        out += _addition_suggestions(design, catalog)
    return out


def _addition_suggestions(design: Design, catalog: Catalog) -> list[Suggestion]:
    out = []
    have = {p.catalog.id for p in design.parts}
    req = design.requirements
    if req.power_source != "wall" and "slide-switch" not in have and "slide-switch" in catalog:
        out.append(Suggestion(kind="addition", catalog_id="slide-switch", name="Power switch", score=0.9,
                              price_delta_usd=round(unit_price(catalog.get("slide-switch")), 2),
                              reasons=["battery designs need a hard power-off to avoid deep discharge"]))  # fmt: skip
    if req.power_source != "wall" and "ina219" not in have and "ina219" in catalog:
        out.append(Suggestion(kind="addition", catalog_id="ina219", name="Battery/current monitor", score=0.5,
                              price_delta_usd=round(unit_price(catalog.get("ina219")), 2),
                              reasons=["show battery percentage / detect stalls"]))  # fmt: skip
    i2c_addrs: dict[str, list[str]] = {}
    for p in design.parts:
        for itf in p.catalog.interfaces:
            if itf.protocol == "i2c" and itf.address:
                i2c_addrs.setdefault(itf.address.lower(), []).append(p.id)
    if any(len(v) > 1 for v in i2c_addrs.values()) and "tca9548a" not in have and "tca9548a" in catalog:
        out.append(Suggestion(kind="addition", catalog_id="tca9548a", name="I2C multiplexer", score=0.8,
                              price_delta_usd=round(unit_price(catalog.get("tca9548a")), 2),
                              reasons=["two devices share an I2C address and no alternate address is free"]))  # fmt: skip
    return out
