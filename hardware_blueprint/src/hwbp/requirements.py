"""Requirement extraction: free-text prompt -> structured ``Requirements``.

The default extractor is deterministic (keyword/phrase rules + number parsing) so the whole pipeline runs
offline and testably. An LLM-backed extractor can be plugged in through the ``RequirementExtractor``
protocol; its output is validated into the same model, so downstream stages never see free text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

# capability -> trigger phrases (regex fragments, matched on word boundaries, case-insensitive)
CAPABILITY_RULES: dict[str, list[str]] = {
    "sense.temperature": [r"temperature", r"thermometer", r"thermostat", r"weather", r"climate"],
    "sense.humidity": [r"humidity", r"humid", r"weather station", r"hygrometer"],
    "sense.pressure": [r"barometer", r"air pressure", r"altitude", r"weather station"],
    "sense.distance": [r"distance", r"range ?finder", r"obstacle", r"avoid(ance)?", r"parking sensor", r"lidar"],
    "sense.presence": [
        r"presence",
        r"occupancy",
        r"radar",
        r"mm ?wave",
        r"people (counter|detect)",
        r"heartbeat sensor",
    ],
    "sense.motion": [r"motion (detect|sens)", r"\bpir\b", r"intruder", r"security alarm", r"movement"],
    "sense.imu": [
        r"\bimu\b",
        r"accelerometer",
        r"gyro",
        r"tilt",
        r"orientation",
        r"balanc",
        r"fall detect",
        r"step count",
    ],
    "sense.pulse": [r"heart ?rate", r"pulse", r"oximet", r"spo2", r"\bbpm\b"],
    "sense.light": [r"light (level|sensor)", r"\blux\b", r"ambient light", r"daylight"],
    "sense.soil_moisture": [r"soil", r"plant", r"garden", r"watering", r"irrigation"],
    "sense.leak": [r"leak", r"flood", r"water (detect|sensor|level)"],
    "sense.co2": [r"\bco2\b", r"carbon dioxide"],
    "sense.gas": [r"air quality", r"\bgas\b", r"smoke", r"\bvoc\b", r"pollution"],
    "sense.gps": [r"\bgps\b", r"location", r"tracker", r"navigation", r"geo"],
    "sense.camera": [
        r"camera",
        r"vision",
        r"photo",
        r"video",
        r"face (detect|recogn)",
        r"object detect",
        r"surveillance",
    ],
    "sense.audio": [r"microphone", r"\bmic\b", r"voice", r"listen", r"sound (detect|level)", r"clap"],
    "sense.current": [r"current (monitor|sens)", r"power (monitor|meter)", r"energy monitor"],
    "ui.display": [
        r"display",
        r"screen",
        r"\boled\b",
        r"\blcd\b",
        r"\btft\b",
        r"dashboard",
        r"\bhud\b",
        r"e-?ink",
        r"e-?paper",
    ],
    "display.touch": [r"touch ?screen", r"touch display", r"tablet"],
    "display.epaper": [r"e-?ink", r"e-?paper"],
    "comm.wifi": [
        r"wi-?fi",
        r"internet",
        r"\biot\b",
        r"cloud",
        r"web",
        r"\bapp\b",
        r"smartphone",
        r"remote",
        r"mqtt",
        r"home ?assistant",
    ],
    "comm.ble": [r"bluetooth", r"\bble\b"],
    "comm.lora": [r"\blora\b", r"long[- ]range", r"miles away", r"\bkm\b"],
    "comm.cellular": [r"\bgsm\b", r"\bsms\b", r"cellular", r"\b4g\b", r"\blte\b"],
    "actuate.drive": [
        r"\bcar\b",
        r"rover",
        r"wheel",
        r"drive",
        r"robot (car|vehicle)",
        r"\brc\b",
        r"delivery robot",
        r"tank",
        r"vehicle",
    ],
    "actuate.servo": [
        r"servo",
        r"robotic arm",
        r"robot arm",
        r"gripper",
        r"pan[- ]tilt",
        r"claw",
        r"\bdof\b",
        r"joint",
        r"leg",
        r"\bdog\b",
        r"hexapod",
        r"quadruped",
    ],
    "actuate.stepper": [r"stepper", r"\bcnc\b", r"plotter", r"precise position", r"linear rail", r"camera slider"],
    "actuate.pump": [r"pump", r"watering", r"water(s|ing)? (the )?plants?", r"irrigation", r"aquarium", r"dispenser"],
    "actuate.relay": [r"relay", r"switch (the )?(lamp|light|appliance|mains)", r"smart plug", r"\bmains\b"],
    "output.light": [
        r"\bled",
        r"light strip",
        r"lamp",
        r"\brgb\b",
        r"neopixel",
        r"ambient light",
        r"night ?light",
        r"glow",
    ],
    "output.audio": [r"speaker", r"music", r"audio", r"talk", r"\btts\b", r"voice assistant"],
    "output.buzzer": [r"buzzer", r"alarm", r"beep", r"siren", r"alert"],
    "input.button": [r"button", r"push[- ]?button", r"\bclick", r"game ?controller"],
    "input.encoder": [r"knob", r"dial", r"rotary", r"volume control"],
    "input.joystick": [r"joystick", r"gamepad", r"remote control"],
}

POWER_RULES: dict[str, list[str]] = {
    "battery": [
        r"battery",
        r"portable",
        r"wearable",
        r"handheld",
        r"wireless",
        r"cordless",
        r"pocket",
        r"field",
        r"drone",
        r"rover",
        r"robot (car|dog|vehicle)",
    ],
    "solar": [r"solar", r"sun[- ]powered", r"off[- ]grid", r"trickle charg"],
    "wall": [r"plug(ged)? in", r"wall", r"mains", r"desk", r"desktop", r"usb[- ]powered", r"stationary", r"kitchen"],
}

SIZE_RULES = {
    "tiny": [r"tiny", r"miniature", r"wearable", r"ring", r"keychain", r"pocket"],
    "small": [r"small", r"compact", r"slim", r"handheld", r"portable", r"thin"],
    "large": [r"large", r"big", r"desktop", r"floor", r"cart", r"truck"],
}

PRIORITY_RULES = {
    "cost": [r"cheap", r"budget", r"low[- ]cost", r"inexpensive", r"affordable"],
    "battery_life": [r"long battery", r"low[- ]power", r"months on", r"years on", r"energy efficient", r"sleep"],
    "precision": [r"accurate", r"precise", r"precision", r"high[- ]accuracy", r"medical", r"lab"],
    "performance": [r"fast", r"real[- ]time", r"high[- ]performance", r"\bai\b", r"machine learning", r"smooth"],
    "robustness": [r"rugged", r"tactical", r"outdoor", r"waterproof", r"industrial", r"military"],
}

MATERIALS = [
    "titanium",
    "aluminum",
    "aluminium",
    "carbon fiber",
    "carbon fibre",
    "wood",
    "acrylic",
    "steel",
    "petg",
    "pla",
    "abs",
    "asa",
    "tpu",
    "nylon",
]

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "twelve": 12}


class Requirements(BaseModel):
    prompt: str
    capabilities: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)  # capability -> quantity (servos, buttons, motors)
    power_source: str = "wall"  # battery | wall | solar
    runtime_hours: float | None = None
    budget_usd: float | None = None
    size_class: str = "medium"  # tiny | small | medium | large
    priorities: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # salient prompt words, used for name/feature matching
    name: str = "Untitled Device"
    # user overrides (chat edits): capability/role -> catalog id; catalog ids never to use
    force: dict[str, str] = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)
    form_factor: dict | None = None  # from a picture silhouette: {widthMm, depthMm, aspect, fillRatio}

    @property
    def design_hash(self) -> str:
        """Stable 16-hex hash of the normalised requirements (cache/dedupe key)."""
        key = "|".join(
            [",".join(sorted(self.capabilities)), self.power_source, self.size_class, ",".join(sorted(self.priorities))]
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]


class RequirementExtractor(Protocol):
    def extract(self, prompt: str) -> Requirements: ...


def _match_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(rf"(?<![a-z]){p}", text) for p in patterns)


@dataclass
class RuleBasedExtractor:
    """Deterministic extractor. ``default_runtime_h`` applies to battery designs with no stated runtime."""

    default_runtime_h: float = 4.0

    def extract(self, prompt: str) -> Requirements:
        text = " " + prompt.lower().replace("’", "'") + " "
        caps = [c for c, pats in CAPABILITY_RULES.items() if _match_any(text, pats)]
        if "display.touch" in caps or "display.epaper" in caps:
            caps.append("ui.display") if "ui.display" not in caps else None
        counts = self._counts(text, caps)
        power = self._power(text, caps)
        req = Requirements(
            prompt=prompt.strip(),
            capabilities=sorted(set(caps)),
            counts=counts,
            power_source=power,
            runtime_hours=self._runtime(text) or (self.default_runtime_h if power != "wall" else None),
            budget_usd=self._budget(text),
            size_class=next((k for k, pats in SIZE_RULES.items() if _match_any(text, pats)), "medium"),
            priorities=[k for k, pats in PRIORITY_RULES.items() if _match_any(text, pats)],
            materials=sorted(
                {
                    m.replace("aluminium", "aluminum").replace("fibre", "fiber")
                    for m in MATERIALS
                    if re.search(rf"\b{m}\b", text)
                }
            ),
            name=self._name(prompt),
        )
        req.tags = self._tags(req)
        req.keywords = self._keywords(text)
        return req

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _num_before(text: str, noun: str) -> int | None:
        m = re.search(rf"(\d+|{'|'.join(_WORD_NUM)})[\s-]*(?:x\s*)?(?:[a-z-]+\s){{0,2}}{noun}", text)
        if not m:
            return None
        tok = m.group(1)
        return int(tok) if tok.isdigit() else _WORD_NUM[tok]

    def _counts(self, text: str, caps: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        if "actuate.servo" in caps:
            n = self._num_before(text, r"(?:servos?|dof|axis|axes|joints?)") or (
                12
                if re.search(r"quadruped|robot dog|\bdog\b", text)
                else 18
                if "hexapod" in text
                else 4
                if re.search(r"arm|gripper", text)
                else 2
            )
            counts["actuate.servo"] = max(1, min(n, 32))
        if "actuate.drive" in caps:
            counts["actuate.drive"] = 4 if re.search(r"4wd|four[- ]wheel|4 wheels|mecanum", text) else 2
        if "actuate.stepper" in caps:
            counts["actuate.stepper"] = self._num_before(text, r"(?:steppers?|axis|axes)") or 2
        if "input.button" in caps:
            counts["input.button"] = min(self._num_before(text, r"buttons?") or 2, 12)
        if "sense.distance" in caps and re.search(r"(front|rear|back|all sides|360)", text):
            counts["sense.distance"] = 2 if "all sides" not in text and "360" not in text else 4
        return counts

    @staticmethod
    def _power(text: str, caps: list[str]) -> str:
        if _match_any(text, POWER_RULES["solar"]):
            return "solar"
        if _match_any(text, POWER_RULES["wall"]) and not re.search(r"portable|battery", text):
            return "wall"
        if _match_any(text, POWER_RULES["battery"]) or "actuate.drive" in caps:
            return "battery"
        return "wall"

    @staticmethod
    def _runtime(text: str) -> float | None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|days?|weeks?|months?)\b", text)
        if not m:
            return None
        v, unit = float(m.group(1)), m.group(2)
        mult = (
            24 if unit.startswith("day") else 168 if unit.startswith("week") else 720 if unit.startswith("month") else 1
        )
        return v * mult

    @staticmethod
    def _budget(text: str) -> float | None:
        m = re.search(
            r"(?:under|below|less than|max(?:imum)?|budget(?: of)?|<)\s*\$?\s*(\d+(?:\.\d+)?)\s*(?:\$|usd|dollars)?",
            text,
        )
        if m and ("$" in text or "usd" in text or "dollar" in text or "budget" in text):
            return float(m.group(1))
        return None

    @staticmethod
    def _name(prompt: str) -> str:
        words = re.findall(r"[A-Za-z0-9-]+", prompt)
        stop = {
            "a",
            "an",
            "the",
            "i",
            "want",
            "to",
            "build",
            "make",
            "create",
            "design",
            "that",
            "with",
            "for",
            "my",
            "me",
            "of",
            "and",
            "which",
            "can",
        }
        core = [w for w in words if w.lower() not in stop][:3]
        return " ".join(w.capitalize() for w in core) or "Untitled Device"

    @staticmethod
    def _keywords(text: str) -> list[str]:
        stop = {
            "with",
            "that",
            "and",
            "the",
            "for",
            "build",
            "make",
            "create",
            "design",
            "want",
            "which",
            "from",
            "into",
            "under",
            "over",
            "using",
            "have",
            "has",
            "can",
            "will",
            "should",
            "device",
            "system",
            "small",
            "portable",
        }
        words = re.findall(r"[a-z][a-z0-9-]{2,}", text)
        return sorted({w for w in words if w not in stop})

    @staticmethod
    def _tags(req: Requirements) -> list[str]:
        tags = [c.split(".", 1)[1].replace("_", " ") for c in req.capabilities]
        tags.append(f"{req.power_source} powered")
        tags += [f"{m} materials" for m in req.materials]
        if req.size_class in ("tiny", "small"):
            tags.append(f"{req.size_class} enclosure")
        return tags[:8]


def extract_requirements(prompt: str, extractor: RequirementExtractor | None = None) -> Requirements:
    return (extractor or RuleBasedExtractor()).extract(prompt)
