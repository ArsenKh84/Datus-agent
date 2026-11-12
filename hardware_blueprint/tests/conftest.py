import pytest

from hwbp.catalog import default_catalog
from hwbp.engine import Engine
from hwbp.models import ProjectContent

PROMPTS = {
    "radar": "A portable tactical radar with a touchscreen display, titanium and carbon fiber enclosure",
    "car": "4WD obstacle-avoiding robot car with ultrasonic sensors front and back and LED lights",
    "arm": "Build a 4-axis robotic arm with a joystick, under $60",
    "garden": "Solar powered garden soil moisture monitor that waters plants and sends data over wifi",
    "wearable": "Small wearable heart rate monitor with OLED and bluetooth, 24 hours battery",
    "weather": "Weather station with temperature, humidity, pressure, CO2, e-paper display and LoRa, solar powered",
    "camera": "Security camera with face detection, motion sensor and buzzer alarm, wall powered",
    "dog": "Robot dog quadruped with 12 servos, IMU and camera, battery powered",
    "lamp": "Smart desk lamp with LED strip, rotary knob, light sensor and wifi, USB powered",
}

# Synthetic document in the reference (blueprint.io) wire format. Expected layout computed by hand:
#   base_plate (root, structural) 120x60x3 at y=1.5; shell centre = base + 11.5 -> y 3..23;
#   board_mount centre y=6 (3..9); board held by its mount (mountsFor, implicit) -> y 9..13;
#   sensor = shell - (40,0,0) -> x -50..-30 (inside the shell).  bbox = 124 x 23 x 64.
REFERENCE_DOC = {
    "projectName": "Reference Format Sample",
    "originalPrompt": "sample",
    "plan": "- sample",
    "notes": ["sample"],
    "nodes": [
        {
            "id": "base_plate",
            "name": "Aluminium Base Plate",
            "type": "structural",
            "category": "mechanical",
            "quantity": 1,
            "estimatedCost": 12.5,
            "dimensions": "120x60x3mm",
            "material": "Aluminium",
        },
        {
            "id": "shell",
            "name": "Printed Shell",
            "type": "3d_printed",
            "category": "mechanical",
            "quantity": 1,
            "estimatedCost": 3.0,
            "dimensions": "124x64x20mm",
            "material": "PETG",
            "printSettings": "20% infill",
        },
        {
            "id": "board",
            "name": "Controller",
            "type": "mcu",
            "category": "electrical",
            "quantity": 1,
            "estimatedCost": 6.0,
            "dimensions": "50x25x4mm",
            "pins": ["GPIO1", "GPIO2", "TXD", "RXD", "VCC", "GND"],
            "productName": "ESP32-S3-DevKitC-1-N8R8",
            "research": {
                "specs": [{"label": "pins", "value": "GPIO0-48"}],
                "sourcing": [{"url": "https://example.com/a", "price": "$5.5961", "vendor": "Vendor A"}],
                "datasheetUrl": None,
                "documentationUrls": [],
            },
        },
        {
            "id": "board_mount",
            "name": "Controller Mount",
            "type": "3d_printed",
            "category": "mechanical",
            "quantity": 1,
            "estimatedCost": 1.0,
            "dimensions": "54x29x6mm",
            "mountsFor": "board",
        },
        {
            "id": "screws",
            "name": "M3 screws",
            "type": "misc",
            "category": "mechanical",
            "quantity": 8,
            "estimatedCost": 0.1,
        },
        {
            "id": "sensor",
            "name": "Sensor",
            "type": "sensor",
            "category": "electrical",
            "quantity": 1,
            "estimatedCost": 4.0,
            "dimensions": "20x15x3mm",
            "pins": ["VCC", "GND", "TX", "RX"],
        },
    ],
    "electricalConnections": [
        {
            "type": "data",
            "source": "sensor",
            "target": "board",
            "protocol": "uart",
            "sourcePin": "TX",
            "targetPin": "RXD",
        },
        {
            "type": "data",
            "source": "sensor",
            "target": "board",
            "protocol": "uart",
            "sourcePin": "RX",
            "targetPin": "TXD",
        },
        {"type": "power", "source": "board", "target": "sensor", "voltage": "5V", "current": "80mA"},
    ],
    "mechanicalConnections": [
        {"source": "shell", "target": "base_plate", "label": "screws", "delta": {"x": 0, "y": -11.5, "z": 0}},
        {"source": "board_mount", "target": "base_plate", "label": "screws", "delta": {"x": 0, "y": -4.5, "z": 0}},
        {"source": "sensor", "target": "shell", "label": "glued", "delta": {"x": 40, "y": 0, "z": 0}},
    ],
    "instructionSteps": [
        {"id": "fabricate", "subSteps": [{"id": "fabricate_1", "title": "Print", "partIds": ["shell", "board_mount"]}]}
    ],
    "instructionPreamble": {"tools": ["3D printer"], "assumptions": []},
    "imagePromptSnapshot": {
        "tags": ["sample"],
        "designHash": "0123456789abcdef",
        "description": "d",
        "technicalDescription": "t",
    },
    "unknownFutureField": {"kept": True},
}


@pytest.fixture(scope="session")
def catalog():
    return default_catalog()


@pytest.fixture(scope="session")
def engine():
    return Engine()


@pytest.fixture(scope="session")
def projects(engine):
    return {k: engine.generate(p) for k, p in PROMPTS.items()}


@pytest.fixture()
def reference_doc():
    import copy

    return copy.deepcopy(REFERENCE_DOC)


@pytest.fixture()
def reference_content(reference_doc):
    return ProjectContent.model_validate(reference_doc)
