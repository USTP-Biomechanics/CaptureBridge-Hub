CaptureBridge Hub - Minimal Offline Portable

Use this package when the target PC should not need internet access or a Python
installation.

Start:
  1. Extract the ZIP.
  2. Double-click Run_CaptureBridge_Hub.bat.
  3. Keep the PC and iOS/Android phones on the same private network.
  4. Allow Windows firewall access on Private networks if prompted.

Network ports:
  - TCP 6000 for phone control and file transfer
  - UDP 6000 for phone discovery
  - UDP 6101 for raw phone live preview streams

If phones do not appear or previews do not show frames, check that Windows uses
the Private network profile and allows inbound TCP 6000, UDP 6000, and UDP
6101.

Included:
  - CaptureBridge Hub desktop app
  - Portable CPython runtime
  - tkinter
  - pyserial
  - Pillow for raw phone stream previews
  - ArduinoBridge folder with the sketch and Vicon monitor file
  - app-release.apk Android client installer

Not included:
  - Extra model runtime packages

No app executable is generated. The launcher starts the bundled Python runtime
with tcp_arduino_sync.py.
