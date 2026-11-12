"""Firmware skeleton generation (PlatformIO / Arduino, or Python for Linux SBCs).

Uses the actual pin allocation, bus assignment and per-part firmware hints from the catalog, so the
generated project compiles against the real wiring. (The reference product has no code tab; this is
an engine extension.)
"""

from __future__ import annotations

import re

from hwbp.design import Design, slug


def _pin_literal(mcu_pin: str) -> str:
    """'GPIO8' -> '8', 'GP4' -> '4', Arduino names ('D2', 'A0') stay symbolic."""
    m = re.fullmatch(r"(?:GPIO|GP)(\d+)", mcu_pin)
    return m.group(1) if m else mcu_pin


def generate_firmware(design: Design) -> dict[str, str]:
    mcu = design.mcu
    if mcu is None or mcu.catalog.mcu is None:
        return {}
    spec = mcu.catalog.mcu
    if spec.framework.startswith("linux"):
        return _python_project(design)
    files: dict[str, str] = {}
    libs = sorted({p.catalog.firmware.lib for p in design.parts if p.catalog.firmware and p.catalog.firmware.lib})
    files["platformio.ini"] = "\n".join(
        [
            f"[env:{spec.pio_board}]",
            f"platform = {spec.pio_platform}",
            f"board = {spec.pio_board}",
            "framework = arduino",
            "monitor_speed = 115200",
            *(["lib_deps ="] + [f"    {lib}" for lib in libs] if libs else []),
            "",
        ]
    )
    # pins.h
    lines = ["#pragma once", "// Generated from the design's pin allocation — edit the design, not this file.", ""]
    for bus in design.buses:
        for role, pin in bus["pins"].items():
            lines.append(f"#define {bus['protocol'].upper()}{bus['index']}_{role.upper():<6} {_pin_literal(pin)}")
    for pid, mapping in sorted(design.pin_map.items()):
        for part_pin, mcu_pin in sorted(mapping.items()):
            lines.append(f"#define {slug(pid).upper()}_{slug(part_pin).upper():<10} {_pin_literal(mcu_pin)}")
    files["include/pins.h"] = "\n".join(lines) + "\n"
    # main.cpp
    is_esp = (spec.pio_platform or "") == "espressif32"
    inc = ["#include <Arduino.h>", '#include "pins.h"']
    glob, init, loop = [], [], []
    has = {b["protocol"] for b in design.buses}
    if "i2c" in has:
        inc.append("#include <Wire.h>")
        init.append("Wire.begin(I2C0_SDA, I2C0_SCL);" if is_esp else "Wire.begin();")
    if "spi" in has:
        inc.append("#include <SPI.h>")
        init.append(
            "SPI.begin(SPI0_SCK, SPI0_MISO, SPI0_MOSI);"
            if is_esp and "miso" in next(b for b in design.buses if b["protocol"] == "spi")["pins"]
            else "SPI.begin();"
        )
    for b in design.buses:
        if b["protocol"] == "uart":
            port = f"Serial{b['index'] + 1}"
            init.append(
                f"{port}.begin(256000, SERIAL_8N1, UART{b['index']}_RX, UART{b['index']}_TX);"
                if is_esp
                else f"{port}.begin(9600);"
            )
    for p in design.parts:
        fw = p.catalog.firmware
        if fw and fw.include and f"#include <{fw.include}>" not in inc and fw.include != "Arduino.h":
            inc.append(f"#include <{fw.include}>")
        addr = next((itf.address for itf in p.catalog.interfaces if itf.address), "")
        if fw and fw.globals:
            glob.append(fw.globals)
        if fw and fw.init:
            init.append(fw.init.replace("{address}", addr or ""))
        if fw and fw.loop:
            loop.append(fw.loop)
        for part_pin in design.pin_map.get(p.id, {}):
            role = _role_of(p, part_pin)
            macro = f"{slug(p.id).upper()}_{slug(part_pin).upper()}"
            if role in ("in", "sig") and p.catalog.type in ("sensor", "input"):
                init.append(
                    f"pinMode({macro}, INPUT_PULLUP);" if p.catalog.type == "input" else f"pinMode({macro}, INPUT);"
                )
            elif role in ("out", "gpio", "pwm", "cs"):
                init.append(f"pinMode({macro}, OUTPUT);")
    motors = [
        p
        for p in design.parts
        if p.drives
        and any(design.part(d).catalog.drive and design.part(d).catalog.drive.kind == "dc_motor" for d in p.drives)
    ]
    if motors:
        glob.append(SOFT_START)
        loop.append("// driveRamp(MOTOR_DRIVER_AIN1, 200); // soft-start keeps the battery peak within budget")
    body = [
        *inc,
        "",
        *glob,
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        *[f"  {x}" for x in init],
        '  Serial.println("boot ok");',
        "}",
        "",
        "void loop() {",
        *[f"  {x}" for x in loop],
        "  delay(1000);",
        "}",
        "",
    ]
    files["src/main.cpp"] = "\n".join(body)
    files["README.md"] = (
        f"# Firmware\n\nBoard: {mcu.catalog.name} (`{spec.pio_board}`)\n\n```bash\npio run -t upload && pio device monitor\n```\n"
    )
    return files


SOFT_START = """// Ramp PWM up gradually so only one motor is ever near stall current (see power budget).
void driveRamp(uint8_t pin, uint8_t target) {
  for (uint8_t d = 0; d < target; d += 5) { analogWrite(pin, d); delay(4); }
  analogWrite(pin, target);
}"""


def _role_of(part, part_pin: str) -> str:
    for itf in part.catalog.interfaces:
        if part_pin in itf.pins:
            return itf.pins[part_pin]
        if part_pin in itf.extra:
            return itf.extra[part_pin]
    return "gpio"


def _python_project(design: Design) -> dict[str, str]:
    lines = [
        "# Generated for a Linux SBC — uses gpiozero / smbus2",
        "from gpiozero import DigitalInputDevice, DigitalOutputDevice",
        "",
    ]
    for pid, mapping in sorted(design.pin_map.items()):
        for part_pin, mcu_pin in sorted(mapping.items()):
            lines.append(f"{slug(pid).upper()}_{slug(part_pin).upper()} = {_pin_literal(mcu_pin)}")
    lines += ["", "def main():", "    print('boot ok')", "", "if __name__ == '__main__':", "    main()", ""]
    return {"main.py": "\n".join(lines), "requirements.txt": "gpiozero\nsmbus2\n"}
