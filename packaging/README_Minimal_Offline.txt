CaptureBridge Hub - Minimal Offline Portable

Use this package when the target PC should not need internet access or a Python
installation.

Start:
  1. Extract the ZIP.
  2. Download the Android APK from the latest CaptureBridge Android release:
     https://github.com/USTP-Biomechanics/CaptureBridge-Android/releases/latest
  3. Install the downloaded APK on the phone. If Android asks, allow installing
     this APK from the file manager or browser you are using.
  4. Keep the PC and phone on the same private Wi-Fi or LAN.
  5. Double-click Run_CaptureBridge_Hub.bat on the PC.
  6. Allow Windows firewall access on Private networks if prompted.
  7. Start the CaptureBridge Android app and confirm the phone appears in the hub.

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
  - numpy and OpenCV for lag-test video analysis
  - lag_test folder for the timing target and reports
  - ArduinoBridge folder with the sketch and Vicon monitor file

Not included:
  - Android client APK, available from:
    https://github.com/USTP-Biomechanics/CaptureBridge-Android/releases/latest
  - Extra model runtime packages

No app executable is generated. The launcher starts the bundled Python runtime
with tcp_arduino_sync.py.
