# Changelog

All notable changes to MPXAir are listed here.

## 1.1.0 — 2026-10-21

### Added
- **Connection progress window**, as in Mission Planner: link, heartbeat, parameters counting up, then
  mission, geofence and rally points. It closes on its own when done.
- **Plan Read and Write progress windows**: item-by-item progress and what the aircraft confirmed.
- **Quick view**: add or remove tiles (2–16), and more than 45 values in sections.
- **Parameters groups sidebar** with friendly names.
- **Hover descriptions** for parameters and settings: Setup forms, the Parameters list, mission item
  fields and Preferences.
- **Flight trail length** setting (10 s … whole flight). Older track is cleared automatically and fades out.
- **Aircraft GPS status** card at the bottom-left of the Fly map.

### Changed
- Drop-down settings show their full value under the setting name, and long option names wrap. This
  fixes unreadable values such as Battery monitoring.
- Preferences → Maps → Internet use is now a list of three clearly explained choices.

## 1.0.0 — 2026-10-21

First public release.

### Added
- **Fly**:
  - HUD, live map, quick-view tiles, actions, messages, RC/servo bars, and Air and Ground status.
  - Telemetry recording and replay.
- **Plan**:
  - Missions, geofence, rally points, survey grid, undo/redo, `.waypoints` files, and vehicle read/write.
  - Clear options: mission, fence, rally, survey area or everything; plan only, or plan and vehicle.
- **Setup**:
  - Firmware install from ArduPilot's official list or an `.apj` file.
  - Frame type, accelerometer, compass and radio calibration, flight modes, failsafes, battery monitor.
  - Motor test with a frame diagram showing motor positions, numbers, test letters and spin directions.
- **Parameters**: search, descriptions, staged writes, `.param` load/save, and online description updates.
- **Logs**: onboard log download, `.bin` graphs, flight path, messages and parameters.
- **Ground**: ground position from the RFD900 + ESP32 ground-station board GPS, NMEA or MAVLink GPS in
  the telemetry stream, a USB GPS, the computer's location, or a fixed position. Also Follow Me and
  the antenna tracker.
- **RFD900 + ESP32 ground station board support**:
  - automatic detection;
  - its GPS as the ground position;
  - the link stays open while the aircraft is off;
  - the board's position broadcast for ArduPilot Follow mode can be switched on.
- **Map**:
  - distinct **AIR** and **GROUND** icons, the ground-to-air distance line and the GND GPS chip;
  - *Keep both in view*;
  - six map sources, offline areas and refresh.
- **Installer**: user or system install, desktop launcher, dialout setup, uninstaller.
- **Built-in demo drone**, which also simulates the ground-station board.

### Fixed during development
- Plan list: plain clicks and touch taps select one item (Ctrl and Shift extend the selection).
- Plan: switching an item to or from Takeoff no longer leaves it at a stray map position.
- Connection picker: long USB device names no longer stretch the header.
