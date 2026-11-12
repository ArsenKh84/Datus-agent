"""Assembly guide generation.

Phases are the fixed, ordered set used by the reference product: fabricate -> wire -> bringup -> assemble.
Sub-steps are derived from the design graph (print groups, power chain, buses, drivers, assembly tree),
not free text, so every step references real node ids and real pin numbers.
"""

from __future__ import annotations

import re

from hwbp.design import Design
from hwbp.models import InstructionPreamble, InstructionStep, SubStep, canonical_type

PHASES = [("fabricate", "Fabricate"), ("wire", "Wire"), ("bringup", "Bring-up"), ("assemble", "Assemble")]

# keyword -> tools (matched against sub-step titles), plus per-phase defaults (reference behaviour)
TOOL_RULES = [
    (r"print", ["3D printer"]),
    (r"insert", ["Soldering iron with heat-set tip"]),
    (r"solder|wire|power path|rail|bus", ["Soldering iron", "Solder wire", "Wire strippers"]),
    (r"verify|continuity|voltage|test", ["Digital multimeter"]),
    (r"flash|firmware", ["USB-C data cable", "Computer with PlatformIO"]),
    (r"mount|fasten|close|secure|install", ["Screwdriver set (M2.5 / M3)"]),
    (r"shrink|insulat", ["Heat gun"]),
]
PHASE_DEFAULT_TOOLS = {
    "fabricate": ["Calipers"],
    "wire": ["Flux"],
    "bringup": ["Digital multimeter"],
    "assemble": ["Tweezers"],
}


def tools_for(title: str, phase: str) -> list[str]:
    tools: list[str] = []
    for pat, ts in TOOL_RULES:
        if re.search(pat, title, re.I):
            tools += [t for t in ts if t not in tools]
    return tools or list(PHASE_DEFAULT_TOOLS.get(phase, []))


def build_instructions(design: Design) -> tuple[InstructionPreamble, list[InstructionStep]]:
    steps = {pid: [] for pid, _ in PHASES}

    def add(phase: str, title: str, part_ids: list[str], details: list[str] | None = None) -> None:
        sid = f"{phase}_{len(steps[phase]) + 1}"
        steps[phase].append(SubStep(id=sid, title=title, part_ids=[p for p in part_ids if design.has(p)], details=details or None,
                                    tools=tools_for(title, phase)))  # fmt: skip

    parts = {p.id: p for p in design.parts}
    # ---- fabricate
    printed = [p for p in design.parts if canonical_type(p.catalog.type) in ("3d_printed", "enclosure")]
    by_mat: dict[str, list[str]] = {}
    for p in printed:
        by_mat.setdefault(p.material or "PLA", []).append(p.id)
    for mat, ids in by_mat.items():
        details = [f"{parts[i].name}: {parts[i].print_settings or 'default profile'}" for i in ids]
        add("fabricate", f"3D print {len(ids)} part(s) in {mat}", ids, details)
    structural = [
        p.id
        for p in design.parts
        if canonical_type(p.catalog.type) == "structural" and p.catalog.id.startswith("custom-")
    ]
    if structural:
        add("fabricate", "Cut / machine the structural plate and deburr edges", structural)
    if any(p.catalog.id == "m3-heat-inserts" for p in design.parts):
        add("fabricate", "Install heat-set inserts into printed bosses", [p.id for p in printed])
    boards = [
        p.id
        for p in design.parts
        if p.catalog.category == "electrical"
        and p.catalog.dims_mm
        and p.catalog.dims_mm[0] * p.catalog.dims_mm[2] >= 300
    ]
    if boards and printed:
        add(
            "fabricate",
            "Test-fit boards on standoffs before wiring",
            boards + [p.id for p in design.parts if p.id in ("pcb_standoffs",)],
        )
    # ---- wire: power chain first (safety), then buses and GPIO
    power_chain = [p.id for p in design.parts if p.catalog.type in ("battery", "power")]
    if power_chain:
        chain_desc = [f"{c.source} {c.source_pin or ''} → {c.target} {c.target_pin or ''} ({c.voltage or ''})".strip()
                      for c in design.connections if c.type == "power" and c.source in power_chain and c.target in power_chain]  # fmt: skip
        add("wire", "Assemble the power path (battery, charger, regulators)", power_chain, chain_desc)
    for rail in design.rails:
        loads = [i for i in rail.loads if i in parts]
        if loads:
            add("wire", f"Distribute the {rail.name} rail to {len(loads)} load(s)", [rail.source_id] + loads,
                [f"{rail.source_id}.{rail.source_pin} → {i}.{parts[i].supply_pin or 'VCC'}" for i in loads])  # fmt: skip
    mcu = design.mcu
    for bus in design.buses:
        proto = bus["protocol"].upper()
        devs = bus["devices"]
        detail = [f"{role.upper()} = {pin}" for role, pin in bus["pins"].items()]
        for d in devs:
            detail += [f"{d}.{pp} ↔ {mp}" for pp, mp in design.pin_map.get(d, {}).items()]
        add(
            "wire",
            f"Solder {proto} bus lines to {', '.join(parts[d].name for d in devs if d in parts)}",
            [mcu.id if mcu else ""] + devs,
            detail,
        )
    bus_devs = {d for b in design.buses for d in b["devices"]}
    for pid, mapping in design.pin_map.items():
        if pid in bus_devs or pid not in parts:
            continue
        add(
            "wire",
            f"Wire {parts[pid].name} signal pins",
            [mcu.id if mcu else "", pid],
            [f"{pid}.{pp} → {mp}" for pp, mp in mapping.items()],
        )
    for drv in [p for p in design.parts if p.drives]:
        add("wire", f"Connect {drv.name} outputs to {len(drv.drives)} load(s)", [drv.id] + drv.drives)
    shifters = [p.id for p in design.parts if p.role == "logic.level_shift"]
    if shifters:
        add("wire", "Insert level shifters (LV = 3.3 V side, HV = 5 V side)", shifters)
    add("wire", "Verify continuity and rail voltages before inserting the MCU", [r.source_id for r in design.rails] + ([mcu.id] if mcu else []),
        [f"{r.name}: expect {r.voltage:g} V ±5 % at {r.source_id}.{r.source_pin}" for r in design.rails])  # fmt: skip
    # ---- bring-up
    if mcu:
        add(
            "bringup",
            "Flash firmware to the controller",
            [mcu.id],
            ["pio run -t upload", "pio device monitor -b 115200"],
        )
    for p in design.parts:
        if p.id in design.pin_map:
            add("bringup", f"Test {p.name}", [mcu.id if mcu else "", p.id])
    if any(p.catalog.drive for p in design.parts):
        add(
            "bringup",
            "Run actuators at low duty first (soft-start) and check driver temperature",
            [p.id for p in design.parts if p.catalog.drive or p.drives],
        )
    # ---- assemble (tree order: boards, panel, external, close)
    internal = [c.source for c in design.mech_connections if c.label in ("M2.5 standoffs", "adhesive pad")]
    if internal:
        add(
            "assemble",
            "Mount boards and modules inside the enclosure",
            internal + ["enclosure_base", "pcb_standoffs", "fastener_kit"],
        )
    panel = [c.source for c in design.mech_connections if c.label == "panel mount"]
    if panel:
        add("assemble", "Install panel parts (display/controls) into the lid", panel + ["enclosure_lid"])
    external = [c.source for c in design.mech_connections if c.label == "bracket"]
    if external:
        add("assemble", "Mount external actuators/sensors and route cables", external)
    if design.has("chassis_plate"):
        add("assemble", "Fasten the enclosure onto the chassis plate", ["enclosure_base", "chassis_plate"])
    add("assemble", "Close the enclosure and fasten the lid", ["enclosure_lid", "enclosure_base", "fastener_kit"])
    out = [InstructionStep(id=pid, title=title, sub_steps=steps[pid]) for pid, title in PHASES]
    # preamble
    all_tools: list[str] = []
    for st in out:
        for sub in st.sub_steps:
            all_tools += [t for t in (sub.tools or []) if t not in all_tools]
    assumptions = ["Slicer (PrusaSlicer/Cura/Bambu Studio) configured for the listed materials",
                   "PlatformIO (VS Code extension or CLI) installed"]  # fmt: skip
    if any(p.catalog.battery and p.catalog.battery.chemistry.startswith("li") for p in design.parts):
        assumptions.append("Lithium battery safety: never short, puncture or charge unattended")
    if any("actuate.switch_mains" in p.catalog.capabilities for p in design.parts):
        assumptions.append("Mains wiring only by a qualified person; keep low-voltage side isolated")
    return InstructionPreamble(tools=all_tools, assumptions=assumptions), out
