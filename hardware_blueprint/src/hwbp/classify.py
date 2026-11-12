"""Classification: chat-message intent (edit vs ask) and part type/category from free text.

* ``classify_message`` mirrors the reference ``/chat/classify`` contract ``{mode: "edit"|"ask"}`` and adds a
  structured ``intent`` + ``targets`` so edits can be applied deterministically.
* ``classify_part`` maps an arbitrary part name/description (e.g. one proposed by an LLM or pasted by a user)
  onto the 12 canonical document types + a finer engine subtype.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hwbp.models import CATEGORY_ELECTRICAL, CATEGORY_MECHANICAL, ELECTRICAL_TYPES, ProjectContent

# ordered: first match wins (specific before generic)
PART_RULES: list[tuple[str, str, str]] = [
    # (regex, canonical type, subtype)
    (r"\b(screw|bolt|nut|washer|standoff|spacer|insert|rivet|zip ?tie|tape|glue|epoxy|adhesive)\b", "misc", "fastener"),
    (r"\b(3d[- ]?print(ed)?|printed)\b", "3d_printed", "3d_printed"),
    (r"\b(enclosure|case|housing|shell)\b", "enclosure", "enclosure"),
    (r"\b(gear|wheel|bearing|pulley|shaft|axle|hinge|spring|caster|track)\b", "mechanism", "mechanism"),
    (r"\b(bracket|plate|chassis|frame|extrusion|panel|beam)\b", "structural", "structural"),
    (
        r"\b(esp32|esp8266|arduino|atmega|stm32|rp2040|rp2350|pico|raspberry pi|microcontroller|mcu|nrf52|teensy|attiny)\b",
        "mcu",
        "mcu",
    ),
    (r"\b(oled|lcd|tft|e-?paper|e-?ink|display|screen|monitor|led matrix|7-segment)\b", "display", "display"),
    (r"\b(battery|lipo|li-ion|18650|cell|aa|aaa)\b", "power", "battery"),
    (
        r"\b(regulator|buck|boost|ldo|converter|charger|bms|protection|power supply|adapter|solar|tp4056|mt3608)\b",
        "power",
        "power",
    ),
    (r"\b(motor driver|h-bridge|servo driver|pca9685|drv8833|tb6612|l298|a4988|uln2003|mosfet)\b", "module", "driver"),
    (r"\b(servo|motor|stepper|pump|solenoid|relay|actuator|vibration)\b", "actuator", "actuator"),
    (r"\b(led strip|neopixel|ws2812|led)\b", "actuator", "lighting"),
    (r"\b(lora|wifi|bluetooth|ble|nrf24|gsm|lte|transceiver|radio|antenna|rfid|nfc)\b", "module", "communication"),
    (r"\b(speaker|amplifier|amp|buzzer|audio)\b", "module", "audio"),
    (r"\b(button|switch|encoder|joystick|keypad|potentiometer|knob)\b", "module", "input"),
    (r"\b(camera|ov2640|ov5640|imx\d+)\b", "sensor", "camera"),
    (
        r"\b(sensor|thermistor|bme\d+|bmp\d+|dht\d+|imu|mpu\d+|accelerometer|gyro|lidar|tof|radar|pir|ultrasonic|gps|microphone|mic|hall|load cell|hx711|ina\d+)\b",
        "sensor",
        "sensor",
    ),
    (r"\b(module|breakout|board|shield|hat)\b", "module", "module"),
]


@dataclass
class PartClass:
    type: str
    subtype: str
    category: str
    confidence: float
    matched: str | None = None


def classify_part(name: str, description: str = "", has_pins: bool = False) -> PartClass:
    text = f"{name} {description}".lower()
    for pat, t, sub in PART_RULES:
        m = re.search(pat, text)
        if m:
            cat = CATEGORY_ELECTRICAL if t in ELECTRICAL_TYPES else CATEGORY_MECHANICAL
            conf = 0.9 if re.search(pat, name.lower()) else 0.6
            return PartClass(t, sub, CATEGORY_ELECTRICAL if has_pins else cat, conf, m.group(0))
    # reference rule: anything with pins is electrical
    return PartClass("module" if has_pins else "other", "module" if has_pins else "other",
                     CATEGORY_ELECTRICAL if has_pins else CATEGORY_MECHANICAL, 0.2)  # fmt: skip


# ------------------------------------------------------------------------------ chat intent
EDIT_VERBS = r"\b(add|remove|delete|drop|replace|swap|switch|change|use|make|increase|decrease|reduce|upgrade|downgrade|put|move|rename|set|double|halve|include|get rid of|power it|convert|turn it into)\b"
ASK_CUES = r"(^|\s)(what|why|how|which|where|when|can|could|does|do|is|are|explain|tell me|compare|should)\b|\?\s*$"

INTENT_RULES: list[tuple[str, str]] = [
    (r"\b(replace|swap|switch)\b.*\b(with|for|to)\b", "swap_part"),
    (r"\b(remove|delete|drop|get rid of)\b", "remove_part"),
    (r"\b(\d+|one|two|three|four|five|six)\s+(more|less|fewer)\b|\b(increase|decrease|double|halve)\b", "set_quantity"),
    (r"\b(add|include|put)\b", "add_part"),
    (r"\b(battery|solar|wall|usb[- ]powered|power(ed)? (by|from))\b", "change_power"),
    (r"\b(cheaper|budget|cost less|under \$?\d+)\b", "reduce_cost"),
    (r"\b(smaller|compact|thinner|bigger|larger)\b", "resize"),
    (r"\b(regenerate|start over|redo|from scratch)\b", "regenerate"),
]


@dataclass
class MessageClass:
    mode: str  # edit | ask
    intent: str
    targets: list[str] = field(default_factory=list)
    confidence: float = 0.5
    decision_receipt: str = ""


def classify_message(text: str, content: ProjectContent | None = None) -> MessageClass:
    t = text.strip().lower()
    edit_hit = re.search(EDIT_VERBS, t)
    ask_hit = re.search(ASK_CUES, t)
    # "can you add ..." is a polite imperative => edit
    polite = re.match(r"^(can|could|would|will) you (please )?(add|remove|replace|swap|change|use|make)", t)
    requirement = re.search(
        r"\b(it|this|the device|the \w+)\s+(should|must|needs? to|has to)\b", t
    ) and not t.rstrip().endswith("?")
    wh_question = t.rstrip().endswith("?") and re.match(
        r"^(what|why|how|which|where|when|does|do|is|are|should|would|will)\b", t
    )
    if wh_question and not polite:
        mode = "ask"
    elif polite or requirement or (edit_hit and not (ask_hit and edit_hit.start() > ask_hit.start() + 25)):
        mode = "edit"
    elif ask_hit:
        mode = "ask"
    else:
        mode = "ask" if t.endswith("?") else "edit" if edit_hit else "ask"
    intent = "question"
    if mode == "edit":
        intent = next((name for pat, name in INTENT_RULES if re.search(pat, t)), "modify")
    targets = _targets(t, content)
    via = (
        "wh-question"
        if (wh_question and not polite)
        else "polite-imperative"
        if polite
        else "requirement-statement"
        if requirement
        else "edit-verb"
        if edit_hit
        else "question-cue"
        if ask_hit
        else "default"
    )
    receipt = f"mode={mode} via {via}"
    conf = 0.9 if (polite or (edit_hit and not ask_hit) or (ask_hit and not edit_hit)) else 0.6
    return MessageClass(mode, intent, targets, conf, receipt)


def _targets(text: str, content: ProjectContent | None) -> list[str]:
    if content is None:
        return []
    hits = []
    for n in content.nodes:
        names = {n.id.replace("_", " "), (n.name or "").lower(), (n.product_name or "").lower()}
        if any(nm and len(nm) > 2 and nm in text for nm in names):
            hits.append(n.id)
    return hits
