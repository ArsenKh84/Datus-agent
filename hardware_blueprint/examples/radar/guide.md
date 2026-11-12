# Portable Tactical Radar

Portable Tactical Radar built around a ESP32-C3 SuperMini with HLK-LD2410C 24 GHz mmWave Presence Radar, 2.8in TFT 320x240 Touch (ILI9341 + XPT2046); battery-powered (≈5.5 h per charge), housed in a Carbon Fiber PETG enclosure.

## Tools
- 3D printer
- Digital multimeter
- Soldering iron
- Solder wire
- Wire strippers
- Soldering iron with heat-set tip
- USB-C data cable
- Computer with PlatformIO
- Screwdriver set (M2.5 / M3)

## Assumptions
- Slicer (PrusaSlicer/Cura/Bambu Studio) configured for the listed materials
- PlatformIO (VS Code extension or CLI) installed
- Lithium battery safety: never short, puncture or charge unattended

## 1. Fabricate
### 1.1 3D print 2 part(s) in Carbon Fiber PETG
Parts: enclosure_base, enclosure_lid
- Enclosure Base: 40% infill, 0.15mm layer
- Enclosure Lid: 40% infill, 0.15mm layer

### 1.2 Test-fit boards on standoffs before wiring
Parts: main_controller, presence_radar, display, battery, charger, reg_5v, pcb_standoffs

## 2. Wire
### 2.1 Assemble the power path (battery, charger, regulators)
Parts: battery, charger, reg_5v
- battery B+ → charger B+ (3.7V)
- charger OUT+ → reg_5v VIN (3.7V)

### 2.2 Distribute the 5V rail to 2 load(s)
Parts: reg_5v, main_controller, presence_radar
- reg_5v.VOUT → main_controller.5V
- reg_5v.VOUT → presence_radar.VCC

### 2.3 Distribute the 3.3V rail to 1 load(s)
Parts: main_controller, display
- main_controller.3V3 → display.VCC

### 2.4 Solder UART bus lines to Presence Radar
Parts: main_controller, presence_radar
- TX = GPIO21
- RX = GPIO20
- presence_radar.TX ↔ GPIO20
- presence_radar.RX ↔ GPIO21
- presence_radar.OUT ↔ GPIO3

### 2.5 Solder SPI bus lines to Display, Display
Parts: main_controller, display, display
- MOSI = GPIO6
- MISO = GPIO5
- SCK = GPIO4
- display.MOSI ↔ GPIO6
- display.MISO ↔ GPIO5
- display.SCK ↔ GPIO4
- display.T_DIN ↔ GPIO6
- display.T_DO ↔ GPIO5
- display.T_CLK ↔ GPIO4
- display.CS ↔ GPIO7
- display.DC ↔ GPIO8
- display.LED ↔ GPIO9
- display.RST ↔ GPIO10
- display.T_CS ↔ GPIO0
- display.T_IRQ ↔ GPIO1
- display.MOSI ↔ GPIO6
- display.MISO ↔ GPIO5
- display.SCK ↔ GPIO4
- display.T_DIN ↔ GPIO6
- display.T_DO ↔ GPIO5
- display.T_CLK ↔ GPIO4
- display.CS ↔ GPIO7
- display.DC ↔ GPIO8
- display.LED ↔ GPIO9
- display.RST ↔ GPIO10
- display.T_CS ↔ GPIO0
- display.T_IRQ ↔ GPIO1

### 2.6 Verify continuity and rail voltages before inserting the MCU
Parts: reg_5v, main_controller, main_controller
- 5V: expect 5 V ±5 % at reg_5v.VOUT
- 3.3V: expect 3.3 V ±5 % at main_controller.3V3

## 3. Bring-up
### 3.1 Flash firmware to the controller
Parts: main_controller
- pio run -t upload
- pio device monitor -b 115200

### 3.2 Test Presence Radar
Parts: main_controller, presence_radar

### 3.3 Test Display
Parts: main_controller, display

## 4. Assemble
### 4.1 Mount boards and modules inside the enclosure
Parts: main_controller, presence_radar, battery, charger, reg_5v, enclosure_base, pcb_standoffs, fastener_kit

### 4.2 Install panel parts (display/controls) into the lid
Parts: display, enclosure_lid

### 4.3 Close the enclosure and fasten the lid
Parts: enclosure_lid, enclosure_base, fastener_kit

