import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import android_adb  # noqa: E402
from android_adb import AdbCommandResult, AdbDevice, ReverseSync  # noqa: E402


SAMSUNG_DEVICES_OUTPUT = (
    "List of devices attached\r\n"
    "R3GYB09WBBP            device product:pa3qxeea model:SM_S938B device:pa3q transport_id:1\r\n"
    "\r\n"
)
UNITY_ADB_PARTS = (
    "Editor", "Data", "PlaybackEngines", "AndroidPlayer", "SDK", "platform-tools", "adb.exe",
)


def _version_output(protocol: int) -> str:
    return f"Android Debug Bridge version 1.0.{protocol}\nVersion 34.0.5-10900879"


def _normcase_all(paths):
    return [os.path.normcase(path) for path in paths]


class ParseAdbOutputTests(unittest.TestCase):
    def test_parses_real_samsung_devices_output(self):
        self.assertEqual(
            android_adb.parse_devices(SAMSUNG_DEVICES_OUTPUT),
            [
                AdbDevice(
                    serial="R3GYB09WBBP",
                    state="device",
                    product="pa3qxeea",
                    model="SM_S938B",
                    device="pa3q",
                    transport_id="1",
                )
            ],
        )

    def test_skips_daemon_banner_lines(self):
        output = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            + SAMSUNG_DEVICES_OUTPUT
        )

        self.assertEqual(
            [device.serial for device in android_adb.parse_devices(output)],
            ["R3GYB09WBBP"],
        )

    def test_parse_reverse_list_handles_crlf_blank_lines_and_other_host_ports(self):
        output = "UsbFfs tcp:6000 tcp:6000\r\nUsbFfs tcp:6101 tcp:7101\r\n\r\n"

        self.assertEqual(
            android_adb.parse_reverse_list(output),
            {("tcp:6000", "tcp:6000"), ("tcp:6101", "tcp:7101")},
        )
        self.assertEqual(android_adb.parse_reverse_list(""), set())


class _FakeReverseAdb:
    def __init__(self, listed="", listing_ok=True, failing_ports=()):
        self.listed = listed
        self.listing_ok = listing_ok
        self.failing_ports = set(failing_ports)
        self.commands = []

    def run(self, _adb_path, args, timeout_sec):
        args = tuple(args)
        self.commands.append(args)
        if args[2:] == ("reverse", "--list"):
            if not self.listing_ok:
                return AdbCommandResult(ok=False, error="timed out")
            return AdbCommandResult(ok=True, stdout=self.listed)
        port = int(args[-1].split(":")[1])
        if port in self.failing_ports:
            return AdbCommandResult(ok=False, error="adb.exe: error: cannot bind listener")
        return AdbCommandResult(ok=True, stdout=str(port))

    def setup_commands(self):
        return [command for command in self.commands if command[2:] != ("reverse", "--list")]


class EnsureReversesTests(unittest.TestCase):
    def _ensure(self, fake, ports=(6000, 6101)):
        with mock.patch.object(android_adb, "_run_adb", fake.run):
            return android_adb.ensure_reverses("adb.exe", "R3GYB09WBBP", ports)

    def test_listed_reverses_need_no_adb_reverse_calls(self):
        fake = _FakeReverseAdb(listed="UsbFfs tcp:6000 tcp:6000\nUsbFfs tcp:6101 tcp:6101\n")

        sync = self._ensure(fake)

        self.assertEqual(sync, ReverseSync(frozenset({6000, 6101}), (), ()))
        self.assertEqual(fake.commands, [("-s", "R3GYB09WBBP", "reverse", "--list")])

    def test_only_missing_ports_are_added(self):
        fake = _FakeReverseAdb(listed="UsbFfs tcp:6000 tcp:6000\nUsbFfs tcp:6101 tcp:7101\n")

        sync = self._ensure(fake)

        self.assertEqual(sync, ReverseSync(frozenset({6000, 6101}), (6101,), ()))
        self.assertEqual(
            fake.setup_commands(),
            [("-s", "R3GYB09WBBP", "reverse", "tcp:6101", "tcp:6101")],
        )

    def test_control_port_stays_active_when_preview_port_fails(self):
        fake = _FakeReverseAdb(failing_ports={6101})

        sync = self._ensure(fake)

        self.assertEqual(sync.active_ports, frozenset({6000}))
        self.assertEqual(sync.created_ports, (6000,))
        self.assertEqual(sync.errors, ((6101, "adb.exe: error: cannot bind listener"),))

    def test_failed_listing_is_treated_as_empty(self):
        fake = _FakeReverseAdb(listing_ok=False)

        sync = self._ensure(fake)

        self.assertEqual(sync, ReverseSync(frozenset({6000, 6101}), (6000, 6101), ()))


class AdbCandidatePathsTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

    def _touch(self, *parts, mtime=None) -> str:
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return str(path)

    def test_lists_sdk_locations_in_order_without_duplicates(self):
        program_files = str(self.root / "Program Files")
        program_files_x86 = str(self.root / "Program Files (x86)")
        sdk = str(self.root / "Local" / "Android" / "Sdk")
        older_unity = self._touch(
            "Program Files", "Unity", "Hub", "Editor", "2021.3.0f1", *UNITY_ADB_PARTS, mtime=1_000_000
        )
        tied_unity = self._touch(
            "Program Files", "Unity", "Hub", "Editor", "2022.3.1f1", *UNITY_ADB_PARTS, mtime=1_000_000
        )
        newest_unity = self._touch(
            "Program Files", "Unity", "Hub", "Editor", "6000.2.6f2", *UNITY_ADB_PARTS, mtime=2_000_000
        )
        path_adb = self._touch("bin", "adb.exe")
        environ = {
            "ANDROID_HOME": str(self.root / "android-home"),
            "ANDROID_SDK_ROOT": sdk.upper(),
            "LOCALAPPDATA": str(self.root / "Local"),
            "USERPROFILE": str(self.root / "User"),
            "PATH": str(self.root / "bin"),
            "ProgramFiles(x86)": program_files_x86,
            "ProgramFiles": program_files,
            "ProgramW6432": program_files,
        }
        app_root = str(self.root / "Hub" / "app")

        candidates = android_adb.adb_candidate_paths("", app_root, environ)

        expected = [
            os.path.join(app_root, "platform-tools", "adb.exe"),
            str(self.root / "Hub" / "platform-tools" / "adb.exe"),
            str(self.root / "android-home" / "platform-tools" / "adb.exe"),
            str(self.root / "android-home" / "platform-tools" / "adb"),
            os.path.join(sdk, "platform-tools", "adb.exe"),
            os.path.join(sdk, "platform-tools", "adb"),
            path_adb,
            os.path.join(program_files_x86, "Android", "android-sdk", "platform-tools", "adb.exe"),
            os.path.join(program_files, "Android", "android-sdk", "platform-tools", "adb.exe"),
            newest_unity,
            tied_unity,
            older_unity,
            "adb.exe",
            "adb",
        ]
        self.assertEqual(_normcase_all(candidates), _normcase_all(expected))
        self.assertTrue(all(os.path.isabs(path) for path in candidates[:-2]))

    def test_user_profile_is_only_a_fallback_for_local_app_data(self):
        user_sdk_adb = str(
            self.root / "User" / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        )

        without_local = android_adb.adb_candidate_paths(
            "", "", {"USERPROFILE": str(self.root / "User")}
        )
        with_local = android_adb.adb_candidate_paths(
            "", "", {"USERPROFILE": str(self.root / "User"), "LOCALAPPDATA": str(self.root / "Local")}
        )

        self.assertEqual(without_local, [user_sdk_adb, "adb.exe", "adb"])
        self.assertNotIn(user_sdk_adb, with_local)

    def test_configured_file_is_used_as_is(self):
        configured = str(self.root / "custom" / "adb.exe")

        candidates = android_adb.adb_candidate_paths(configured, "", {})

        self.assertEqual(candidates, [configured, "adb.exe", "adb"])

    def test_configured_folder_resolves_relative_to_app_root(self):
        (self.root / "tools").mkdir()
        expected = [
            str(self.root / "tools" / "adb.exe"),
            str(self.root / "tools" / "platform-tools" / "adb.exe"),
        ]

        relative = android_adb.adb_candidate_paths("tools", str(self.root), {})
        with mock.patch.dict(os.environ, {"CAPTUREBRIDGE_TEST_ADB_DIR": str(self.root / "tools")}):
            from_variable = android_adb.adb_candidate_paths("%CAPTUREBRIDGE_TEST_ADB_DIR%", "", {})

        self.assertEqual(relative[:2], expected)
        self.assertEqual(from_variable[:2], expected)

    def test_configured_bare_name_also_means_the_adb_on_path(self):
        on_path = self._touch("bin", "adb.exe")
        app_root = str(self.root / "app")

        with mock.patch.object(android_adb.shutil, "which", return_value=on_path) as which:
            paths = android_adb.configured_adb_paths("adb.exe", app_root)

        self.assertEqual(paths, [os.path.join(app_root, "adb.exe"), on_path])
        which.assert_called_once_with("adb.exe")

    def test_blank_configured_path_adds_nothing(self):
        self.assertEqual(android_adb.adb_candidate_paths("   ", "", {}), ["adb.exe", "adb"])


class FindAdbExeTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

    def _touch(self, *parts) -> str:
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return str(path)

    def _find(self, candidates, results, configured_path="configured"):
        spawned = []

        def fake_run(adb_path, args, timeout_sec):
            spawned.append((adb_path, tuple(args)))
            return results[adb_path]

        with mock.patch.object(android_adb, "adb_candidate_paths", return_value=candidates), \
                mock.patch.object(android_adb, "_run_adb", side_effect=fake_run):
            found = android_adb.find_adb_exe(configured_path, "root")
        return found, spawned

    def test_skips_missing_paths_without_spawning_and_prefers_protocol_41(self):
        missing = str(self.root / "missing" / "adb.exe")
        old = self._touch("old", "adb.exe")
        current = self._touch("current", "adb.exe")

        found, spawned = self._find(
            [missing, old, current],
            {
                old: AdbCommandResult(ok=True, stdout=_version_output(40)),
                current: AdbCommandResult(ok=True, stdout=_version_output(41)),
            },
        )

        self.assertEqual(found, current)
        self.assertEqual(spawned, [(old, ("version",)), (current, ("version",))])

    def test_falls_back_to_first_working_adb_without_protocol_41(self):
        broken = self._touch("broken", "adb.exe")
        oldest = self._touch("oldest", "adb.exe")
        old = self._touch("old", "adb.exe")

        found, _spawned = self._find(
            [broken, oldest, old],
            {
                broken: AdbCommandResult(ok=False, error="boom"),
                oldest: AdbCommandResult(ok=True, stdout=_version_output(39)),
                old: AdbCommandResult(ok=True, stdout=_version_output(40)),
            },
        )

        self.assertEqual(found, oldest)

    def test_configured_adb_is_used_whatever_its_version(self):
        configured = self._touch("configured", "adb.exe")
        current = self._touch("current", "adb.exe")

        found, spawned = self._find(
            [configured, current],
            {
                configured: AdbCommandResult(ok=True, stdout=_version_output(40)),
                current: AdbCommandResult(ok=True, stdout=_version_output(41)),
            },
            configured_path=configured,
        )

        self.assertEqual(found, configured)
        self.assertEqual(spawned, [(configured, ("version",))])

    def test_returns_none_when_no_candidate_runs(self):
        found, spawned = self._find(
            [str(self.root / "missing.exe"), "adb.exe"],
            {"adb.exe": AdbCommandResult(ok=False, error="[WinError 2] not found")},
        )

        self.assertIsNone(found)
        self.assertEqual(spawned, [("adb.exe", ("version",))])

    def test_bare_name_is_resolved_on_path(self):
        on_path = self._touch("bin", "adb.exe")

        with mock.patch.object(android_adb.shutil, "which", return_value=on_path):
            found, _spawned = self._find(
                ["adb.exe"],
                {"adb.exe": AdbCommandResult(ok=True, stdout=_version_output(41))},
            )

        self.assertEqual(found, on_path)

    def test_bare_name_outside_path_uses_installed_location(self):
        output = _version_output(41) + "\r\nInstalled as C:\\Program Files\\Tools\\adb.exe\r\nRunning on Windows"

        with mock.patch.object(android_adb.shutil, "which", return_value=None):
            found, _spawned = self._find(
                ["adb.exe"],
                {"adb.exe": AdbCommandResult(ok=True, stdout=output)},
            )

        self.assertEqual(found, "C:\\Program Files\\Tools\\adb.exe")


class AdbExecutableMissingTests(unittest.TestCase):
    def test_absolute_paths_are_checked_on_disk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adb_path = Path(temp_dir) / "adb.exe"
            adb_path.write_bytes(b"")

            self.assertFalse(android_adb.adb_executable_missing(str(adb_path)))
            adb_path.unlink()
            self.assertTrue(android_adb.adb_executable_missing(str(adb_path)))

    def test_bare_names_are_checked_on_path(self):
        with mock.patch.object(android_adb.shutil, "which", return_value=None):
            self.assertTrue(android_adb.adb_executable_missing("adb.exe"))
        with mock.patch.object(android_adb.shutil, "which", return_value="C:\\tools\\adb.exe"):
            self.assertFalse(android_adb.adb_executable_missing("adb.exe"))


class RunAdbTests(unittest.TestCase):
    def test_tolerates_undecodable_output(self):
        result = android_adb._run_adb(
            sys.executable,
            ["-c", "import sys; sys.stdout.buffer.write(bytes([0x90]))"],
            timeout_sec=30.0,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.stdout, "\ufffd")

    def test_missing_executable_returns_error_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = android_adb._run_adb(
                os.path.join(temp_dir, "adb.exe"),
                ["version"],
                timeout_sec=5.0,
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
