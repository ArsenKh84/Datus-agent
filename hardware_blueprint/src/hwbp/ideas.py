"""Deterministic content helpers that replace the reference's text-generation actions.

* ``generate_ideas``        — "NEED AN IDEA?" wizard: answers {interests, budget, skill, power} -> project ideas
* ``suggested_prompts``     — edit / ask prompt chips derived from open issues, alternatives and missing features
* ``substep_detail``        — "View details" for an instruction sub-step: {summary, steps[], tip}
* ``refresh_part_descriptions`` — one-line descriptions from catalog data (role + key specs)
* ``topology_hash``         — cache key used to invalidate cached prompts when the design changes
"""

from __future__ import annotations

import hashlib

from hwbp.catalog import Catalog
from hwbp.models import ProjectContent, SubStep

IDEA_BANK = [
    {"name": "Smart Plant Monitor", "tags": {"home", "garden", "iot", "beginner"}, "budget": 20, "skill": 1,
     "prompt": "Smart plant monitor with soil moisture, OLED display and wifi alerts, USB powered"},
    {"name": "Desk Weather Station", "tags": {"home", "iot", "data", "beginner"}, "budget": 35, "skill": 1,
     "prompt": "Weather station with temperature, humidity, pressure, CO2 and e-paper display, wall powered"},
    {"name": "Obstacle-Avoiding Robot Car", "tags": {"robotics", "kids", "motion"}, "budget": 45, "skill": 2,
     "prompt": "2WD obstacle-avoiding robot car with ultrasonic sensor, LED lights and bluetooth control"},
    {"name": "4-Axis Robotic Arm", "tags": {"robotics", "motion", "maker"}, "budget": 40, "skill": 2,
     "prompt": "4-axis robotic arm with a joystick, wall powered"},
    {"name": "Wearable Heart-Rate Band", "tags": {"health", "wearable"}, "budget": 30, "skill": 2,
     "prompt": "Small wearable heart rate monitor with OLED and bluetooth, 24 hours battery"},
    {"name": "Presence-Aware Desk Lamp", "tags": {"home", "lighting", "iot"}, "budget": 25, "skill": 1,
     "prompt": "Smart desk lamp with LED strip, mmWave presence radar, rotary knob and wifi, USB powered"},
    {"name": "LoRa Garden Sensor", "tags": {"garden", "outdoor", "iot", "long-range"}, "budget": 40, "skill": 3,
     "prompt": "Solar powered garden soil moisture sensor with LoRa, months of battery life"},
    {"name": "Leak Alarm", "tags": {"home", "safety", "beginner"}, "budget": 15, "skill": 1,
     "prompt": "Water leak detector with buzzer alarm and wifi notification, battery powered"},
    {"name": "Pan-Tilt Security Camera", "tags": {"security", "camera", "iot"}, "budget": 50, "skill": 3,
     "prompt": "Security camera with motion sensor, pan tilt servos and buzzer alarm, wall powered"},
    {"name": "GPS Bike Tracker", "tags": {"outdoor", "tracking", "mobility"}, "budget": 45, "skill": 2,
     "prompt": "Portable GPS bike tracker with cellular SMS alerts and IMU theft detection, battery powered"},
]  # fmt: skip


def generate_ideas(answers: dict, limit: int = 4) -> dict:
    """Rank the idea bank by overlap with interests, budget fit and skill fit (deterministic)."""
    interests = {str(a).lower() for a in (answers.get("interests") or [])}
    budget = float(answers.get("budget") or 1e9)
    skill = int(answers.get("skill") or 2)
    power = answers.get("power")
    scored = []
    for idea in IDEA_BANK:
        score = 2.0 * len(interests & idea["tags"])
        score += 1.0 if idea["budget"] <= budget else -1.0
        score -= 0.5 * max(0, idea["skill"] - skill)
        if power and power in idea["prompt"]:
            score += 0.5
        scored.append((score, idea["name"], idea))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return {
        "projects": [
            {"name": i["name"], "description": i["prompt"], "estBudgetUsd": i["budget"]} for _, _, i in scored[:limit]
        ]
    }


def topology_hash(content: ProjectContent) -> str:
    key = "|".join(sorted(f"{n.id}:{n.catalog_id or n.product_name or n.name}" for n in content.nodes))
    key += "|" + "|".join(sorted(f"{c.source}>{c.target}:{c.type}" for c in content.electrical_connections))
    return "v1::" + hashlib.sha256(key.encode()).hexdigest()[:16]


def suggested_prompts(content: ProjectContent, mode: str = "unified", limit: int = 6) -> dict:
    """Prompt chips: fixes for open issues, cheaper alternatives, common feature additions, and questions."""
    edit: list[str] = []
    for i in content.issues:
        if i.severity in ("error", "warning") and i.node_ids:
            node = content.node(i.node_ids[0])
            name = node.name if node else i.node_ids[0]
            if i.code == "rail_overcurrent":
                edit.append(f"Make the power supply handle more current for {name}")
            elif i.code == "battery_capacity":
                edit.append("Use a bigger battery")
            elif i.code == "logic_level":
                edit.append(f"Replace the {name.lower()} with a 3.3 V part")
    for s in content.suggestions:
        if s.kind == "alternative" and (s.price_delta_usd or 0) < -0.5 and s.node_id:
            node = content.node(s.node_id)
            if node:
                edit.append(f"Replace the {node.name.lower()} with a {s.name}")
        elif s.kind == "addition":
            edit.append(f"Add a {s.name.lower()}")
    caps = set((content.requirements or {}).get("capabilities") or [])
    for cap, text in (("comm.wifi", "Add wifi connectivity"), ("ui.display", "Add a small display"),
                      ("output.buzzer", "Add a buzzer for alerts"), ("input.button", "Add a power button")):  # fmt: skip
        if cap not in caps:
            edit.append(text)
    ask = ["How long will the battery last?" if (content.power or {}).get("runtimeHours") else "How much power does it draw?",
           "What is the most expensive part?", "Which parts need soldering?", "Why was this microcontroller chosen?"]  # fmt: skip
    edit = list(dict.fromkeys(edit))[:limit]
    out = {"edit": edit, "ask": ask[:limit], "hash": topology_hash(content)}
    if mode == "edit":
        out["prompts"] = edit
    elif mode == "ask":
        out["prompts"] = out["ask"]
    else:
        out["prompts"] = (edit[: limit // 2 + 1] + ask)[:limit]
    return out


def substep_detail(content: ProjectContent, step_id: str, substep_id: str) -> dict:
    """{summary, steps[], tip} for an instruction sub-step, built from the parts, pins and connections it references."""
    sub: SubStep | None = None
    for st in content.instruction_steps:
        if st.id == step_id:
            sub = next((s for s in st.sub_steps if s.id == substep_id), None)
    if sub is None:
        raise KeyError(f"unknown sub-step {step_id}/{substep_id}")
    parts = [content.node(p) for p in sub.part_ids if content.node(p)]
    names = ", ".join(p.name for p in parts)
    ids = set(sub.part_ids)
    steps = list(sub.details or [])
    for c in content.electrical_connections:
        if c.source in ids and c.target in ids and c.type in ("data", "power") and len(steps) < 12:
            pins = f" ({c.source_pin} → {c.target_pin})" if c.source_pin and c.target_pin else ""
            what = c.protocol or c.voltage or c.type
            steps.append(f"Connect {c.source} to {c.target}{pins} — {what}")
    for p in parts:
        if p.print_settings and step_id == "fabricate":
            steps.append(f"Slice {p.name} in {p.material or 'PLA'} with {p.print_settings}")
    tip = {
        "fabricate": "Print a small test piece first to check tolerances for screw holes and snap fits.",
        "wire": "Pre-tin pads and wire ends; use colour-coded wire (red = power, black = GND) and heat-shrink every joint.",
        "bringup": "Power the board from USB before connecting the battery, and watch for parts getting warm.",
        "assemble": "Route cables before closing the enclosure and leave service loops so boards can be lifted out.",
    }.get(step_id, "Double-check orientation before fastening.")
    return {"summary": f"{sub.title}: {names}." if names else sub.title, "steps": steps or [sub.title], "tip": tip}


def refresh_part_descriptions(content: ProjectContent, catalog: Catalog) -> list[dict]:
    """[{id, description}] — role + key specs from the catalog (only for nodes resolvable in the catalog)."""
    out = []
    for n in content.nodes:
        if not n.catalog_id or n.catalog_id not in catalog:
            continue
        p = catalog.get(n.catalog_id)
        bits = []
        if p.mcu:
            bits.append(f"{p.mcu.cpu_mhz} MHz, {', '.join(p.mcu.radios) or 'no radio'}")
        if p.interfaces:
            bits.append("/".join(sorted({i.protocol.upper() for i in p.interfaces})) + " interface")
        if p.power_inputs:
            pi = p.power_inputs[0]
            bits.append(f"{pi.vmin:g}–{pi.vmax:g} V supply")
        if p.power_output and p.power_output.vout:
            bits.append(f"{p.power_output.kind} to {p.power_output.vout:g} V, {p.power_output.imax_ma:g} mA")
        if p.battery:
            bits.append(f"{p.battery.capacity_mah:g} mAh {p.battery.chemistry}")
        desc = f"{p.name} — {(n.role or p.type).split('#')[0]}" + (f"; {', '.join(bits)}" if bits else "") + "."
        out.append({"id": n.id, "description": desc})
    return out


def apply_descriptions(content: ProjectContent, descriptions: list[dict]) -> int:
    by_id = {d["id"]: d["description"] for d in descriptions}
    n = 0
    for node in content.nodes:
        if node.id in by_id:
            node.description = by_id[node.id]
            n += 1
    return n
