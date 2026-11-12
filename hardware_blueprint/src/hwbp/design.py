"""In-memory design state shared by all pipeline stages (selection -> power -> wiring -> mech -> ...)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hwbp.models import CatalogPart, ElectricalConnection, Issue, MechanicalConnection, Suggestion
from hwbp.requirements import Requirements


@dataclass
class Alternative:
    part: CatalogPart
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Part:
    """An instantiated component (becomes a document ``Node``)."""

    id: str
    catalog: CatalogPart
    role: str
    name: str
    quantity: int = 1
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    alternatives: list[Alternative] = field(default_factory=list)
    capability: str | None = None
    rail: str | None = None  # supply rail this part draws from
    supply_pin: str | None = None
    drives: list[str] = field(default_factory=list)  # for drivers: ids of loads
    driven_by: str | None = None  # for loads: driver id
    # mechanical extensions
    dims_override: tuple[float, float, float] | None = None
    material: str | None = None
    print_settings: str | None = None
    mounts_for: str | None = None
    description: str | None = None
    est_cost_override: float | None = None
    subtype_override: str | None = None


@dataclass
class Rail:
    name: str  # "5V", "3.3V", "VBAT", "12V"
    voltage: float
    source_id: str  # part id of the regulator/battery/adapter feeding it
    source_pin: str
    capacity_ma: float
    loads: list[str] = field(default_factory=list)
    typ_ma: float = 0.0
    peak_ma: float = 0.0
    avg_ma: float = 0.0  # duty-cycled average (energy budget)
    parent: str | None = None  # name of the rail feeding this rail's regulator (None = primary source)


@dataclass
class Design:
    requirements: Requirements
    parts: list[Part] = field(default_factory=list)
    rails: list[Rail] = field(default_factory=list)
    connections: list[ElectricalConnection] = field(default_factory=list)
    mech_connections: list[MechanicalConnection] = field(default_factory=list)
    pin_map: dict[str, dict[str, str]] = field(default_factory=dict)  # node -> {part pin: mcu pin}
    buses: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    power_summary: dict = field(default_factory=dict)
    mech_layout: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ access helpers
    def part(self, part_id: str) -> Part:
        for p in self.parts:
            if p.id == part_id:
                return p
        raise KeyError(part_id)

    def has(self, part_id: str) -> bool:
        return any(p.id == part_id for p in self.parts)

    @property
    def mcu(self) -> Part | None:
        return next((p for p in self.parts if p.catalog.type == "mcu"), None)

    def by_role(self, prefix: str) -> list[Part]:
        return [p for p in self.parts if p.role == prefix or p.role.startswith(prefix + "#")]

    def unique_id(self, base: str) -> str:
        base = slug(base)
        if not self.has(base):
            return base
        i = 2
        while self.has(f"{base}_{i}"):
            i += 1
        return f"{base}_{i}"

    def add(self, part: Part) -> Part:
        part.id = self.unique_id(part.id)
        self.parts.append(part)
        return part

    def remove(self, part_id: str) -> None:
        self.parts = [p for p in self.parts if p.id != part_id]

    def issue(self, severity: str, code: str, message: str, node_ids=(), fix: str | None = None) -> None:
        if any(i.code == code and i.node_ids == list(node_ids) for i in self.issues):
            return
        self.issues.append(Issue(severity=severity, code=code, message=message, node_ids=list(node_ids), fix=fix))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "part"
