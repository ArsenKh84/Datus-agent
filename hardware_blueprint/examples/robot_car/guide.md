# 4wd Obstacle-avoiding Robot

4wd Obstacle-avoiding Robot built around a Raspberry Pi Pico W with HC-SR04 Ultrasonic Distance (4 m), WS2812B RGB LED Strip (30 LEDs, 1 m), N20 Micro Gear Motor 6 V 300 rpm, DRV8833 Dual H-Bridge Motor Driver; battery-powered (≈4.8 h per charge), housed in a PETG enclosure.

## Tools
- 3D printer
- Calipers
- Digital multimeter
- Soldering iron
- Solder wire
- Wire strippers
- Flux
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

### 1.2 Cut / machine the structural plate and deburr edges
Parts: chassis_plate

### 1.3 Test-fit boards on standoffs before wiring
Parts: main_controller, distance_sensor_1, distance_sensor_2, led_strip, drive_motor_1, drive_motor_2, drive_motor_3, drive_motor_4, motor_driver, motor_driver_2, battery, charger, reg_5v, pcb_standoffs

## 2. Wire
### 2.1 Assemble the power path (battery, charger, regulators)
Parts: battery, charger, reg_5v
- battery B+ → charger B+ (3.6V)
- charger OUT+ → reg_5v VIN (3.6V)

### 2.2 Distribute the 5V rail to 4 load(s)
Parts: reg_5v, main_controller, distance_sensor_1, distance_sensor_2, led_strip
- reg_5v.VOUT → main_controller.VSYS
- reg_5v.VOUT → distance_sensor_1.VCC
- reg_5v.VOUT → distance_sensor_2.VCC
- reg_5v.VOUT → led_strip.5V

### 2.3 Distribute the VBAT rail to 4 load(s)
Parts: charger, drive_motor_1, drive_motor_2, drive_motor_3, drive_motor_4
- charger.OUT+ → drive_motor_1.M+
- charger.OUT+ → drive_motor_2.M+
- charger.OUT+ → drive_motor_3.M+
- charger.OUT+ → drive_motor_4.M+

### 2.4 Distribute the 3.3V rail to 2 load(s)
Parts: main_controller, level_shifter, level_shifter_2
- main_controller.3V3 → level_shifter.LV
- main_controller.3V3 → level_shifter_2.LV

### 2.5 Wire Distance Sensor 1 signal pins
Parts: main_controller, distance_sensor_1
- distance_sensor_1.ECHO → GP0
- distance_sensor_1.TRIG → GP1

### 2.6 Wire Distance Sensor 2 signal pins
Parts: main_controller, distance_sensor_2
- distance_sensor_2.ECHO → GP2
- distance_sensor_2.TRIG → GP3

### 2.7 Wire LED Strip signal pins
Parts: main_controller, led_strip
- led_strip.DIN → GP4

### 2.8 Wire Motor Driver 1 signal pins
Parts: main_controller, motor_driver
- motor_driver.AIN1 → GP5
- motor_driver.AIN2 → GP6
- motor_driver.BIN1 → GP7
- motor_driver.BIN2 → GP8

### 2.9 Wire Motor Driver 2 signal pins
Parts: main_controller, motor_driver_2
- motor_driver_2.AIN1 → GP9
- motor_driver_2.AIN2 → GP10
- motor_driver_2.BIN1 → GP11
- motor_driver_2.BIN2 → GP12

### 2.10 Connect Motor Driver 1 outputs to 2 load(s)
Parts: motor_driver, drive_motor_1, drive_motor_2

### 2.11 Connect Motor Driver 2 outputs to 2 load(s)
Parts: motor_driver_2, drive_motor_3, drive_motor_4

### 2.12 Insert level shifters (LV = 3.3 V side, HV = 5 V side)
Parts: level_shifter, level_shifter_2

### 2.13 Verify continuity and rail voltages before inserting the MCU
Parts: reg_5v, charger, main_controller, main_controller
- 5V: expect 5 V ±5 % at reg_5v.VOUT
- VBAT: expect 3.6 V ±5 % at charger.OUT+
- 3.3V: expect 3.3 V ±5 % at main_controller.3V3

## 3. Bring-up
### 3.1 Flash firmware to the controller
Parts: main_controller
- pio run -t upload
- pio device monitor -b 115200

### 3.2 Test Distance Sensor 1
Parts: main_controller, distance_sensor_1

### 3.3 Test Distance Sensor 2
Parts: main_controller, distance_sensor_2

### 3.4 Test LED Strip
Parts: main_controller, led_strip

### 3.5 Test Motor Driver 1
Parts: main_controller, motor_driver

### 3.6 Test Motor Driver 2
Parts: main_controller, motor_driver_2

### 3.7 Run actuators at low duty first (soft-start) and check driver temperature
Parts: drive_motor_1, drive_motor_2, drive_motor_3, drive_motor_4, motor_driver, motor_driver_2

## 4. Assemble
### 4.1 Mount boards and modules inside the enclosure
Parts: main_controller, distance_sensor_1, distance_sensor_2, motor_driver, motor_driver_2, level_shifter, level_shifter_2, battery, charger, reg_5v, enclosure_base, pcb_standoffs, fastener_kit

### 4.2 Mount external actuators/sensors and route cables
Parts: led_strip, drive_motor_1, drive_motor_2, drive_motor_3, drive_motor_4

### 4.3 Fasten the enclosure onto the chassis plate
Parts: enclosure_base, chassis_plate

### 4.4 Close the enclosure and fasten the lid
Parts: enclosure_lid, enclosure_base, fastener_kit

## Design review
- **warning** logic_level: Distance Sensor 1 uses 5 V logic but Raspberry Pi Pico W is 3.3 V and not 5 V tolerant. — A BSS138 level shifter is inserted in these signal lines by the wiring stage.
- **warning** logic_level: Distance Sensor 2 uses 5 V logic but Raspberry Pi Pico W is 3.3 V and not 5 V tolerant. — A BSS138 level shifter is inserted in these signal lines by the wiring stage.
- **warning** logic_level: LED Strip uses 5 V logic but Raspberry Pi Pico W is 3.3 V and not 5 V tolerant. — A BSS138 level shifter is inserted in these signal lines by the wiring stage.
- **warning** battery_capacity: 18650 Li-ion 3000 mAh + Holder gives ≈3.9 h, below the 4 h target. — Use a larger pack or parallel cells.
- **error** rail_overcurrent: VBAT rail peak 4177 mA exceeds 3000 mA source rating. — Choose a higher-current regulator or split the rail.
