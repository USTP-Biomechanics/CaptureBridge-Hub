# CaptureBridge ADB Reverse Transport Plan

## Decision

- [x] Use `adb reverse` as the wired phone transport.
- [x] Do not build a custom USB serial protocol for phones.
- [x] Keep the existing TCP protocol on port `6000`; route it through USB with ADB reverse when available.

## Why ADB Reverse

Android apps do not normally expose a simple app-owned USB serial channel to Windows. ADB reverse gives us a clean wired path with minimal protocol churn:

- Hub still listens on local TCP `6000`.
- Hub runs `adb -s <serial> reverse tcp:6000 tcp:6000`.
- Android connects to `127.0.0.1:6000`.
- The cable carries the TCP stream through ADB.
- Existing commands still work: `HELLO`, `NAME`, `SETTINGS`, `PREPARE`, `START`, `STOP`, `LIST`, `TRANSFER`, delete, and lag-test messages.

## Scope

- Hub repo: `C:\Users\MoCapLab\PycharmProjects\CaptureBridge-Hub`
- Android repo: `C:\Users\MoCapLab\PycharmProjects\CaptureBridge-Android`
- Primary Hub file today: `src/tcp_arduino_sync.py`
- Primary Android file today: `src/main/java/com/marksimonlehner/capturebridge/TcpController.kt`

## Current Code Facts

- [x] Hub starts `TcpServer` on `SERVER_PORT = 6000` and UDP discovery on `DISCOVERY_UDP_PORT = 6000`.
- [x] Hub sends all phone commands over the accepted TCP socket.
- [x] Android discovery sends UDP `DISCOVER_UDPCAMERA`, then connects to the discovered Hub IP.
- [x] Android already has `tryUsbReverseFallback()`, which tries `127.0.0.1:6000` after UDP discovery times out.
- [x] That fallback only works if someone already ran `adb reverse tcp:6000 tcp:6000`.
- [x] Hub currently has no ADB discovery, no reverse setup, no USB status, and no transport label per phone.

## Target Behavior

- [ ] When Hub phone networking starts, Hub detects authorized USB Android devices.
- [ ] For every authorized USB phone, Hub creates ADB reverse:
  - `adb -s <serial> reverse tcp:6000 tcp:6000`
- [ ] Android tries USB reverse first:
  - connect to `127.0.0.1:6000`
- [ ] If USB succeeds, Android reports the connection as `usb_adb_reverse`.
- [ ] If USB fails and fallback is allowed, Android uses the existing Wi-Fi UDP discovery path.
- [ ] Hub shows each connected phone transport as `USB` or `Wi-Fi`.
- [ ] Lag-test reports include the transport so bad Wi-Fi runs are not mixed with USB runs.

## Hub Implementation Plan

- [ ] Add an ADB helper module, likely `src/android_adb.py`.
  - `find_adb_exe()`
  - `list_devices()`
  - `setup_reverse(serial, local_port=6000, remote_port=6000)`
  - `remove_reverse(serial, remote_port=6000)`
  - `remove_all_managed_reverses()`
  - Return structured results instead of parsing strings in the UI layer.

- [ ] Locate `adb.exe` robustly.
  - Check app config override first, for example `phone_transport.adb_path`.
  - Check `ANDROID_HOME`.
  - Check `ANDROID_SDK_ROOT`.
  - Check common Windows SDK paths:
    - `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`
    - `C:\Users\<user>\AppData\Local\Android\Sdk\platform-tools\adb.exe`
  - If unavailable, log one clear warning and continue Wi-Fi only.

- [ ] Parse `adb devices -l`.
  - Handle `device`, `unauthorized`, `offline`, and unknown states.
  - Extract `serial`, `model`, `product`, and `transport_id` when present.
  - Keep unauthorized devices visible in logs because they need user action on the phone.

- [ ] Add a background ADB monitor to the Hub.
  - Start it when `connect_phone_network()` succeeds.
  - Stop it when `disconnect_phone_network()` or app quit runs.
  - Poll every few seconds, or trigger once on startup plus a slower refresh.
  - Avoid blocking Tkinter UI.

- [ ] Manage reverse port lifecycle.
  - Create reverse only for authorized `device` entries.
  - Do not repeatedly spam `adb reverse` if a serial is already configured.
  - Remove managed reverses on Hub shutdown/disconnect.
  - If a phone disappears, remove it from Hub ADB state.

- [ ] Add app config.
  - Add `phone_transport` in `app_config.json`:
    - `mode`: `adb_reverse_first`
    - `adb_path`: optional string
    - `adb_poll_interval_sec`: maybe `3`
    - `allow_wifi_fallback`: `true`
  - Keep Wi-Fi behavior unchanged when ADB is missing.

- [ ] Add Hub UI/log visibility.
  - Show summary near connection status:
    - `Phone connection: USB first, 1 ADB phone ready`
    - `ADB unavailable; Wi-Fi discovery only`
    - `ADB phone unauthorized: <serial>`
  - Add transport to connected-phone labels:
    - `Pixel 8 (USB)`
    - `S25 Ultra (Wi-Fi 192.168.x.x:port)`

- [ ] Teach Hub to accept transport metadata.
  - Preferred: Android sends a new line after connect:
    - `TRANSPORT usb_adb_reverse serial=<android_serial_if_known>`
    - `TRANSPORT wifi host=<resolved_host>`
  - Simpler fallback: append fields to `HELLO`, but this is messier because names can contain spaces.
  - Store transport on the Hub client dict.
  - Include it in client labels, logs, and lag-test `report_extra`.

## Android Implementation Plan

- [ ] Add explicit transport state in `TcpController.kt`.
  - `enum class TransportMode { UsbAdbReverse, WifiDiscovery, DirectHost }`
  - Track current transport for reconnects and status text.

- [ ] Change connection order.
  - Current order: UDP discovery first, USB reverse only after UDP timeout.
  - New order: USB reverse first, then UDP discovery if fallback is allowed.
  - Try `127.0.0.1:6000` with a short timeout, around `300-500 ms`.
  - If it connects, skip UDP discovery.

- [ ] Send transport metadata after `HELLO`.
  - On USB reverse:
    - `TRANSPORT usb_adb_reverse`
  - On Wi-Fi discovery:
    - `TRANSPORT wifi host=<resolvedHubIp>`
  - On manual/direct host:
    - `TRANSPORT direct host=<host>`

- [ ] Improve Android status text.
  - `Connected via USB`
  - `USB unavailable, discovering Wi-Fi...`
  - `Connected via Wi-Fi`
  - `Waiting for Hub...`

- [ ] Keep reconnect behavior simple.
  - If the last connection was USB, retry USB first.
  - If USB fails and Wi-Fi fallback is allowed, discover Wi-Fi.
  - Avoid adding Android-side ADB logic; ADB is entirely Hub/desktop responsibility.

## Lag-Test Reliability Plan After ADB

- [ ] Add transport to lag reports.
  - CSV and JSON should include `transport`, `client_addr`, and ideally ADB serial/model if Hub can map it.

- [ ] Separate timing sources in the report.
  - Hub scheduled START target.
  - Hub actual START send time.
  - Hub actual STOP send time.
  - Phone START receive elapsed timestamp.
  - Phone STOP receive elapsed timestamp.
  - Phone START/STOP marking timestamps.

- [ ] Add command-send diagnostics.
  - Measure queue enqueue time.
  - Measure actual socket send completion time.
  - Keep current phone receive timestamps.
  - This tells us whether delay came from Tk scheduling, Hub send queue, USB/Wi-Fi transport, or phone processing.

- [ ] Consider priority send for lag-test START/STOP.
  - Current Hub uses a per-client send queue.
  - For lag tests, add a blocking/priority `send_to_client_now()` using the same socket lock.
  - Use it only for `START` and `STOP` timing commands.
  - Keep normal queued send for file/list/settings commands.

- [ ] Improve analyzer confidence after transport is stable.
  - Decode a small frame window around first/last frame.
  - Add verdicts like `transport_jitter`, `mux_cut_offset`, `analyzer_uncertain`, `ok`.
  - Keep this as phase two, because current evidence says command arrival jitter is the larger issue.

## Verification Plan

- [ ] Hub unit/smoke tests for ADB parsing.
  - No devices.
  - One authorized device.
  - Unauthorized device.
  - Offline device.
  - Multiple authorized devices.

- [ ] Manual ADB setup test.
  - Plug in one phone.
  - Enable USB debugging.
  - Start Hub.
  - Confirm Hub logs ADB device and reverse success.
  - Start Android app.
  - Confirm Android shows USB connection.
  - Confirm Hub connected-phone list shows USB.

- [ ] Manual command test over USB.
  - `SETTINGS_LIST`
  - `NAME`
  - `START`
  - `STOP`
  - `LIST`
  - Transfer one capture.

- [ ] Fallback test.
  - Start with USB connected.
  - Unplug cable.
  - Confirm Hub logs device removal.
  - Confirm Android reconnects over Wi-Fi if `allow_wifi_fallback=true`.

- [ ] Wi-Fi-only regression test.
  - Disable ADB or set mode to Wi-Fi only.
  - Confirm current discovery/control/transfer behavior still works.

- [ ] Lag-test comparison.
  - Same phone.
  - Same camera mode.
  - Same lighting.
  - Same display.
  - Run 10 lag tests over USB ADB reverse.
  - Run 10 lag tests over Wi-Fi.
  - Compare:
    - command timing warning count
    - phone START-to-STOP receive duration spread
    - start lag spread
    - stop lag spread

## Implementation Status

- [x] Phase 1: Hub ADB helper and reverse setup.
- [x] Phase 2: Android USB-first connection and `TRANSPORT` message.
- [x] Phase 3: Hub transport labeling and report metadata.
- [ ] Phase 4: Lag-test priority send and command-send diagnostics.
- [ ] Phase 5: Analyzer/report verdict improvements.

## Review

Implemented the ADB reverse transport slice.

- Hub now has `src/android_adb.py` for ADB lookup, `adb devices -l` parsing, reverse setup, and reverse cleanup.
- Hub config now has `phone_transport.mode = adb_reverse_first`, optional `adb_path`, and `adb_poll_interval_sec`.
- Hub starts an ADB monitor when phone networking starts, creates `adb reverse tcp:6000 tcp:6000` for authorized devices, logs unauthorized/offline states, and removes managed reverse mappings on disconnect/quit.
- Hub stores phone transport metadata from a new Android `TRANSPORT` message, shows USB/Wi-Fi in connected-phone labels, and writes `client_transport` into lag-test JSON/CSV reports.
- Android now tries `127.0.0.1:6000` first, reports `TRANSPORT usb_adb_reverse` when that works, and falls back to Wi-Fi discovery when USB is unavailable.

Verification completed:

- Hub Python compile passed with `.venv\Scripts\python.exe -m py_compile src\tcp_arduino_sync.py src\android_adb.py src\lag_test\report.py`.
- ADB parser smoke test passed.
- Local ADB was found at `C:\Users\MoCapLab\AppData\Local\Android\Sdk\platform-tools\adb.exe`.
- Android `.\gradlew.bat assembleDebug` passed when `JAVA_HOME` was set to `C:\Program Files\Android\Android Studio\jbr`; Gradle printed an SDK XML version warning but returned success.
- After USB debugging was authorized, phone `R3GYB09WECE` (`SM_S938B`) was visible as `device`.
- Debug APK was installed after uninstalling the prior differently signed package.
- ADB reverse `tcp:6000 tcp:6000` was created successfully.
- USB reverse handshake test passed with a temporary listener:
  - `HELLO S25U_2`
  - `TRANSPORT usb_adb_reverse host=127.0.0.1`
- Android app was launched and remained running on the phone.
- Follow-up inspection of the first USB lag tests showed command timing fixed but a consistent early video cut remained:
  - `client_transport=usb_adb_reverse`
  - command timing warnings were gone
  - phone START-to-STOP receive duration matched Hub timing within about 1 ms
  - start/stop lag was still about `-55` to `-75 ms`
- Root cause was the timestamped-input trigger mapping in `RollingVideoEncoder`: it mapped phone receive time through callback wall time, effectively subtracting camera pipeline delay from the requested cut.
- Patched timestamped-input trigger cuts to use the phone elapsed realtime clock directly.
- Rebuilt and installed the patched Android debug APK.
- Verified the patched app is running and connected to the real Hub over ADB reverse; port `6000` is owned by Hub `pythonw` with an established loopback connection.
- Follow-up lag tests after the timestamp mapping patch showed the large early offset was fixed.
  - New USB 60 fps runs were mostly `start_lag=-5..0 ms` and `stop_lag=-5..+5 ms`, with two start outliers at `-25 ms`.
  - Phone receive duration exactly matched requested segment duration in the segment JSON.
  - Segment cut offsets were small: start average about `+0.25 ms`, end average about `+10 ms` for the 60 fps runs.
  - Remaining CSV lag is dominated by visual timecode/display/camera-frame quantization rather than transport or encoder cut timing.
- README camera-lead wording was corrected: positive camera lead shifts the phone-side video cut later, so a repeatable negative lag-test offset can be compensated with a small positive lead.

# CaptureBridge Timing Sync, Jitter Measurement, and Camera Error Plan

## Question

Can we measure Wi-Fi/USB jitter and machine/phone clock offset so a jittery transport can still cut the video at the right time?

Yes. The right fix is not only "send faster". The Hub should estimate the phone clock, measure transport round-trip/jitter, then send scheduled phone-time capture commands. Because the Android recorder has a rolling buffer, the phone can cut at an exact phone-clock timestamp even if the control message arrives a bit early or late, as long as the requested timestamp is still inside the available buffer.

## Current Evidence

- [x] Latest USB ADB reverse lag tests no longer show large command timing errors.
- [x] The old `-55..-75 ms` offset was caused by timestamped camera input mapping, not transport jitter.
- [x] After the Android timestamp mapping patch, 60 fps USB lag tests are mostly around `start_lag=-5..0 ms` and `stop_lag=-5..+5 ms`, with a couple start outliers around `-25 ms`.
- [x] Android already stamps command receive time with `SystemClock.elapsedRealtimeNanos()`.
- [x] Android already reports `phone_rx_ns` and `phone_tx_ns` on `PONG`.
- [x] Hub currently sends lag-test `START` and `STOP` at Hub display times, and the phone cuts at the receive timestamp.
- [x] That means any late or jittery delivery becomes a cut-time error.

## Timing Design

- [ ] Add a formal time-sync protocol on the existing TCP control connection.
  - Hub sends:
    - `SYNC <seq> hub_tx_ns=<perf_counter_ns>`
  - Phone replies immediately:
    - `SYNC_OK <seq> hub_tx_ns=<same> phone_rx_ns=<elapsedRealtimeNanos at receive> phone_tx_ns=<elapsedRealtimeNanos at reply>`
  - Hub records `hub_rx_ns=<perf_counter_ns>` when the reply is received.

- [ ] Estimate transport delay and phone-clock offset NTP-style.
  - `hub_mid_ns = (hub_tx_ns + hub_rx_ns) / 2`
  - `phone_mid_ns = (phone_rx_ns + phone_tx_ns) / 2`
  - `offset_phone_minus_hub_ns = phone_mid_ns - hub_mid_ns`
  - `rtt_ns = (hub_rx_ns - hub_tx_ns) - (phone_tx_ns - phone_rx_ns)`
  - Use the lowest-RTT samples as the best offset estimate because they have the least queueing delay.

- [ ] Keep per-client sync quality stats.
  - Current offset.
  - Offset drift.
  - RTT min, median, p95, max.
  - Jitter as median absolute deviation or p95-min.
  - Sample count and age.
  - Transport label: `usb_adb_reverse`, `wifi`, or `direct`.

- [ ] Run sync automatically.
  - On client connect: burst of 20 samples.
  - Before each lag test: burst of 10 samples.
  - During idle connection: 1 sample per second or every 2 seconds.
  - Before scheduled capture: ensure last good sync is recent, for example `<5 s` old.

## Scheduled Cut Design

- [ ] Add scheduled protocol commands while keeping old commands for compatibility.
  - `START_AT <seq> phone_elapsed_ns=<target> capture=<name optional>`
  - `STOP_AT <seq> phone_elapsed_ns=<target>`
  - Phone replies:
    - `START_OK ... target_phone_elapsed_ns=<target> applied_phone_elapsed_ns=<actual>`
    - `STOP_MARKED ... target_phone_elapsed_ns=<target> applied_phone_elapsed_ns=<actual>`
    - `START_ERR LATE_OUT_OF_PREROLL late_by_ms=<x>` if the requested start is too far in the past.
    - `STOP_ERR TARGET_INVALID late_by_ms=<x>` only if the segment cannot be cut safely.

- [ ] Android implementation shape.
  - Parse `START_AT` and `STOP_AT` in `MainActivity.handleCommand`.
  - Pass the target elapsed timestamp to `CaptureCameraController.startRecording(...)` and `stopRecording(...)`.
  - In `RollingVideoEncoder`, use `target_elapsed_ns / 1000` as the requested presentation time for timestamped input.
  - If `START_AT` arrives after target but target is still within preroll, still cut at the target.
  - If `STOP_AT` arrives after target, still end the segment at the target if frames are already buffered/encoded and the segment is active.
  - Report `late_by_ms` and `target_vs_applied_ms` in every ACK.

- [ ] Hub implementation shape.
  - Add a `ClientTimeSync` object per TCP client.
  - Convert Hub display/capture target time to phone time:
    - `target_phone_ns = target_hub_perf_ns + offset_phone_minus_hub_ns`
  - Send `START_AT`/`STOP_AT` before the actual target by a configurable lead time.
  - Lead time should be based on measured jitter:
    - USB: likely `100-250 ms`.
    - Wi-Fi: likely `250-750 ms`, or `p95_rtt + safety_margin`.
  - If sync quality is bad, show a warning and either use larger lead time or block the lag test.

## Lag-Test Changes

- [ ] Before the lag-test display opens, run a sync burst and store sync stats in the session.
- [ ] Keep the visual display targets at 1000 ms and 2000 ms.
- [ ] Convert those display target times into phone elapsed timestamps.
- [ ] Send `START_AT` and `STOP_AT` early enough that transport jitter should not matter.
- [ ] Report both old and new timing fields:
  - Hub intended visual target.
  - Hub command send time.
  - Phone target time.
  - Phone receive time.
  - Phone applied cut time.
  - Command arrived early/late versus target.
  - Sync offset and RTT stats.
  - Transport.

## Capture Workflow Changes

- [ ] Use scheduled commands for normal captures too when possible.
  - Hardware/Arduino trigger happens on the Hub clock.
  - Hub maps the event time to phone elapsed time.
  - Hub sends `START_AT`/`STOP_AT`.
  - If the phone receives the command late but the rolling buffer still covers the target, the cut is still correct.

- [ ] Keep immediate `START`/`STOP` for manual fallback.
  - Immediate commands remain useful when sync has not been established.
  - The Hub should log that these captures are not jitter-compensated.

## Report and UI Changes

- [ ] Add a small Hub status line per phone:
  - `USB sync good: RTT 3.2 ms p95 5.0 ms offset stable 0.4 ms`
  - `Wi-Fi sync jittery: RTT p95 48 ms, using 300 ms command lead`
- [ ] Add sync stats to lag-test JSON and CSV.
- [ ] Add a lag-test verdict:
  - `transport_ok`
  - `transport_jitter_compensated`
  - `sync_quality_poor`
  - `phone_cut_late`
  - `analyzer_uncertain`

## Camera Error 4 Findings

- [x] App-side code path is `CaptureCameraController.openCamera(...).StateCallback.onError`.
- [x] Current behavior closes the camera and sets `Camera error <number>`.
- [x] Android Camera2 error `4` is `ERROR_CAMERA_DEVICE`, a fatal camera device/HAL error.
- [x] `dumpsys media.camera` contains a CameraService error trace for this app:
  - Device status: `ERROR`
  - Cause: `processCaptureResult: Unknown frame number for capture result: 284624`
- [x] CameraService history also shows repeated app camera-client deaths/disconnects around reinstall/relaunch cycles.
- [x] Current logs do not show a clean app stack trace for a crash at the camera error; the strongest evidence points to a Camera2/HAL device error surfaced to the app.

## Camera Error Recovery Plan

- [ ] Replace plain `Camera error 4` with descriptive names.
  - `1 = ERROR_CAMERA_IN_USE`
  - `2 = ERROR_MAX_CAMERAS_IN_USE`
  - `3 = ERROR_CAMERA_DISABLED`
  - `4 = ERROR_CAMERA_DEVICE`
  - `5 = ERROR_CAMERA_SERVICE`

- [ ] Add structured Android logcat messages.
  - Tag: `CaptureBridgeCamera`
  - Log camera id, selected resolution, selected fps, high-speed mode, armed/recording state, and current transport.
  - Log every camera open, disconnect, error, session configure failure, encoder arm failure, and recovery attempt.

- [ ] Add safe recovery for `ERROR_CAMERA_DEVICE` when not recording.
  - Close camera and sessions.
  - Release/recreate encoder surfaces if needed.
  - Wait 500-1000 ms.
  - Reopen the selected camera.
  - Recreate idle/high-speed session.
  - Re-arm rolling encoder.
  - Send `READY_ERR CAMERA_DEVICE_RECOVERING` first, then `READY` or `PREPARE_OK READY` after recovery.

- [ ] During active recording, fail loudly and preserve data.
  - Stop using the broken camera session.
  - Try to finalize the current segment if the encoder still has valid frames.
  - Send `STOP_ERR CAMERA_DEVICE` or `READY_ERR CAMERA_DEVICE`.
  - Do not silently mark the capture as successful.

- [ ] Hub-side handling.
  - Treat `READY_ERR CAMERA_DEVICE...` as a phone-camera fault, not a transport fault.
  - Show a clear message in the log and status area.
  - Block lag tests until the phone sends a fresh `PREPARE_OK READY`.

## Verification Plan

- [ ] Unit-test Hub sync math with synthetic samples:
  - symmetric low RTT
  - asymmetric high RTT
  - jitter spike
  - stale samples
- [ ] Android parser smoke test for `SYNC`, `START_AT`, and `STOP_AT`.
- [ ] Manual USB test:
  - 50 sync samples over ADB reverse.
  - Confirm low RTT and stable offset.
  - Run 10 scheduled lag tests.
- [ ] Manual Wi-Fi test:
  - 50 sync samples over Wi-Fi.
  - Confirm RTT/jitter is visible in the report.
  - Run 10 scheduled lag tests.
  - Compare against old immediate-command lag tests.
- [ ] Artificial jitter test:
  - Add a temporary debug send delay or Android receive delay.
  - Confirm scheduled cuts still land correctly when commands arrive within rolling-buffer/preroll limits.
- [ ] Camera recovery test:
  - Reopen app repeatedly after reinstall/force-stop.
  - Switch camera modes.
  - Confirm no plain `Camera error 4` remains; logs must show named error and recovery outcome.

## Implementation Review

- [x] Added Hub-side `ClientTimeSync` state per TCP client.
- [x] Added `SYNC` / `SYNC_OK` protocol support.
- [x] Hub timestamps sync packets at actual socket-send time, so send-queue delay does not pollute RTT.
- [x] Hub now runs a sync burst when a phone connects and another burst before a lag-test countdown.
- [x] Hub estimates clock offset, RTT min/median/p95/max, offset jitter, sample age, and a jitter-based scheduled-command lead.
- [x] Lag tests use scheduled `START_AT` / `STOP_AT` when sync quality is usable.
- [x] Lag-test display phase changes remain fixed at the visual 1000 ms and 2000 ms targets.
- [x] `START_AT` is sent early and cuts at the target phone elapsed timestamp.
- [x] `STOP_AT` is sent early but Android waits until the target phone elapsed timestamp before finalizing, so future frames are not missing.
- [x] Lag-test reports include sync summary, scheduled command lead, target phone timestamps, and ACK diagnostic fields.
- [x] Android parses `SYNC`, `START_AT`, and `STOP_AT`.
- [x] Android ACKs now include target timestamp, receive-vs-target delta, and handler-vs-target delta.
- [x] Camera2 error handling now logs structured `CaptureBridgeCamera` entries, maps error numbers to names, and auto-reopens/rearms after idle camera-device errors.

Verification completed:

- [x] Hub compile passed:
  - `.venv\Scripts\python.exe -m py_compile src\tcp_arduino_sync.py src\android_adb.py src\lag_test\report.py`
- [x] Android debug build passed:
  - `JAVA_HOME=C:\Program Files\Android\Android Studio\jbr`
  - `.\gradlew.bat assembleDebug`
- [x] Debug APK installed successfully on `R3GYB09WECE`.
- [x] Android app launched successfully.
- [x] Logcat showed structured camera startup logs:
  - `Opening camera id=0 ...`
  - `Camera opened id=0`
- [x] Live ADB reverse protocol smoke test passed:
  - Phone connected to temporary listener over `127.0.0.1:6000`.
  - Phone sent `HELLO S25U_2`.
  - Phone sent `TRANSPORT usb_adb_reverse host=127.0.0.1`.
  - Phone replied to `SYNC` with `SYNC_OK seq=1 ... phone_rx_ns=... phone_tx_ns=...`.
- [x] Hub sync math smoke test passed with synthetic timestamps.
