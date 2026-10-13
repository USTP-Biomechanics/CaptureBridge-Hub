# Changelog

## Unreleased

- Keep searching for `adb.exe` while the Hub runs instead of giving up at
  startup, so USB phones connect without Wi-Fi once Android SDK
  Platform-Tools is available. The status line now reads
  `USB unavailable: adb.exe not found - ...` while it is missing.
- Look for `adb.exe` in more places: a `platform-tools` folder next to the
  launcher, the Visual Studio/Xamarin Android SDK, and Unity's bundled SDK.
  `phone_transport.adb_path` may now name the folder that contains `adb.exe`.
  Among the found copies, prefer adb 1.0.41 or newer so the Hub does not
  restart a running adb server; a configured `adb_path` that runs is always
  used.
- Check USB reverse forwarding on every ADB poll and re-create it after an adb
  server restart or a cable replug. Disconnect removes only the ports the Hub
  manages.
- Start TCP control and USB even when the UDP discovery port cannot be opened.
- Read adb output as UTF-8 and ignore adb daemon start-up lines.
- Ignore log and UI updates from background threads after the window closes.
- Add the `phone_transport` block to the portable package configuration.
- Document that the shutter is fixed above 60 fps (about one frame period); the
  ISO and shutter tooltips say so. CaptureBridge Android 1.0.16 matches the
  brightness of the entered ISO and shutter automatically in those modes.
- Measure when each lag-test timecode actually reached the screen (DWM vertical
  blank per displayed timecode, Windows only) and report the endpoint lag
  against that screen time (`screen_start_lag_ms`, `screen_stop_lag_ms`, their
  half-widths, and `screen_*_residual_ms` after the phone's chosen boundary
  frame offset). The raw render-time lag fields are unchanged.
- Write the phone's camera mode, requested ISO and shutter, and the automatic
  high-speed exposure compensation it applied to lag-test reports as `camera_*`
  fields.
- Show the lag-test result against measured screen time, after the phone's
  boundary-frame offset, in milliseconds and frames; the raw values follow it.

## v1.0.10 - 2026-07-30

- Update the portable-release and source-install Pillow requirement to 12.3.0
  or newer, which contains the upstream fixes for the advisories reported
  against Pillow 12.2.0.
- Clarify the Android-only release scope, pin the compatible SoftwareX release
  pair, document checksum verification, and align boundary-timing terminology.

## v1.0.9 - 2026-07-11

- Require Pillow 12.2 or newer and build the portable release from an exact,
  audited dependency set.
- Accept file payloads only after an operator-requested transfer, enforce
  per-file and per-request size limits, and publish files atomically after a
  matching FILE_DONE.
- Remove partial files after interrupted or invalid transfers.
- Add transfer-hardening tests, push/pull-request CI, dependency auditing, and
  a SHA-256 checksum for the portable release archive.
- Document the trusted-network threat model and publish the manuscript bench
  observation protocol and summary.

## v1.0.8 - 2026-07-11

- Display Android battery level and charging state.
- Harden protocol-line handling and UI metadata updates.
- Run the Hub unit-test suite in release builds.

## v1.0.7 - 2026-07-01

- Add USB/ADB reverse control transport and TCP live preview over USB.
- Add background clock-offset estimation and scheduled phone-clock capture
  boundaries.

## v1.0.6 - 2026-06-27

- Expand lag-test timing, segment, display, and analyzer diagnostics.
