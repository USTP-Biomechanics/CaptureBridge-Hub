from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


# An adb client restarts a running server that reports a different version,
# which drops every reverse; current Platform-Tools report 1.0.41.
MIN_ADB_PROTOCOL_VERSION = 41
_ADB_VERSION_PATTERN = re.compile(r"Android Debug Bridge version \d+\.\d+\.(\d+)")
_ADB_INSTALLED_AS_PATTERN = re.compile(r"^Installed as (.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    product: str = ""
    model: str = ""
    device: str = ""
    transport_id: str = ""


@dataclass(frozen=True)
class AdbCommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str = ""


@dataclass(frozen=True)
class ReverseSync:
    active_ports: FrozenSet[int]
    created_ports: Tuple[int, ...]
    errors: Tuple[Tuple[int, str], ...]


def configured_adb_paths(configured_path: str, app_root: str = "") -> List[str]:
    value = os.path.expandvars(os.path.expanduser(str(configured_path or "").strip()))
    if not value:
        return []
    configured = os.path.abspath(os.path.join(app_root, value))
    if os.path.isdir(configured):
        return [
            os.path.join(configured, "adb.exe"),
            os.path.join(configured, "platform-tools", "adb.exe"),
        ]
    paths = [configured]
    if not os.path.dirname(value):
        # A bare name such as "adb.exe" also means the adb on PATH.
        on_path = shutil.which(value)
        if on_path:
            paths.append(os.path.abspath(on_path))
    return paths


def adb_candidate_paths(
    configured_path: str = "",
    app_root: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    env = os.environ if environ is None else environ
    candidates = configured_adb_paths(configured_path, app_root)

    if app_root:
        # A platform-tools folder dropped next to Run_CaptureBridge_Hub.bat.
        for folder in (app_root, os.path.dirname(app_root)):
            candidates.append(os.path.join(folder, "platform-tools", "adb.exe"))

    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = env.get(env_name)
        if root:
            candidates.append(os.path.join(root, "platform-tools", "adb.exe"))
            candidates.append(os.path.join(root, "platform-tools", "adb"))

    local_app_data = env.get("LOCALAPPDATA")
    if not local_app_data and env.get("USERPROFILE"):
        local_app_data = os.path.join(env["USERPROFILE"], "AppData", "Local")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"))

    on_path = shutil.which("adb", path=env.get("PATH", ""))
    if on_path:
        candidates.append(os.path.abspath(on_path))

    # Visual Studio / Xamarin install their Android SDK here.
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        program_files = env.get(env_name)
        if program_files:
            candidates.append(
                os.path.join(program_files, "Android", "android-sdk", "platform-tools", "adb.exe")
            )

    program_files = env.get("ProgramFiles")
    if program_files:
        unity_pattern = os.path.join(
            glob.escape(program_files),
            "Unity", "Hub", "Editor", "*", "Editor", "Data", "PlaybackEngines",
            "AndroidPlayer", "SDK", "platform-tools", "adb.exe",
        )
        candidates.extend(
            sorted(
                glob.glob(unity_pattern),
                key=lambda path: (os.path.getmtime(path), path),
                reverse=True,
            )
        )

    candidates.extend(("adb.exe", "adb"))

    unique: List[str] = []
    seen: Set[str] = set()
    for candidate in map(os.path.normpath, candidates):
        key = os.path.normcase(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_adb_exe(configured_path: str = "", app_root: str = "") -> Optional[str]:
    configured = {os.path.normcase(path) for path in configured_adb_paths(configured_path, app_root)}
    first_working: Optional[str] = None
    for candidate in adb_candidate_paths(configured_path, app_root):
        if os.path.isabs(candidate) and not os.path.isfile(candidate):
            continue
        result = _run_adb(candidate, ("version",), timeout_sec=3.0)
        if not result.ok:
            continue
        # The version preference only chooses among automatically found copies.
        if os.path.normcase(candidate) in configured:
            return candidate
        if not os.path.isabs(candidate):
            resolved = shutil.which(candidate)
            if resolved:
                candidate = os.path.abspath(resolved)
            else:
                # CreateProcess also searches the Python and Windows folders,
                # which shutil.which skips; adb reports where it ran from.
                installed = _ADB_INSTALLED_AS_PATTERN.search(result.stdout)
                if installed:
                    candidate = installed.group(1)
        if _adb_protocol_version(result.stdout) >= MIN_ADB_PROTOCOL_VERSION:
            return candidate
        if first_working is None:
            first_working = candidate
    return first_working


def adb_executable_missing(adb_path: str) -> bool:
    if os.path.isabs(adb_path):
        return not os.path.isfile(adb_path)
    return shutil.which(adb_path) is None


def list_devices(adb_path: str) -> AdbCommandResult:
    return _run_adb(adb_path, ("devices", "-l"), timeout_sec=15.0)


def parse_devices(output: str) -> List[AdbDevice]:
    devices: List[AdbDevice] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        # Older adb versions print "* daemon ..." start-up banners on stdout.
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        state = parts[1]
        attrs: Dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            attrs[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                product=attrs.get("product", ""),
                model=attrs.get("model", ""),
                device=attrs.get("device", ""),
                transport_id=attrs.get("transport_id", ""),
            )
        )
    return devices


def setup_reverse(adb_path: str, serial: str, host_port: int, device_port: int) -> AdbCommandResult:
    return _run_adb(
        adb_path,
        ("-s", serial, "reverse", f"tcp:{device_port}", f"tcp:{host_port}"),
        timeout_sec=10.0,
    )


def remove_reverse(adb_path: str, serial: str, device_port: int) -> AdbCommandResult:
    return _run_adb(
        adb_path,
        ("-s", serial, "reverse", "--remove", f"tcp:{device_port}"),
        timeout_sec=10.0,
    )


def list_reverses(adb_path: str, serial: str) -> AdbCommandResult:
    return _run_adb(adb_path, ("-s", serial, "reverse", "--list"), timeout_sec=10.0)


def parse_reverse_list(output: str) -> Set[Tuple[str, str]]:
    reverses: Set[Tuple[str, str]] = set()
    for raw_line in str(output or "").splitlines():
        parts = raw_line.split()
        if len(parts) >= 2:
            reverses.add((parts[-2], parts[-1]))
    return reverses


def ensure_reverses(adb_path: str, serial: str, ports: Sequence[int]) -> ReverseSync:
    listing = list_reverses(adb_path, serial)
    listed = parse_reverse_list(listing.stdout) if listing.ok else set()
    active: Set[int] = set()
    created: List[int] = []
    errors: List[Tuple[int, str]] = []
    for port in ports:
        if (f"tcp:{port}", f"tcp:{port}") in listed:
            active.add(port)
            continue
        result = setup_reverse(adb_path, serial, port, port)
        if result.ok:
            active.add(port)
            created.append(port)
        else:
            errors.append((port, result.error))
    return ReverseSync(
        active_ports=frozenset(active),
        created_ports=tuple(created),
        errors=tuple(errors),
    )


def model_label(device: AdbDevice) -> str:
    return device.model or device.device or device.product or device.serial


def device_summary(devices: Iterable[AdbDevice]) -> str:
    items = list(devices)
    ready = [d for d in items if d.state == "device"]
    unauthorized = [d for d in items if d.state == "unauthorized"]
    offline = [d for d in items if d.state == "offline"]
    parts = []
    if ready:
        parts.append(f"{len(ready)} ready")
    if unauthorized:
        parts.append(f"{len(unauthorized)} unauthorized")
    if offline:
        parts.append(f"{len(offline)} offline")
    if not parts:
        return "no USB phones"
    return ", ".join(parts)


def _adb_protocol_version(version_output: str) -> int:
    match = _ADB_VERSION_PATTERN.search(version_output or "")
    return int(match.group(1)) if match else 0


def _run_adb(adb_path: str, args: Iterable[str], timeout_sec: float) -> AdbCommandResult:
    try:
        completed = subprocess.run(
            [adb_path, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return AdbCommandResult(ok=False, error=str(exc))

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    return AdbCommandResult(
        ok=completed.returncode == 0,
        stdout=stdout,
        stderr=stderr,
        error="" if completed.returncode == 0 else stderr or stdout or f"adb exited {completed.returncode}",
    )
