"""Data models.

Two families:

* **Project document** (``ProjectContent`` and friends) — the wire format. Field names serialise to
  camelCase and are a superset of the document observed on blueprint.io (``nodes``,
  ``electricalConnections``, ``mechanicalConnections``, ``instructionSteps`` ...), so a document
  exported from there loads unchanged (``extra="allow"`` keeps unknown fields round-tripping).
* **Catalog** (``CatalogPart``) — the engine's internal component knowledge: electrical ratings,
  interfaces, MCU pin capabilities, offers with price breaks, firmware hints.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ---------------------------------------------------------------------------- enums (open sets)
CATEGORY_ELECTRICAL = "electrical"
CATEGORY_MECHANICAL = "mechanical"

# Canonical document types — identical to the reference product's 12 types (interoperable documents).
ELECTRICAL_TYPES = ("mcu", "sensor", "actuator", "power", "module", "display")
MECHANICAL_TYPES = ("structural", "enclosure", "mechanism", "misc", "3d_printed", "other")
DOC_TYPES = ELECTRICAL_TYPES + MECHANICAL_TYPES

# label, dark colour, sort key  (other/unknown sorts last)
TYPE_META = {
    "mcu": ("MCU", "#06abab", 0),
    "sensor": ("Sensor", "#418dbf", 1),
    "actuator": ("Actuator", "#df663b", 2),
    "power": ("Power", "#f5c94e", 3),
    "module": ("Module", "#7253ad", 4),
    "display": ("Display", "#db87a1", 5),
    "structural": ("Structural", "#6fb2fd", 10),
    "enclosure": ("Enclosure", "#a58be0", 11),
    "mechanism": ("Mechanism", "#e8a25b", 12),
    "misc": ("Misc", "#b7a26b", 13),
    "3d_printed": ("3D Print", "#bfd36a", 14),
    "other": ("Other", "#9d9f9e", 20),
}
TYPE_LABELS = {k: v[0] for k, v in TYPE_META.items()}

# Engine catalog subtypes (finer-grained) -> canonical document type.
SUBTYPE_TO_TYPE = {
    "battery": "power",
    "driver": "module",
    "communication": "module",
    "audio": "module",
    "input": "module",
    "lighting": "actuator",
    "camera": "sensor",
    "passive": "module",
    "connector": "other",
    "fastener": "misc",
    "bearing": "mechanism",
    "wheel": "mechanism",
    "gear": "mechanism",
}


def canonical_type(t: str | None) -> str:
    """Map any engine subtype / legacy alias to one of the 12 document types (unknown -> 'other')."""
    t = (t or "other").lower()
    t = SUBTYPE_TO_TYPE.get(t, t)
    return t if t in DOC_TYPES else "other"


def type_sort_key(t: str) -> int:
    return TYPE_META.get(t, ("", "", 99))[2]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


# ---------------------------------------------------------------------------- sourcing
class PriceBreak(CamelModel):
    qty: int
    unit_price: float


class Offer(CamelModel):
    """A purchasable listing. Compatible with blueprint.io ``research.sourcing[]`` ({url, price, vendor})."""

    vendor: str
    url: str | None = None
    price: str | None = None  # raw display string, e.g. "$5.5961"
    currency: str = "USD"
    price_breaks: list[PriceBreak] = Field(default_factory=list)
    moq: int | None = None
    pack_size: int | None = None
    stock: int | None = None
    sku: str | None = None


class Spec(CamelModel):
    label: str
    value: str


class DocLink(CamelModel):
    url: str
    title: str


class Research(CamelModel):
    specs: list[Spec] = Field(default_factory=list)
    sourcing: list[Offer] = Field(default_factory=list)
    datasheet_url: str | None = None
    documentation_urls: list[DocLink] = Field(default_factory=list)


# ---------------------------------------------------------------------------- project document
class Node(CamelModel):
    id: str
    name: str
    type: str
    category: str = CATEGORY_ELECTRICAL
    part_id: str | None = None
    product_name: str | None = None
    quantity: int = 1
    estimated_cost: float | None = None
    dimensions: str | None = None
    pins: list[str] | None = None
    description: str | None = None
    material: str | None = None
    print_settings: str | None = None
    mounts_for: str | None = None
    purchase_url: str | None = None
    ebay_url: str | None = None
    aliexpress_url: str | None = None
    image_url: str | None = None
    research: Research | None = None
    # engine extensions (not present in the reference format)
    subtype: str | None = None  # fine-grained engine type, e.g. battery/driver/communication
    catalog_id: str | None = None
    role: str | None = None
    rationale: list[str] | None = None

    def all_offers(self) -> list[Offer]:
        return list(self.research.sourcing) if self.research else []


class Vec3(CamelModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class ElectricalConnection(CamelModel):
    type: Literal["power", "data", "ground"] | str
    source: str
    target: str
    voltage: str | None = None
    current: str | None = None
    protocol: str | None = None
    source_pin: str | None = None
    target_pin: str | None = None
    net: str | None = None  # engine extension: net name, e.g. "5V", "GND", "I2C0_SDA"


class MechanicalConnection(CamelModel):
    source: str
    target: str
    label: str = "attached"
    delta: Vec3 | None = Field(default_factory=Vec3)  # None => implicit placement (mountsFor / top / beside)


class SubStep(CamelModel):
    id: str
    title: str
    part_ids: list[str] = Field(default_factory=list)
    details: list[str] | None = None
    tools: list[str] | None = None


class InstructionStep(CamelModel):
    id: str
    title: str | None = None
    sub_steps: list[SubStep] = Field(default_factory=list)


class InstructionPreamble(CamelModel):
    tools: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class ImagePromptSnapshot(CamelModel):
    tags: list[str] = Field(default_factory=list)
    design_hash: str | None = None
    description: str | None = None
    technical_description: str | None = None


class Issue(CamelModel):
    """Validation finding (ERC/mechanical/BOM)."""

    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    node_ids: list[str] = Field(default_factory=list)
    fix: str | None = None


class Suggestion(CamelModel):
    """A component offering: an alternative or an addition, with a ranked score and reasons."""

    kind: Literal["alternative", "addition", "removal"]
    node_id: str | None = None
    catalog_id: str
    name: str
    score: float
    price_delta_usd: float | None = None
    reasons: list[str] = Field(default_factory=list)


class ProjectContent(CamelModel):
    project_name: str = "Untitled"
    original_prompt: str = ""
    project_description: str | None = None
    plan: str | None = None
    notes: list[str] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    electrical_connections: list[ElectricalConnection] = Field(default_factory=list)
    mechanical_connections: list[MechanicalConnection] = Field(default_factory=list)
    instruction_preamble: InstructionPreamble | None = None
    instruction_steps: list[InstructionStep] = Field(default_factory=list)
    image_prompt_snapshot: ImagePromptSnapshot | None = None
    project_image_url: str | None = None
    project_image_hash: str | None = None
    # engine extensions
    engine_version: str | None = None
    requirements: dict[str, Any] | None = None
    issues: list[Issue] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    bom: dict[str, Any] | None = None
    wiring_layout: dict[str, Any] | None = None
    mech_layout: dict[str, Any] | None = None
    firmware: dict[str, str] | None = None
    power: dict[str, Any] | None = None
    cad: dict[str, Any] | None = None  # per-node CAD summaries (print cost, volume)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)


class Project(CamelModel):
    id: str
    revision: int = 1
    owner_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    content: ProjectContent = Field(default_factory=ProjectContent)


# ---------------------------------------------------------------------------- catalog
class PowerInput(BaseModel):
    pin: str
    vmin: float
    vmax: float


class PowerOutput(BaseModel):
    """A regulator/charger/source output. ``kind``: ldo | buck | boost | buck_boost | charger | adapter | protection."""

    kind: str
    pin_in: str = "VIN"
    pin_out: str = "VOUT"
    vin_min: float
    vin_max: float
    vout: float | None = None  # None => pass-through (protection board, charger output follows battery)
    imax_ma: float
    efficiency: float = 0.9
    quiescent_ma: float = 0.1


class BatterySpec(BaseModel):
    chemistry: str
    cells: int = 1
    nominal_v: float
    vmax: float
    vmin: float
    capacity_mah: float
    max_discharge_ma: float
    needs_protection: bool = False
    needs_charger: bool = False


class Interface(BaseModel):
    """Data interface exposed by a peripheral.

    ``pins`` maps the part's pin name to a bus signal role: i2c {sda, scl}; spi {mosi, miso, sck, cs};
    uart {tx, rx} (roles are from the PART's perspective); single-signal protocols use role "sig"
    with protocol gpio/pwm/adc/i2s/onewire. ``extra`` maps control pins to the MCU capability they need.
    """

    protocol: str
    pins: dict[str, str]
    address: str | None = None
    alt_addresses: list[str] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)
    max_clock_hz: int | None = None


class McuPin(BaseModel):
    name: str
    caps: list[str]  # gpio, adc, pwm, dac, touch, i2s ...


class McuSpec(BaseModel):
    pins: list[McuPin]
    buses: dict[str, list[dict[str, str]]]  # protocol -> list of bus instances {role: pin}
    reserved: list[str] = Field(default_factory=list)
    logic_v: float = 3.3
    radios: list[str] = Field(default_factory=list)
    cpu_mhz: int = 0
    ram_kb: int = 0
    flash_kb: int = 0
    pio_board: str | None = None
    pio_platform: str | None = None
    framework: str = "arduino"
    supply_pins: dict[str, str] = Field(default_factory=dict)  # rail voltage -> pin name, e.g. {"5": "5V"}


class Drive(BaseModel):
    """Electromechanical load (motor/servo/pump) characteristics."""

    kind: str  # dc_motor | servo | stepper | pump | solenoid | relay
    vmin: float
    vmax: float
    run_ma: float
    stall_ma: float
    phases: int = 1


class FirmwareHint(BaseModel):
    lib: str | None = None
    include: str | None = None
    globals: str | None = None
    init: str | None = None
    loop: str | None = None


class CatalogPart(BaseModel):
    id: str
    type: str
    category: str = CATEGORY_ELECTRICAL
    name: str
    manufacturer: str | None = None
    mpn: str | None = None
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    power_inputs: list[PowerInput] = Field(default_factory=list)
    power_output: PowerOutput | None = None
    battery: BatterySpec | None = None
    interfaces: list[Interface] = Field(default_factory=list)
    mcu: McuSpec | None = None
    drive: Drive | None = None
    current_ma: dict[str, float] = Field(default_factory=dict)  # typ, peak, sleep
    logic_v: float | None = None
    io_5v_tolerant: bool = False
    dims_mm: tuple[float, float, float] | None = None  # w (x), h (y, height), d (z)
    material: str | None = None
    offers: list[Offer] = Field(default_factory=list)
    est_price_usd: float | None = None
    datasheet: str | None = None
    popularity: float = 0.5  # 0..1 ecosystem/tutorial availability
    firmware: FirmwareHint | None = None
    tags: list[str] = Field(default_factory=list)
    specs: dict[str, str] = Field(default_factory=dict)
    pack_qty: int | None = None  # fasteners etc. sold in packs

    def all_pins(self) -> list[str]:
        """Ordered, de-duplicated pin list for diagram ports."""
        names: list[str] = []
        if self.mcu:
            names += [p.name for p in self.mcu.pins]
            names += list(self.mcu.supply_pins.values())
        for itf in self.interfaces:
            names += list(itf.pins) + list(itf.extra)
        if self.power_output:
            names += [self.power_output.pin_in, self.power_output.pin_out]
        for p in self.power_inputs:
            names.append(p.pin)
        if self.battery:
            names += ["B+", "B-"]
        if self.drive:
            names += ["IN1", "IN2"] if self.drive.kind == "dc_motor" else []
        if self.category == CATEGORY_ELECTRICAL and (names or self.type != "passive"):
            names.append("GND")
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]


def project_schema() -> dict:
    """JSON Schema (draft 2020-12) of the project document wire format (camelCase)."""
    schema = ProjectContent.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://hwbp.local/schema/project.schema.json"
    schema["description"] = (
        "hwbp project document; a superset of the blueprint.io project format (unknown fields are preserved)."
    )
    return schema
