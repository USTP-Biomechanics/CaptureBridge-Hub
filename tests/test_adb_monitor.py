import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import android_adb  # noqa: E402
import tcp_arduino_sync  # noqa: E402
from android_adb import AdbCommandResult  # noqa: E402
from tcp_arduino_sync import (  # noqa: E402
    ADB_MISSING_STATUS,
    ADB_REDISCOVERY_INTERVAL_SEC,
    App,
)


ADB_PATH = "C:\\Android\\Sdk\\platform-tools\\adb.exe"
OTHER_ADB_PATH = "C:\\Program Files (x86)\\Android\\android-sdk\\platform-tools\\adb.exe"
SERIAL = "R3GYB09WBBP"
LABEL = f"SM_S938B ({SERIAL})"
BIND_ERROR = "adb.exe: error: cannot bind listener"
BOTH_REVERSES = {("tcp:6000", "tcp:6000"), ("tcp:6101", "tcp:6101")}


class FakeAdb:
    """adb.exe and its server: installed executables, devices, and reverse listeners."""

    def __init__(self):
        self.installed = [ADB_PATH]
        self.devices = {SERIAL: "device"}
        self.reverses = {SERIAL: set()}
        self.failing_ports = set()
        self.before_reverse = None
        self.commands = []
        self.find_calls = 0

    def find_adb_exe(self, configured_path="", app_root=""):
        self.find_calls += 1
        return self.installed[0] if self.installed else None

    def adb_executable_missing(self, adb_path):
        return adb_path not in self.installed

    def restart_server(self):
        for listeners in self.reverses.values():
            listeners.clear()

    def run(self, adb_path, args, timeout_sec):
        args = tuple(args)
        self.commands.append(args)
        if adb_path not in self.installed:
            return AdbCommandResult(ok=False, error="[WinError 2] The system cannot find the file specified")
        if args == ("devices", "-l"):
            lines = ["List of devices attached"]
            for serial, state in self.devices.items():
                lines.append(
                    f"{serial}            {state} product:pa3qxeea model:SM_S938B device:pa3q transport_id:1"
                )
            return AdbCommandResult(ok=True, stdout="\n".join(lines))

        listeners = self.reverses.setdefault(args[1], set())
        if args[2:] == ("reverse", "--list"):
            lines = [f"UsbFfs {device} {host}" for device, host in sorted(listeners)]
            return AdbCommandResult(ok=True, stdout="\n".join(lines))
        if args[2:4] == ("reverse", "--remove"):
            matching = {listener for listener in listeners if listener[0] == args[4]}
            if not matching:
                return AdbCommandResult(ok=False, error=f"adb.exe: error: listener '{args[4]}' not found")
            listeners -= matching
            return AdbCommandResult(ok=True)

        device_spec, host_spec = args[3], args[4]
        if self.before_reverse is not None:
            self.before_reverse()
        if int(device_spec.split(":")[1]) in self.failing_ports:
            return AdbCommandResult(ok=False, error=BIND_ERROR)
        listeners -= {listener for listener in listeners if listener[0] == device_spec}
        listeners.add((device_spec, host_spec))
        return AdbCommandResult(ok=True, stdout=device_spec.split(":")[1])

    def reverse_commands(self, action=None):
        commands = [command for command in self.commands if command[2:3] == ("reverse",)]
        if action == "add":
            return [command for command in commands if command[3] not in ("--list", "--remove")]
        if action is not None:
            return [command for command in commands if command[3] == action]
        return commands


class _InstantEvent:
    """Stop event whose wait() returns at once, so the monitor loop runs without delays."""

    def __init__(self):
        self._set = False
        self.waits = []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


class _ClosedRoot:
    """Tk root after mainloop has exited, as seen from a worker thread."""

    def __init__(self):
        self.after_calls = 0

    def after(self, _delay_ms, _callback):
        self.after_calls += 1
        raise RuntimeError("main thread is not in main loop")


class AdbMonitorTests(unittest.TestCase):
    def setUp(self):
        self.adb = FakeAdb()
        self.now = 1000.0
        for patcher in (
            mock.patch.object(android_adb, "_run_adb", self.adb.run),
            mock.patch.object(android_adb, "find_adb_exe", self.adb.find_adb_exe),
            mock.patch.object(android_adb, "adb_executable_missing", self.adb.adb_executable_missing),
            mock.patch("tcp_arduino_sync.time.monotonic", lambda: self.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.logs = []
        self.app = App.__new__(App)
        self.app.phone_transport_config = {
            "mode": "adb_reverse_first",
            "adb_path": "",
            "adb_poll_interval_sec": 3,
        }
        self.app.phone_stream_enabled = True
        self.app.phone_stream_config = SimpleNamespace(udp_port=6101)
        self.app.adb_path = None
        self.app.adb_ready_serials = set()
        self.app.adb_reverse_serials = set()
        self.app.adb_reverse_ports_by_serial = {}
        self.app.adb_status_text = "ADB not checked"
        self.app._adb_monitor_stop_event = threading.Event()
        self.app._adb_monitor_thread = None
        self.app._adb_last_log_state = None
        self.app._adb_last_search_time = None
        self.app._adb_reverse_errors_by_serial = {}
        self.app.log = self.logs.append
        self.app.update_phone_connection_ui = lambda error_text="": None

    def _logged(self, prefix):
        return [line for line in self.logs if line.startswith(prefix)]

    def test_reverse_is_restored_after_adb_server_restart(self):
        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.reverses[SERIAL], BOTH_REVERSES)
        self.assertIn(f"ADB found: {ADB_PATH}", self.logs)
        self.assertIn(f"ADB reverse ready for {LABEL} on tcp:6000, tcp:6101", self.logs)
        self.assertEqual(self.app.adb_reverse_serials, {SERIAL})

        self.adb.restart_server()
        self.app._refresh_adb_reverses()
        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.reverses[SERIAL], BOTH_REVERSES)
        self.assertEqual(
            self._logged("ADB reverse restored"),
            [f"ADB reverse restored for {LABEL} after adb restart/replug"],
        )
        self.assertEqual(len(self._logged("ADB reverse ready")), 1)
        self.assertEqual(len(self.adb.reverse_commands("add")), 4)
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {SERIAL: {6000, 6101}})
        self.assertEqual(self.app.adb_status_text, "ADB: 1 ready, USB reverse 1/1")

    def test_existing_reverses_are_verified_without_adding_them_again(self):
        self.adb.reverses[SERIAL] = set(BOTH_REVERSES)

        self.app._refresh_adb_reverses()
        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.reverse_commands("add"), [])
        self.assertEqual(
            self._logged("ADB reverse ready"),
            [f"ADB reverse ready for {LABEL} on tcp:6000, tcp:6101"],
        )
        self.assertEqual(self.app.adb_status_text, "ADB: 1 ready, USB reverse 1/1")

    def test_status_reflects_verified_reverse_state(self):
        self.adb.failing_ports = {6101}
        self.app._refresh_adb_reverses()
        self.app._refresh_adb_reverses()

        self.assertEqual(
            self.app.adb_status_text,
            f"ADB: 1 ready, USB reverse 1/1; reverse failed: tcp:6101: {BIND_ERROR}",
        )
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {SERIAL: {6000}})
        self.assertEqual(
            self._logged("ADB reverse failed"),
            [f"ADB reverse failed for {LABEL}: tcp:6101: {BIND_ERROR}"],
        )
        self.assertEqual(self._logged("ADB reverse ready"), [f"ADB reverse ready for {LABEL} on tcp:6000"])

        self.adb.failing_ports = {6000, 6101}
        self.adb.restart_server()
        self.app._refresh_adb_reverses()

        self.assertEqual(
            self.app.adb_status_text,
            f"ADB: 1 ready, USB reverse 0/1; reverse failed: tcp:6000: {BIND_ERROR}",
        )
        self.assertEqual(self.app.adb_reverse_serials, set())
        self.assertEqual(self.app.adb_ready_serials, {SERIAL})

    def test_unauthorized_phone_gets_no_reverse_and_a_hint(self):
        self.adb.devices = {SERIAL: "unauthorized"}

        self.app._refresh_adb_reverses()

        self.assertEqual(self.app.adb_status_text, "ADB: 1 unauthorized; authorize USB debugging on phone")
        self.assertEqual(self.adb.reverse_commands(), [])
        self.assertEqual(self.app.adb_ready_serials, set())

    def test_unplugged_phone_is_dropped_without_removing_its_reverses(self):
        self.app._refresh_adb_reverses()
        self.adb.devices = {}

        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.reverse_commands("--remove"), [])
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {})
        self.assertEqual(self.app.adb_reverse_serials, set())
        self.assertEqual(self.app.adb_status_text, "ADB: no USB phones")

    def test_missing_adb_is_rediscovered_after_throttle(self):
        self.adb.installed = []
        self.app._refresh_adb_reverses()

        self.assertEqual(self.app.adb_status_text, ADB_MISSING_STATUS)
        self.assertEqual(self.adb.find_calls, 1)

        self.adb.installed = [ADB_PATH]
        self.now += ADB_REDISCOVERY_INTERVAL_SEC - 1
        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.find_calls, 1)
        self.assertEqual(self.app.adb_status_text, ADB_MISSING_STATUS)

        self.now += 1
        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.find_calls, 2)
        self.assertIn(f"ADB found: {ADB_PATH}", self.logs)
        self.assertEqual(self.adb.reverses[SERIAL], BOTH_REVERSES)
        self.assertEqual(self.app.adb_status_text, "ADB: 1 ready, USB reverse 1/1")
        self.assertEqual(self.logs.count(ADB_MISSING_STATUS), 1)

    def test_vanished_adb_triggers_immediate_search(self):
        self.app._refresh_adb_reverses()
        self.adb.installed = [OTHER_ADB_PATH]

        self.app._refresh_adb_reverses()

        self.assertEqual(self.adb.find_calls, 2)
        self.assertEqual(self.app.adb_path, OTHER_ADB_PATH)
        self.assertIn(f"ADB executable disappeared: {ADB_PATH}; searching again", self.logs)
        self.assertIn(f"ADB found: {OTHER_ADB_PATH}", self.logs)
        self.assertEqual(self.app.adb_status_text, "ADB: 1 ready, USB reverse 1/1")

    def test_device_check_failure_from_vanished_adb_searches_next_poll(self):
        self.app._refresh_adb_reverses()
        self.adb.installed = []
        # adb.exe disappears between the start-of-poll check and "adb devices".
        with mock.patch.object(android_adb, "adb_executable_missing", side_effect=[False, True]):
            self.app._refresh_adb_reverses()

        self.assertTrue(self.app.adb_status_text.startswith("ADB device check failed: [WinError 2]"))
        self.assertIsNone(self.app.adb_path)

        self.adb.installed = [OTHER_ADB_PATH]
        self.now += ADB_REDISCOVERY_INTERVAL_SEC
        self.app._refresh_adb_reverses()

        self.assertEqual(self.app.adb_path, OTHER_ADB_PATH)
        self.assertEqual(self.app.adb_status_text, "ADB: 1 ready, USB reverse 1/1")

    def test_stop_removes_exactly_the_managed_ports(self):
        self.adb.reverses[SERIAL] = {("tcp:8081", "tcp:8081")}
        self.app._refresh_adb_reverses()

        self.app.stop_adb_monitor()

        self.assertEqual(
            self.adb.reverse_commands("--remove"),
            [
                ("-s", SERIAL, "reverse", "--remove", "tcp:6000"),
                ("-s", SERIAL, "reverse", "--remove", "tcp:6101"),
            ],
        )
        self.assertEqual(self.adb.reverses[SERIAL], {("tcp:8081", "tcp:8081")})
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {})
        self.assertEqual(self.app.adb_reverse_serials, set())
        self.assertEqual(self.app.adb_ready_serials, set())
        self.assertEqual(self.app.adb_status_text, "ADB stopped")

    def test_stop_during_reverse_setup_removes_ports_this_poll_created(self):
        stop_event = threading.Event()
        self.app._adb_monitor_stop_event = stop_event
        self.adb.devices = {SERIAL: "device", "SECOND": "device"}
        self.adb.reverses["SECOND"] = set()
        setups = []

        def stop_on_second_phone():
            setups.append(True)
            if len(setups) == 3:
                stop_event.set()

        self.adb.before_reverse = stop_on_second_phone

        self.app._refresh_adb_reverses(stop_event)

        self.assertEqual(self.adb.reverses, {SERIAL: set(), "SECOND": set()})
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {})
        self.assertEqual(self.app.adb_status_text, "ADB not checked")

    def test_stop_while_poll_waits_for_ui_thread_leaves_no_reverses_or_state(self):
        stop_event = threading.Event()
        self.app._adb_monitor_stop_event = stop_event

        def log_then_disconnect(line):
            self.logs.append(line)
            # The poll is blocked handing this line to the UI thread, so the
            # join in stop_adb_monitor times out and its cleanup runs first.
            if line.startswith("ADB reverse ready"):
                self.app.stop_adb_monitor()

        self.app.log = log_then_disconnect

        self.app._refresh_adb_reverses(stop_event)

        self.assertEqual(self.adb.reverses[SERIAL], set())
        self.assertEqual(self.app.adb_reverse_ports_by_serial, {})
        self.assertEqual(self.app.adb_reverse_serials, set())
        self.assertEqual(self.app.adb_ready_serials, set())
        self.assertEqual(self.app.adb_status_text, "ADB stopped")

    def test_stop_during_device_check_adds_no_reverses(self):
        stop_event = threading.Event()
        run = self.adb.run

        def stop_during_device_check(adb_path, args, timeout_sec):
            if tuple(args) == ("devices", "-l"):
                stop_event.set()
            return run(adb_path, args, timeout_sec)

        with mock.patch.object(android_adb, "_run_adb", stop_during_device_check):
            self.app._refresh_adb_reverses(stop_event)

        self.assertEqual(self.adb.reverse_commands(), [])
        self.assertEqual(self.app.adb_ready_serials, set())

    def test_configured_bare_name_on_path_is_not_reported_unusable(self):
        self.app.phone_transport_config["adb_path"] = "adb.exe"

        with mock.patch.object(android_adb.shutil, "which", return_value=ADB_PATH):
            self.app._refresh_adb_reverses()

        self.assertIn(f"ADB found: {ADB_PATH}", self.logs)
        self.assertEqual(self._logged("Config phone_transport.adb_path"), [])

    def test_unusable_configured_path_is_reported_once_adb_is_found(self):
        self.app.phone_transport_config["adb_path"] = "C:\\missing\\adb.exe"

        self.app._refresh_adb_reverses()

        self.assertEqual(
            self._logged("Config phone_transport.adb_path"),
            ["Config phone_transport.adb_path is not usable: C:\\missing\\adb.exe"],
        )
        self.assertIn(f"ADB found: {ADB_PATH}", self.logs)

    def test_monitor_loop_survives_refresh_exception(self):
        stop_event = _InstantEvent()
        calls = []

        def refresh(event):
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError("boom")
            event.set()

        self.app._refresh_adb_reverses = refresh

        self.app._adb_monitor_loop(stop_event)

        self.assertEqual(calls, [stop_event, stop_event])
        self.assertIn("ADB monitor error: boom", self.logs)
        self.assertEqual(stop_event.waits, [3, 3])

    def test_each_start_uses_a_new_stop_event(self):
        received = []

        def loop(stop_event):
            received.append(stop_event)
            stop_event.wait(5.0)

        self.app._adb_monitor_loop = loop

        self.app.start_adb_monitor()
        self.assertEqual(self.app.adb_status_text, "ADB: searching for adb.exe...")
        first_event = self.app._adb_monitor_stop_event
        self.app.stop_adb_monitor()
        self.app.start_adb_monitor()
        second_event = self.app._adb_monitor_stop_event
        self.app.stop_adb_monitor()

        self.assertEqual(received, [first_event, second_event])
        self.assertIsNot(first_event, second_event)
        self.assertTrue(first_event.is_set())
        self.assertEqual(self.adb.find_calls, 0)

    def test_wifi_only_mode_does_not_start_monitor(self):
        self.app.phone_transport_config["mode"] = "wifi_only"

        self.app.start_adb_monitor()

        self.assertIsNone(self.app._adb_monitor_thread)
        self.assertEqual(self.app.adb_status_text, "ADB disabled; Wi-Fi only")


class UiThreadHandoffTests(unittest.TestCase):
    def test_worker_calls_after_mainloop_exit_do_not_raise(self):
        app = App.__new__(App)
        app.root = _ClosedRoot()
        errors = []

        def worker():
            try:
                app.log("late message")
                app._call_on_ui_thread(lambda: None)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(app.root.after_calls, 2)


class _FakeTcpServer:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class ConnectPhoneNetworkTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.adb_starts = []
        self.app = App.__new__(App)
        self.app.tcp = None
        self.app.discovery = None
        self.app.phone_connection_enabled = False
        self.app.is_running = False
        self.app._lag_test_session = None
        self.app.log = self.logs.append
        self.app.update_phone_connection_ui = lambda error_text="": None
        self.app.start_adb_monitor = lambda: self.adb_starts.append(True)
        self.app.stop_adb_monitor = lambda: None
        self.app.broadcast_generated_name_on_change = lambda: None
        for name in ("on_clients_changed", "on_client_metadata_changed", "on_client_message",
                     "on_transfer_progress", "get_save_dir"):
            setattr(self.app, name, lambda *_args: None)

    def test_usb_starts_when_discovery_port_is_unavailable(self):
        def busy_discovery(**_kwargs):
            raise OSError("[WinError 10048] address already in use")

        with mock.patch.object(tcp_arduino_sync, "TcpServer", _FakeTcpServer), \
                mock.patch.object(tcp_arduino_sync, "UdpDiscoveryResponder", busy_discovery):
            connected = self.app.connect_phone_network()

        self.assertTrue(connected)
        self.assertIsInstance(self.app.tcp, _FakeTcpServer)
        self.assertIsNone(self.app.discovery)
        self.assertEqual(self.adb_starts, [True])
        self.assertIn(
            "Wi-Fi discovery unavailable: [WinError 10048] address already in use",
            self.logs,
        )

        tcp = self.app.tcp
        self.assertTrue(self.app.disconnect_phone_network())
        self.assertTrue(tcp.closed)
        self.assertIsNone(self.app.tcp)

    def test_tcp_server_failure_is_still_fatal(self):
        def busy_tcp(**_kwargs):
            raise OSError("[WinError 10048] address already in use")

        with mock.patch.object(tcp_arduino_sync, "TcpServer", busy_tcp):
            connected = self.app.connect_phone_network()

        self.assertFalse(connected)
        self.assertIsNone(self.app.tcp)
        self.assertEqual(self.adb_starts, [])
        self.assertIn("Phone connection could not start: [WinError 10048] address already in use", self.logs)


if __name__ == "__main__":
    unittest.main()
