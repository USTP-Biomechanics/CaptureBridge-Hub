# CaptureBridge Hub v1.0.11

Highlights:

- connect phones over USB without Wi-Fi: the Hub keeps looking for `adb.exe`
  while it runs, finds it in more places, and re-creates USB reverse forwarding
  after an adb server restart or a cable replug;
- measure when each lag-test timecode reached the screen (Windows DWM vertical
  blank) and report the endpoint lag against that screen time, after the
  phone's boundary-frame offset, in milliseconds and frames;
- remove the High-speed EV setting: above 60 fps the shutter is fixed at about
  one frame period and CaptureBridge Android v1.0.17 matches the brightness of
  the entered ISO and shutter automatically; and
- start TCP control and USB even when the UDP discovery port cannot be opened.

Install the matching CaptureBridge Android v1.0.17 APK. Use an authorized USB
connection or an isolated, trusted private network.
