from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


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


def find_adb_exe(configured_path: str = "") -> Optional[str]:
    candidates: List[str] = []
    if configured_path:
        candidates.append(configured_path)

    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(os.path.join(root, "platform-tools", "adb.exe"))
            candidates.append(os.path.join(root, "platform-tools", "adb"))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"))

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(
            os.path.join(user_profile, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe")
        )

    candidates.extend(("adb.exe", "adb"))

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate) and not os.path.isfile(candidate):
            continue
        result = _run_adb(candidate, ("version",), timeout_sec=3.0)
        if result.ok:
            return candidate
    return None


def list_devices(adb_path: str) -> AdbCommandResult:
    return _run_adb(adb_path, ("devices", "-l"), timeout_sec=15.0)


def parse_devices(output: str) -> List[AdbDevice]:
    devices: List[AdbDevice] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("list of devices"):
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


def _run_adb(adb_path: str, args: Iterable[str], timeout_sec: float) -> AdbCommandResult:
    try:
        completed = subprocess.run(
            [adb_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return AdbCommandResult(ok=False, error=str(exc))

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return AdbCommandResult(
        ok=completed.returncode == 0,
        stdout=stdout,
        stderr=stderr,
        error="" if completed.returncode == 0 else stderr or stdout or f"adb exited {completed.returncode}",
    )
