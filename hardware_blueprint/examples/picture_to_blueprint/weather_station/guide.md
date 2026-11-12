# Portable Weather Station

Portable Weather Station built around a ESP32-C3 SuperMini with BME280 Temp/Humidity/Pressure, 12 mm Tactile Push Button, 2.9in e-Paper 296x128 (SPI); battery-powered (≈10.4 h per charge), housed in a PETG enclosure.

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
### 1.1 3D print 2 part(s) in PETG
Parts: enclosure_base, enclosure_lid
- Enclosure Base: 25% infill, 0.2mm layer
- Enclosure Lid: 25% infill, 0.2mm layer

### 1.2 Test-fit boards on standoffs before wiring
Parts: main_controller, display, battery, charger, reg_5v, pcb_standoffs

## 2. Wire
### 2.1 Assemble the power path (battery, charger, regulators)
Parts: battery, charger, reg_5v
- battery B+ → charger B+ (3.7V)
- charger OUT+ → reg_5v VIN (3.7V)

### 2.2 Distribute the 5V rail to 1 load(s)
Parts: reg_5v, main_controller
- reg_5v.VOUT → main_controller.5V

### 2.3 Distribute the 3.3V rail to 2 load(s)
Parts: main_controller, bme280_temp_humidity_pressure, display
- main_controller.3V3 → bme280_temp_humidity_pressure.VIN
- main_controller.3V3 → display.VCC

### 2.4 Solder I2C bus lines to BME280 Temp/Humidity/Pressure
Parts: main_controller, bme280_temp_humidity_pressure
- SDA = GPIO8
- SCL = GPIO9
- bme280_temp_humidity_pressure.SDA ↔ GPIO8
- bme280_temp_humidity_pressure.SCL ↔ GPIO9

### 2.5 Solder SPI bus lines to Display
Parts: main_controller, display
- MOSI = GPIO6
- MISO = GPIO5
- SCK = GPIO4
- display.DIN ↔ GPIO6
- display.CLK ↔ GPIO4
- display.BUSY ↔ GPIO21
- display.CS ↔ GPIO0
- display.DC ↔ GPIO1
- display.RST ↔ GPIO3

### 2.6 Wire Button 1 signal pins
Parts: main_controller, button_1
- button_1.SW → GPIO7

### 2.7 Wire Button 2 signal pins
Parts: main_controller, button_2
- button_2.SW → GPIO10

### 2.8 Wire Button 3 signal pins
Parts: main_controller, button_3
- button_3.SW → GPIO20

### 2.9 Verify continuity and rail voltages before inserting the MCU
Parts: reg_5v, main_controller, main_controller
- 5V: expect 5 V ±5 % at reg_5v.VOUT
- 3.3V: expect 3.3 V ±5 % at main_controller.3V3

## 3. Bring-up
### 3.1 Flash firmware to the controller
Parts: main_controller
- pio run -t upload
- pio device monitor -b 115200

### 3.2 Test BME280 Temp/Humidity/Pressure
Parts: main_controller, bme280_temp_humidity_pressure

### 3.3 Test Button 1
Parts: main_controller, button_1

### 3.4 Test Button 2
Parts: main_controller, button_2

### 3.5 Test Button 3
Parts: main_controller, button_3

### 3.6 Test Display
Parts: main_controller, display

## 4. Assemble
### 4.1 Mount boards and modules inside the enclosure
Parts: main_controller, bme280_temp_humidity_pressure, battery, charger, reg_5v, enclosure_base, pcb_standoffs, fastener_kit

### 4.2 Install panel parts (display/controls) into the lid
Parts: button_1, button_2, button_3, display, enclosure_lid

### 4.3 Close the enclosure and fasten the lid
Parts: enclosure_lid, enclosure_base, fastener_kit

