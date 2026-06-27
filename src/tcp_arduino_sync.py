import socket
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk, filedialog, messagebox
from datetime import datetime
import time
import os
import json
import copy
import queue
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import serial
import serial.tools.list_ports
from lag_test import LagTiming, LagTimingDisplay, analyze_lag_video, write_lag_report
# -----------------------------------
# CONFIG
# -----------------------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 6000

# UDP discovery (broadcast) settings
DISCOVERY_UDP_PORT = 6000
DISCOVERY_REQUEST = "DISCOVER_UDPCAMERA"
DISCOVERY_RESPONSE_PREFIX = "UDPCAMERA_OK"
ARDUINO_BAUD = 9600
ARDUINO_PROBE_COMMAND = b"PING\n"
ARDUINO_PROBE_RESPONSE = "CAPTUREBRIDGE_ARDUINO_BRIDGE"
ARDUINO_PROBE_BOOT_WAIT_SEC = 2.0
ARDUINO_PROBE_RESPONSE_TIMEOUT_SEC = 1.0
NAME_RESEND_INTERVAL_MS = 1000
NAME_RESEND_LOG_INTERVAL_SEC = 5
EDIT_FIELD_DEBOUNCE_MS = 500
CAMERA_APPLY_DEBOUNCE_MS = EDIT_FIELD_DEBOUNCE_MS
NAME_EDIT_DEBOUNCE_MS = EDIT_FIELD_DEBOUNCE_MS
SAVE_DIR_EDIT_DEBOUNCE_MS = EDIT_FIELD_DEBOUNCE_MS
DEFAULT_CAMERA_WIDTH = 1920
DEFAULT_CAMERA_HEIGHT = 1080
DEFAULT_CAMERA_ISO = 800
DEFAULT_CAMERA_SHUTTER_FPS_MULTIPLIER = 2.0
LAG_TEST_START_TARGET_MS = 1000.0
LAG_TEST_STOP_TARGET_MS = 2000.0
LAG_TEST_DURATION_S = (LAG_TEST_STOP_TARGET_MS - LAG_TEST_START_TARGET_MS) / 1000.0
LAG_TEST_PREPARE_TIMEOUT_MS = 8000
LAG_TEST_STOP_MARKED_TIMEOUT_MS = 3000
LAG_TEST_STOP_OK_TIMEOUT_MS = 20000
LAG_TEST_READY_TIMEOUT_MS = 3000
LAG_TEST_PHONE_PREROLL_MS = 1000
LAG_TEST_COMMAND_TIMING_TOLERANCE_MS = 25.0
LAG_TEST_TRANSFER_ROOT = "LagTests"
CAPTURE_STOP_OK_TIMEOUT_MS = 20000
CAPTURE_READY_TIMEOUT_MS = 3000
TCP_KEEPALIVE_ENABLED = True
TCP_KEEPALIVE_IDLE_SEC = 30
TCP_KEEPALIVE_INTERVAL_SEC = 10
TCP_KEEPALIVE_COUNT = 3
CLIENT_SOCKET_TIMEOUT_SEC = 1.0
ARDUINO_WRITE_TIMEOUT_SEC = 1.0
MAX_LOG_LINES = 2000
LOG_TRIM_BATCH_LINES = 250
APP_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_app_root(source_dir: str) -> str:
    if os.path.basename(source_dir).lower() == "src":
        return os.path.dirname(source_dir)
    return source_dir


APP_ROOT = resolve_app_root(APP_SOURCE_DIR)
APP_CONFIG_PATH = os.path.join(APP_ROOT, "app_config.json")
APP_STATE_PATH = os.path.join(APP_ROOT, "capturebridge_state.json")
INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
DEFAULT_APP_CONFIG = {
    "default_save_path": ".",
    "name_separator": "_",
    "name_fields": [
        {
            "key": "key",
            "label": "Key",
            "type": "text",
            "default": "CaptureBridge",
        },
        {
            "key": "id",
            "label": "ID (001-999)",
            "type": "number",
            "default": "001",
            "min": 1,
            "max": 999,
            "pad_to": 3,
            "output_prefix": "ID",
        },
        {
            "key": "trial",
            "label": "Trial number",
            "type": "number",
            "default": "001",
            "min": 1,
            "max": 999,
            "pad_to": 3,
            "output_prefix": "TR",
            "lockable": True,
            "locked_by_default": True,
            "auto_increment_on_stop": True,
        },
    ],
    "phone_stream": {
        "enabled": True,
        "udp_port": 6101,
        "max_fps": 20,
        "jpeg_quality": 70,
        "max_dimension": 1280,
        "socket_buffer_bytes": 4194304,
    },
    "camera_defaults": {
        "preferred_width": DEFAULT_CAMERA_WIDTH,
        "preferred_height": DEFAULT_CAMERA_HEIGHT,
        "iso": DEFAULT_CAMERA_ISO,
        "shutter_fps_multiplier": DEFAULT_CAMERA_SHUTTER_FPS_MULTIPLIER,
    },
}


# -----------------------------------
# Helpers
# -----------------------------------
def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ["KB", "MB", "GB", "TB"]:
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} PB"


def format_number_value(number: int, pad_to: int) -> str:
    return f"{number:0{max(int(pad_to), 1)}d}"


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def clamp_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def compact_float_text(value: float, precision: int = 6) -> str:
    return f"{float(value):.{precision}f}".rstrip("0").rstrip(".")


def format_shutter_seconds(shutter_seconds: float) -> str:
    try:
        shutter = float(shutter_seconds)
    except (TypeError, ValueError):
        return ""
    if shutter <= 0:
        return ""

    reciprocal = 1.0 / shutter
    rounded = int(round(reciprocal))
    if rounded > 0 and abs(reciprocal - rounded) < 0.01:
        return f"1/{rounded}"
    return compact_float_text(shutter, precision=6)


def default_shutter_seconds_for_fps(fps: float, multiplier: float) -> float:
    fps = max(float(fps or 0.0), 1.0)
    multiplier = max(float(multiplier or DEFAULT_CAMERA_SHUTTER_FPS_MULTIPLIER), 0.1)
    return 1.0 / (fps * multiplier)


def normalize_camera_profile(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    try:
        width = int(raw.get("width"))
        height = int(raw.get("height"))
        fps = float(raw.get("fps"))
        iso = float(raw.get("iso"))
        shutter_seconds = float(raw.get("shutterSeconds"))
    except (TypeError, ValueError):
        return None

    if width <= 0 or height <= 0 or fps <= 0 or iso <= 0 or shutter_seconds <= 0:
        return None

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "iso": iso,
        "shutterSeconds": shutter_seconds,
    }


def load_app_state() -> Tuple[dict, List[str]]:
    if not os.path.isfile(APP_STATE_PATH):
        return {}, []

    try:
        with open(APP_STATE_PATH, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            return loaded, []
        return {}, [f"Ignoring {APP_STATE_PATH}: state root must be a JSON object"]
    except Exception as exc:
        return {}, [f"Ignoring {APP_STATE_PATH}: {exc}"]


def save_app_state(state: dict):
    temp_path = f"{APP_STATE_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(temp_path, APP_STATE_PATH)


def resolve_configured_path(path_text: str) -> str:
    raw = str(path_text or "").strip()
    if not raw:
        return APP_ROOT
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(APP_ROOT, expanded))


def resolve_safe_transfer_path(save_dir: str, relative_path: str) -> str:
    base_dir = os.path.abspath(save_dir)
    raw = str(relative_path or "").strip()
    if not raw:
        raise ValueError("empty relative path")

    normalized = raw.replace("\\", "/")
    if os.path.isabs(raw) or os.path.isabs(normalized):
        raise ValueError("absolute paths are not allowed")

    parts = normalized.split("/")
    safe_parts = []
    for part in parts:
        if not part or part in (".", ".."):
            raise ValueError("path must not contain empty, current, or parent segments")
        if any(ch in INVALID_FILENAME_CHARS or ord(ch) < 32 for ch in part):
            raise ValueError("path contains invalid filename characters")
        safe_parts.append(part)

    target_path = os.path.abspath(os.path.join(base_dir, *safe_parts))
    if os.path.commonpath([base_dir, target_path]) != base_dir:
        raise ValueError("path escapes the save directory")
    return target_path


def lag_test_storage_relative_path(relative_path: str) -> str:
    raw = str(relative_path or "").strip()
    normalized = raw.replace("\\", "/")
    first_part = normalized.split("/", 1)[0]
    if first_part.startswith("lagtest_"):
        return f"{LAG_TEST_TRANSFER_ROOT}/{normalized}"
    return raw


def _format_protocol_timing_for_log(text: str) -> str:
    formatted = []
    values_ns = {}
    for token in str(text or "").split():
        if "=" not in token:
            formatted.append(token)
            continue

        key, value = token.split("=", 1)
        if not key.endswith("_ns"):
            formatted.append(token)
            continue

        try:
            ns_value = int(value)
        except ValueError:
            formatted.append(token)
            continue

        values_ns[key] = ns_value
        if key not in ("phone_rx_ns", "phone_tx_ns"):
            ms_value = (ns_value + 500_000) // 1_000_000 if ns_value >= 0 else ns_value
            formatted.append(f"{key[:-3]}_ms={ms_value}")

    phone_rx_ns = values_ns.get("phone_rx_ns")
    phone_tx_ns = values_ns.get("phone_tx_ns")
    if phone_rx_ns is not None and phone_tx_ns is not None:
        delta_ms = (phone_tx_ns - phone_rx_ns) / 1_000_000.0
        insert_at = 1 if formatted and "=" not in formatted[0] else 0
        formatted[insert_at:insert_at] = [f"phone_rx_tx_delta_ms={delta_ms:.3f}"]

    return " ".join(formatted)


def _truncate_log_text(text: str, limit: int = 240) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _parse_protocol_fields(text: str) -> Tuple[List[str], Dict[str, str]]:
    labels = []
    fields = {}
    for token in str(text or "").split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
        else:
            labels.append(token)
    return labels, fields


def _field_float(fields: Dict[str, str], key: str) -> Optional[float]:
    try:
        return float(fields[key])
    except Exception:
        return None


def _fmt_delta_ms(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:+.1f} ms"


def _phone_rx_tx_delta_ms(fields: Dict[str, str]) -> Optional[float]:
    direct = _field_float(fields, "phone_rx_tx_delta_ms")
    if direct is not None:
        return direct
    phone_rx_ns = _field_float(fields, "phone_rx_ns")
    phone_tx_ns = _field_float(fields, "phone_tx_ns")
    if phone_rx_ns is None or phone_tx_ns is None:
        return None
    return (phone_tx_ns - phone_rx_ns) / 1_000_000.0


def _format_phone_lifecycle_for_log(cmd: str, rest: str) -> str:
    labels, fields = _parse_protocol_fields(rest)
    parts = [cmd]
    parts.extend(labels)

    phone_delta = _phone_rx_tx_delta_ms(fields)
    if phone_delta is not None:
        parts.append(f"phone={phone_delta:.1f} ms")

    stop_begin_ms = _field_float(fields, "phone_stop_begin_ms")
    stop_marked_ms = _field_float(fields, "phone_stop_marked_ms")
    stop_mux_done_ms = _field_float(fields, "phone_stop_mux_done_ms")
    requested_end_us = _field_float(fields, "requested_end_us")

    if stop_begin_ms is not None:
        marked_delta = None if stop_marked_ms is None else stop_marked_ms - stop_begin_ms
        mux_delta = None if stop_mux_done_ms is None else stop_mux_done_ms - stop_begin_ms
        requested_delta = None if requested_end_us is None else (requested_end_us / 1000.0) - stop_begin_ms
        if marked_delta is not None:
            parts.append(f"mark={_fmt_delta_ms(marked_delta)}")
        if mux_delta is not None:
            parts.append(f"mux={_fmt_delta_ms(mux_delta)}")
        if requested_delta is not None:
            parts.append(f"requested_end={_fmt_delta_ms(requested_delta)}")

    if cmd == "PREPARE_OK":
        preroll = fields.get("preroll_ms")
        lead = fields.get("camera_lead_ms")
        if preroll is not None:
            parts.append(f"preroll={preroll} ms")
        if lead is not None:
            parts.append(f"lead={lead} ms")

    return " ".join(part for part in parts if part)


def _compact_resolution_text(profile: dict) -> str:
    width = int(profile.get("width", 0) or 0)
    height = int(profile.get("height", 0) or 0)
    fps = profile.get("fps")
    if fps is None:
        return f"{width} x {height}"
    fps_text = f"{float(fps):.2f}".rstrip("0").rstrip(".")
    return f"{width} x {height} @ {fps_text} fps"


def _format_client_line_for_log(line: str) -> str:
    parts = str(line or "").split(" ", 1)
    cmd = parts[0].upper() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("SETTINGS_LIST_OK", "SETTINGS_OK"):
        try:
            payload = json.loads(rest)
            resolutions = payload.get("resolutions", [])
            mode_keys = {
                (
                    int(option.get("width", 0) or 0),
                    int(option.get("height", 0) or 0),
                )
                for option in resolutions
            }
            current = payload.get("current", {})
            current_text = _compact_resolution_text(current) if current else "unknown"
            position = payload.get("position")
            position_text = f" position={position}" if position else ""
            return (
                f"{cmd} current={current_text} "
                f"modes={len(mode_keys)} options={len(resolutions)}{position_text}"
            )
        except Exception:
            return f"{cmd} {_truncate_log_text(rest)}".rstrip()

    if cmd == "LIST_OK":
        try:
            payload = json.loads(rest)
            captures = payload.get("captures", [])
            file_count = sum(len(capture.get("files", [])) for capture in captures)
            names = [
                _truncate_log_text(str(capture.get("name", "")), 48)
                for capture in captures[:2]
            ]
            names_text = f" first={', '.join(name for name in names if name)}" if names else ""
            if len(captures) > 2:
                names_text += f" +{len(captures) - 2} more"
            return f"LIST_OK captures={len(captures)} files={file_count}{names_text}"
        except Exception:
            return f"{cmd} {_truncate_log_text(rest)}".rstrip()

    if cmd == "LIVE_PREVIEW_STATE":
        try:
            payload = json.loads(rest)
            active = bool(payload.get("active"))
            message = payload.get("message") or ("streaming" if active else "stopped")
            host = payload.get("host")
            port = payload.get("port")
            target = f" {host}:{port}" if host and port else ""
            error = f" error={payload.get('error')}" if payload.get("error") else ""
            return f"LIVE_PREVIEW_STATE {message}{target}{error}"
        except Exception:
            return f"{cmd} {_truncate_log_text(rest)}".rstrip()

    if cmd in ("STOP_MARKED", "STOP_OK", "READY", "READY_ERR", "PREPARE_OK"):
        return _format_phone_lifecycle_for_log(cmd, rest)

    formatted = _format_protocol_timing_for_log(line)
    return _truncate_log_text(formatted)


def _split_protocol_payload_and_fields(text: str) -> Tuple[str, str]:
    payload = []
    fields = []
    for token in str(text or "").split():
        if "=" in token:
            fields.append(token)
        else:
            payload.append(token)
    return " ".join(payload), " ".join(fields)


@dataclass
class HubLagTestSession:
    label: str
    client_key: str
    client_addr: str
    client_name: str
    display: LagTimingDisplay
    duration_s: float
    display_started_perf: float
    display_refresh_hz: Optional[float] = None
    display_tick_interval_ms: Optional[float] = None
    actual_start_command_elapsed_ms: Optional[float] = None
    actual_stop_command_elapsed_ms: Optional[float] = None
    start_command_elapsed_ms: Optional[float] = None
    stop_command_elapsed_ms: Optional[float] = None
    start_ack_elapsed_ms: Optional[float] = None
    stop_marked_elapsed_ms: Optional[float] = None
    stop_ok_elapsed_ms: Optional[float] = None
    stop_ack_elapsed_ms: Optional[float] = None
    ready_elapsed_ms: Optional[float] = None
    capture_name: Optional[str] = None
    capture_info: Optional[dict] = None
    segment_metrics: Optional[dict] = None
    poll_attempts: int = 0
    transfer_requested: bool = False
    transfer_lookup_started: bool = False
    analysis_started: bool = False
    camera_setup_requested: bool = False
    camera_setup_applied: bool = False
    prepare_requested: bool = False
    prepared: bool = False


def _build_lag_test_command_timing(extra: dict, analysis) -> dict:
    segment = extra.get("segment") if isinstance(extra.get("segment"), dict) else {}
    actual_start_ms = extra.get("actual_start_command_elapsed_ms")
    actual_stop_ms = extra.get("actual_stop_command_elapsed_ms")
    intended_start_ms = extra.get("intended_start_ms")
    intended_stop_ms = extra.get("intended_stop_ms")
    phone_rx_duration_us = segment.get("phone_rx_duration_us")

    hub_send_duration_ms = None
    if actual_start_ms is not None and actual_stop_ms is not None:
        hub_send_duration_ms = float(actual_stop_ms) - float(actual_start_ms)

    intended_duration_ms = None
    if intended_start_ms is not None and intended_stop_ms is not None:
        intended_duration_ms = float(intended_stop_ms) - float(intended_start_ms)

    phone_rx_duration_ms = None
    if phone_rx_duration_us is not None:
        phone_rx_duration_ms = float(phone_rx_duration_us) / 1000.0

    duration_error_vs_hub_ms = None
    if phone_rx_duration_ms is not None and hub_send_duration_ms is not None:
        duration_error_vs_hub_ms = phone_rx_duration_ms - hub_send_duration_ms

    duration_error_vs_target_ms = None
    if phone_rx_duration_ms is not None and intended_duration_ms is not None:
        duration_error_vs_target_ms = phone_rx_duration_ms - intended_duration_ms

    threshold_ms = LAG_TEST_COMMAND_TIMING_TOLERANCE_MS
    late_kind = "ok"
    late_message = ""
    if duration_error_vs_hub_ms is not None and abs(duration_error_vs_hub_ms) > threshold_ms:
        if duration_error_vs_hub_ms > 0:
            late_kind = "stop_received_late"
            late_message = (
                f"STOP reached phone {duration_error_vs_hub_ms:.1f} ms late "
                "relative to Hub START-to-STOP send duration"
            )
        else:
            late_kind = "start_received_late"
            late_message = (
                f"START reached phone {-duration_error_vs_hub_ms:.1f} ms late "
                "relative to Hub START-to-STOP send duration"
            )
    elif duration_error_vs_target_ms is not None and abs(duration_error_vs_target_ms) > threshold_ms:
        late_kind = "command_duration_off_target"
        late_message = (
            f"phone START-to-STOP receive duration was {duration_error_vs_target_ms:+.1f} ms "
            "from the lag-test target"
        )

    if not late_message:
        late_message = "phone START-to-STOP receive duration matched Hub command timing"

    return {
        "late": late_kind != "ok",
        "late_kind": late_kind,
        "late_message": late_message,
        "tolerance_ms": threshold_ms,
        "intended_duration_ms": intended_duration_ms,
        "hub_send_duration_ms": hub_send_duration_ms,
        "phone_rx_duration_ms": phone_rx_duration_ms,
        "duration_error_vs_hub_ms": duration_error_vs_hub_ms,
        "duration_error_vs_target_ms": duration_error_vs_target_ms,
        "start_lag_ms": getattr(analysis, "start_lag_ms", None),
        "stop_lag_ms": getattr(analysis, "stop_lag_ms", None),
    }


PHONE_STREAM_IMPORT_ERROR = ""
try:
    from phone_stream import PhoneStreamConfig, PhoneStreamPane
except Exception as exc:
    PHONE_STREAM_IMPORT_ERROR = str(exc)
    PhoneStreamPane = None

    class PhoneStreamConfig:
        def __init__(
            self,
            enabled: bool = True,
            udp_port: int = 6101,
            max_fps: int = 20,
            jpeg_quality: int = 70,
            max_dimension: int = 1280,
            socket_buffer_bytes: int = 4194304,
        ):
            self.enabled = enabled
            self.udp_port = udp_port
            self.max_fps = max_fps
            self.jpeg_quality = jpeg_quality
            self.max_dimension = max_dimension
            self.socket_buffer_bytes = socket_buffer_bytes

        @classmethod
        def from_mapping(cls, raw: Optional[dict], base_dir: str) -> "PhoneStreamConfig":
            del base_dir
            raw = raw if isinstance(raw, dict) else {}
            return cls(
                enabled=bool(raw.get("enabled", True)),
                udp_port=clamp_int(raw.get("udp_port"), 6101, 1024, 65535),
                max_fps=clamp_int(raw.get("max_fps"), 20, 1, 240),
                jpeg_quality=clamp_int(raw.get("jpeg_quality"), 70, 20, 95),
                max_dimension=clamp_int(raw.get("max_dimension"), 1280, 0, 8192),
                socket_buffer_bytes=clamp_int(
                    raw.get("socket_buffer_bytes"),
                    4194304,
                    262144,
                    67108864,
                ),
            )

        def build_start_payload(self, host: str) -> str:
            payload = {
                "host": host,
                "port": self.udp_port,
                "maxFps": self.max_fps,
                "jpegQuality": self.jpeg_quality,
                "maxDimension": self.max_dimension,
            }
            return json.dumps(payload, separators=(",", ":"))


class HoverTooltip:
    def __init__(self, widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip_window = None

        self.widget.bind("<Enter>", self._schedule_show, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")
        self.widget.bind("<ButtonPress>", self._hide, add="+")
        self.widget.bind("<Destroy>", self._hide, add="+")

    def _schedule_show(self, _event=None):
        self._cancel_scheduled_show()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_scheduled_show(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip_window is not None or not self.widget.winfo_exists():
            return

        self._tip_window = tk.Toplevel(self.widget)
        self._tip_window.wm_overrideredirect(True)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 10
        y = self.widget.winfo_rooty() + 2
        self._tip_window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            self._tip_window,
            text=self.text,
            justify=tk.LEFT,
            bg="#fff6bf",
            relief=tk.SOLID,
            borderwidth=1,
            padx=8,
            pady=4,
        )
        label.pack()

    def _hide(self, _event=None):
        self._cancel_scheduled_show()
        if self._tip_window is not None:
            try:
                self._tip_window.destroy()
            except tk.TclError:
                pass
            self._tip_window = None


def clone_default_name_fields() -> List[dict]:
    return copy.deepcopy(DEFAULT_APP_CONFIG["name_fields"])


def normalize_name_fields(fields_raw) -> Tuple[List[dict], List[str]]:
    messages = []
    normalized = []
    seen_keys = set()

    for index, raw_field in enumerate(fields_raw or [], start=1):
        if not isinstance(raw_field, dict):
            messages.append(f"Config field #{index} is not an object; skipping it")
            continue

        key = str(raw_field.get("key") or "field_%d" % index).strip()
        if not key:
            key = "field_%d" % index
        if key in seen_keys:
            messages.append(f"Config field key '{key}' is duplicated; skipping it")
            continue

        field_type = str(raw_field.get("type", "text")).strip().lower()
        if field_type not in ("text", "choice", "number"):
            messages.append(
                f"Config field '{key}' has unsupported type '{field_type}'; skipping it"
            )
            continue

        label = str(raw_field.get("label") or key).strip() or key
        default_value = str(raw_field.get("default", "")).strip()
        field = {
            "key": key,
            "label": label,
            "type": field_type,
            "default": default_value,
        }
        output_prefix = str(raw_field.get("output_prefix", "")).strip()
        output_suffix = str(raw_field.get("output_suffix", "")).strip()
        if output_prefix:
            field["output_prefix"] = output_prefix
        if output_suffix:
            field["output_suffix"] = output_suffix

        if field_type == "choice":
            options_raw = raw_field.get("options")
            if not isinstance(options_raw, list) or not options_raw:
                messages.append(f"Choice field '{key}' must define a non-empty options list")
                continue

            options = []
            for option in options_raw:
                text = str(option).strip()
                if text:
                    options.append(text)

            if not options:
                messages.append(f"Choice field '{key}' must define at least one non-empty option")
                continue

            allow_custom = bool(raw_field.get("allow_custom", False))
            if not default_value:
                default_value = options[0]
            elif not allow_custom and default_value not in options:
                messages.append(
                    f"Choice field '{key}' default '{default_value}' is invalid; using '{options[0]}'"
                )
                default_value = options[0]

            value_map_raw = raw_field.get("value_map", {})
            value_map = {}
            if isinstance(value_map_raw, dict):
                for map_key, map_value in value_map_raw.items():
                    value_map[str(map_key)] = str(map_value)

            field.update(
                {
                    "default": default_value,
                    "options": options,
                    "allow_custom": allow_custom,
                    "value_map": value_map,
                }
            )
        elif field_type == "number":
            try:
                min_value = int(raw_field.get("min", 1))
            except (TypeError, ValueError):
                min_value = 1
                messages.append(f"Number field '{key}' has invalid min value; using 1")

            try:
                max_value = int(raw_field.get("max", 999))
            except (TypeError, ValueError):
                max_value = 999
                messages.append(f"Number field '{key}' has invalid max value; using 999")

            if min_value > max_value:
                min_value, max_value = max_value, min_value
                messages.append(f"Number field '{key}' had min > max; values were swapped")

            default_pad = max(len(str(abs(min_value))), len(str(abs(max_value))), 1)
            try:
                pad_to = max(1, int(raw_field.get("pad_to", default_pad)))
            except (TypeError, ValueError):
                pad_to = default_pad
                messages.append(
                    f"Number field '{key}' has invalid pad_to value; using {default_pad}"
                )

            try:
                default_number = int(default_value)
            except (TypeError, ValueError):
                default_number = min_value
                if default_value:
                    messages.append(
                        f"Number field '{key}' default '{default_value}' is invalid; using {min_value}"
                    )

            if not min_value <= default_number <= max_value:
                messages.append(
                    f"Number field '{key}' default '{default_number}' is out of range; using {min_value}"
                )
                default_number = min_value

            field.update(
                {
                    "default": format_number_value(default_number, pad_to),
                    "min": min_value,
                    "max": max_value,
                    "pad_to": pad_to,
                    "max_digits": max(pad_to, len(str(abs(min_value))), len(str(abs(max_value)))),
                    "lockable": bool(
                        raw_field.get("lockable", False)
                        or raw_field.get("auto_increment_on_stop", False)
                    ),
                    "locked_by_default": bool(
                        raw_field.get(
                            "locked_by_default",
                            raw_field.get("auto_increment_on_stop", False),
                        )
                    ),
                    "auto_increment_on_stop": bool(raw_field.get("auto_increment_on_stop", False)),
                }
            )
        else:
            if raw_field.get("lockable") or raw_field.get("auto_increment_on_stop"):
                messages.append(
                    f"Field '{key}' is not numeric; lockable and auto_increment_on_stop were ignored"
                )

        normalized.append(field)
        seen_keys.add(key)

    return normalized, messages


def normalize_camera_defaults(raw, messages: List[str]) -> dict:
    defaults = copy.deepcopy(DEFAULT_APP_CONFIG["camera_defaults"])
    if not isinstance(raw, dict):
        messages.append("Config camera_defaults must be an object; using defaults")
        raw = {}

    return {
        "preferred_width": clamp_int(
            raw.get("preferred_width"),
            defaults["preferred_width"],
            1,
            8192,
        ),
        "preferred_height": clamp_int(
            raw.get("preferred_height"),
            defaults["preferred_height"],
            1,
            8192,
        ),
        "iso": clamp_float(raw.get("iso"), defaults["iso"], 1.0, 100000.0),
        "shutter_fps_multiplier": clamp_float(
            raw.get("shutter_fps_multiplier"),
            defaults["shutter_fps_multiplier"],
            0.1,
            16.0,
        ),
    }


def load_app_config() -> Tuple[dict, List[str]]:
    messages = []
    raw_config = {}

    if os.path.isfile(APP_CONFIG_PATH):
        try:
            with open(APP_CONFIG_PATH, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a JSON object")
            raw_config = loaded
            messages.append(f"Loaded config from {APP_CONFIG_PATH}")
        except Exception as exc:
            messages.append(f"Failed to load {APP_CONFIG_PATH}: {exc}. Using defaults")
    else:
        messages.append(f"Config file not found at {APP_CONFIG_PATH}. Using defaults")

    default_save_path = raw_config.get(
        "default_save_path", DEFAULT_APP_CONFIG["default_save_path"]
    )
    if not isinstance(default_save_path, str):
        messages.append("Config default_save_path must be a string; using app root")
        default_save_path = DEFAULT_APP_CONFIG["default_save_path"]

    name_separator = raw_config.get("name_separator", DEFAULT_APP_CONFIG["name_separator"])
    if not isinstance(name_separator, str) or not name_separator:
        messages.append("Config name_separator must be a non-empty string; using '_'")
        name_separator = DEFAULT_APP_CONFIG["name_separator"]

    fields_raw = raw_config.get("name_fields", clone_default_name_fields())
    if not isinstance(fields_raw, list) or not fields_raw:
        messages.append("Config name_fields must be a non-empty list; using defaults")
        fields_raw = clone_default_name_fields()

    normalized_fields, field_messages = normalize_name_fields(fields_raw)
    messages.extend(field_messages)

    if not normalized_fields:
        messages.append("Config produced no usable name fields; reverting to defaults")
        normalized_fields, fallback_messages = normalize_name_fields(clone_default_name_fields())
        messages.extend(fallback_messages)

    camera_defaults = normalize_camera_defaults(
        raw_config.get("camera_defaults", DEFAULT_APP_CONFIG["camera_defaults"]),
        messages,
    )

    return (
        {
            "default_save_path": resolve_configured_path(default_save_path),
            "name_separator": name_separator,
            "name_fields": normalized_fields,
            "phone_stream": copy.deepcopy(
                raw_config.get(
                    "phone_stream",
                    DEFAULT_APP_CONFIG["phone_stream"],
                )
            ),
            "camera_defaults": camera_defaults,
        },
        messages,
    )


# -----------------------------------
# Arduino controller
# -----------------------------------
class ArduinoController:
    def __init__(self, log_callback, command_callback: Optional[Callable[[str], None]] = None):
        self.ser: Optional[serial.Serial] = None
        self.log = log_callback
        self.command_callback = command_callback
        self._listen_armed = False
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_stop_event = threading.Event()
        self._incoming_buffer = ""
        self._connect_lock = threading.Lock()
        self._connect_thread: Optional[threading.Thread] = None
        self._closed = False
        self.connect_async()

    def connect_async(self):
        if self._closed:
            return
        if self._connect_thread and self._connect_thread.is_alive():
            return
        self._connect_thread = threading.Thread(target=self.connect, daemon=True)
        self._connect_thread.start()

    def connect(self):
        with self._connect_lock:
            self._connect_locked()

    def _connect_locked(self):
        try:
            if self._closed:
                return
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

            ports = list(serial.tools.list_ports.comports())
            if not ports:
                self.log("No serial ports found for Arduino")
                return

            candidates = [port for port in ports if self._is_likely_arduino_port(port)]
            if not candidates:
                available_ports = ", ".join(self._describe_port(port) for port in ports)
                self.log(
                    "No likely Arduino serial ports found; skipping serial connection. "
                    f"Available ports: {available_ports}"
                )
                return

            self.log(
                "Probing Arduino candidates: "
                + ", ".join(self._describe_port(port) for port in candidates)
            )

            for port in candidates:
                ser = self._probe_port(port)
                if ser:
                    if self._closed:
                        try:
                            ser.close()
                        except Exception:
                            pass
                        return
                    self.ser = ser
                    self.log(f"Arduino connected on {port.device} @ {ARDUINO_BAUD} baud")
                    return

            self.log("No Arduino bridge responded on any candidate serial port")
        except Exception as e:
            self.log(f"Failed to connect to Arduino: {e}")
            self.ser = None

    def _probe_port(self, port_info) -> Optional[serial.Serial]:
        port_name = getattr(port_info, "device", "")
        description = self._describe_port(port_info)
        try:
            ser = serial.Serial(
                port_name,
                ARDUINO_BAUD,
                timeout=0.1,
                write_timeout=ARDUINO_WRITE_TIMEOUT_SEC,
            )
        except Exception as exc:
            self.log(f"Could not open Arduino candidate {description}: {exc}")
            return None

        try:
            time.sleep(ARDUINO_PROBE_BOOT_WAIT_SEC)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(ARDUINO_PROBE_COMMAND)
            ser.flush()

            deadline = time.time() + ARDUINO_PROBE_RESPONSE_TIMEOUT_SEC
            response_buffer = ""
            while time.time() < deadline:
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    continue

                response_buffer += chunk.decode("utf-8", errors="ignore")
                if ARDUINO_PROBE_RESPONSE in response_buffer:
                    ser.reset_input_buffer()
                    return ser

            self.log(f"Arduino probe failed on {description}")
        except Exception as exc:
            self.log(f"Arduino probe error on {description}: {exc}")

        try:
            ser.close()
        except Exception:
            pass
        return None

    @staticmethod
    def _describe_port(port_info) -> str:
        device = getattr(port_info, "device", "unknown")
        description = getattr(port_info, "description", "") or "no description"
        return f"{device} ({description})"

    @staticmethod
    def _is_likely_arduino_port(port_info) -> bool:
        metadata_parts = [
            getattr(port_info, "device", ""),
            getattr(port_info, "description", ""),
            getattr(port_info, "manufacturer", ""),
            getattr(port_info, "product", ""),
            getattr(port_info, "hwid", ""),
        ]
        metadata = " ".join(str(part).lower() for part in metadata_parts if part)
        keyword_matches = (
            "arduino",
            "wch",
            "ch340",
            "ch341",
            "usb-serial",
            "usb serial",
        )
        if any(keyword in metadata for keyword in keyword_matches):
            return True

        return getattr(port_info, "vid", None) in (0x2341, 0x2A03)

    def send_bytes(self, value: bytes):
        if not self.ser:
            self.log("Cannot send to Arduino: no serial connection")
            return
        try:
            self.ser.write(value)
            self.ser.flush()
            self.log(f"Sent to Arduino: {value!r}")
        except Exception as e:
            self.log(f"Error sending to Arduino: {e}")

    def start(self):
        self.send_bytes(b"1")

    def stop(self):
        self.send_bytes(b"0")

    def arm_listener(self) -> bool:
        if not self.ser:
            self.connect()
        if not self.ser:
            self.log("Cannot arm Arduino serial listener: no serial connection")
            return False

        if not self._listener_thread or not self._listener_thread.is_alive():
            self._listener_stop_event.clear()
            self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._listener_thread.start()

        self._listen_armed = True
        self._incoming_buffer = ""
        self.log("Arduino serial listener armed")
        return True

    def disarm_listener(self):
        if not self._listen_armed:
            return
        self._listen_armed = False
        self._incoming_buffer = ""
        self.log("Arduino serial listener disarmed")

    def _listen_loop(self):
        while not self._listener_stop_event.is_set():
            if not self._listen_armed:
                time.sleep(0.05)
                continue

            ser = self.ser
            if not ser:
                time.sleep(0.1)
                continue

            try:
                chunk = ser.read(ser.in_waiting or 1)
            except Exception as e:
                if not self._listener_stop_event.is_set():
                    self.log(f"Error reading from Arduino: {e}")
                self._listen_armed = False
                return

            if not chunk:
                self._flush_pending_buffer()
                continue

            self._process_incoming_chunk(chunk)

    def _process_incoming_chunk(self, chunk: bytes):
        text = chunk.decode("utf-8", errors="ignore")
        if not text:
            return

        self._incoming_buffer += text.replace("\r", "\n")
        while "\n" in self._incoming_buffer:
            line, self._incoming_buffer = self._incoming_buffer.split("\n", 1)
            self._handle_incoming_text(line)

    def _flush_pending_buffer(self):
        pending = self._incoming_buffer.strip()
        if not pending:
            self._incoming_buffer = ""
            return

        self._handle_incoming_text(pending)
        self._incoming_buffer = ""

    def _handle_incoming_text(self, raw_text: str):
        command = self._normalize_command(raw_text)
        if not command:
            return

        self.log(f"Received from Arduino: {command}")
        if self.command_callback:
            self.command_callback(command)

    @staticmethod
    def _normalize_command(raw_text: str) -> Optional[str]:
        text = str(raw_text or "").strip().upper()
        if text in ("1", "START"):
            return "START"
        if text in ("0", "STOP"):
            return "STOP"
        return None

    def close(self):
        self._closed = True
        self._listen_armed = False
        self._listener_stop_event.set()
        if self.ser:
            try:
                port = self.ser.port
                self.ser.close()
                self.log(f"Closed Arduino serial on {port}")
            except Exception as e:
                self.log(f"Error closing Arduino serial: {e}")
            self.ser = None
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=0.5)


# -----------------------------------
# TCP server
# -----------------------------------
class TcpServer:
    def __init__(self, host, port, log_callback, clients_changed_callback,
                 message_callback, transfer_progress_callback, save_dir_getter):
        self.host = host
        self.port = port
        self.log = log_callback
        self.clients_changed_callback = clients_changed_callback
        self.message_callback = message_callback
        self.transfer_progress_callback = transfer_progress_callback
        self.save_dir_getter = save_dir_getter
        self._running = True

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive_addr_use = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_addr_use is not None:
            self.server_sock.setsockopt(socket.SOL_SOCKET, exclusive_addr_use, 1)
        else:
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen()
        self.log(f"TCP server listening on {self.host}:{self.port}")
        if TCP_KEEPALIVE_ENABLED:
            self.log(
                "TCP keepalive enabled: "
                f"idle={TCP_KEEPALIVE_IDLE_SEC}s "
                f"interval={TCP_KEEPALIVE_INTERVAL_SEC}s "
                f"count={TCP_KEEPALIVE_COUNT}"
            )

        self.clients = []
        self.lock = threading.Lock()

        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while self._running:
            try:
                conn, addr = self.server_sock.accept()
            except OSError:
                break
            self._configure_keepalive(conn)
            conn.settimeout(CLIENT_SOCKET_TIMEOUT_SEC)
            self.log(f"Client connected: {addr}")
            client = {
                "conn": conn,
                "addr": addr,
                "key": f"{addr[0]}:{addr[1]}",
                "name": None,
                "last_name_ok": None,
                "last_name_sent": None,
                "rx_mode": "line",
                "file_bytes_remaining": 0,
                "file_handle": None,
                "file_path": None,
                "file_total": 0,
                "file_received": 0,
                "transfer_total": 0,
                "transfer_received": 0,
                "transfer_active": False,
                "current_file_rel": "",
                "pending_file_done_rel": "",
                "send_queue": queue.Queue(),
                "closed": False,
            }

            with self.lock:
                self.clients.append(client)
            self._notify_clients_changed()

            threading.Thread(target=self._client_send_loop, args=(client,), daemon=True).start()
            threading.Thread(target=self.client_loop, args=(client,), daemon=True).start()

    def client_loop(self, client):
        conn = client["conn"]
        addr = client["addr"]
        buffer = bytearray()

        try:
            with conn:
                while True:
                    try:
                        data = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buffer.extend(data)

                    while True:
                        if client["rx_mode"] == "file":
                            self._receive_file_payload(client, conn, buffer)
                            continue

                        newline = buffer.find(b"\n")
                        if newline < 0:
                            break

                        line = buffer[:newline]
                        del buffer[:newline + 1]
                        line = line.decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue
                        action = self.handle_line(client, line)
                        if action == "START_FILE":
                            self._receive_file_payload(client, conn, buffer)
        except Exception as e:
            self.log(f"Error with client {addr}: {e}")
        finally:
            self._close_open_file(client)
            self.log(f"Client disconnected: {addr}")
            removed = False
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
                    removed = True
            if removed:
                self._notify_clients_changed()
            self._stop_client_sender(client)

    def _close_open_file(self, client):
        if client.get("file_handle"):
            try:
                client["file_handle"].close()
            except Exception:
                pass
        client["file_handle"] = None

    def _receive_file_payload(self, client, conn, buffer):
        remaining = client["file_bytes_remaining"]
        while remaining > 0:
            if buffer:
                take = min(len(buffer), remaining)
                chunk = buffer[:take]
                del buffer[:take]
            else:
                try:
                    chunk = conn.recv(min(65536, remaining))
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("Socket closed during file transfer")

            if client["file_handle"]:
                client["file_handle"].write(chunk)

            remaining -= len(chunk)
            client["file_bytes_remaining"] = remaining
            client["file_received"] += len(chunk)
            client["transfer_received"] += len(chunk)
            self.transfer_progress_callback(client)

        client["pending_file_done_rel"] = client.get("current_file_rel", "")
        self._close_open_file(client)
        client["file_path"] = None
        client["file_total"] = 0
        client["file_received"] = 0
        client["current_file_rel"] = ""
        client["rx_mode"] = "line"

    def handle_line(self, client, line: str):
        addr = client["addr"]
        self.log(f"From {addr}: {_format_client_line_for_log(line)}")

        parts = line.split(" ", 1)
        cmd = parts[0].upper()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "HELLO":
            client["name"] = rest or None
            self._notify_clients_changed()
        elif cmd in ("NAME_OK", "NAMEOK", "NAME-OK"):
            ack_payload, ack_fields = _split_protocol_payload_and_fields(rest)
            ack_name = ack_payload or client.get("last_name_sent")
            client["last_name_ok"] = ack_name or None
            if ack_name:
                details = _format_protocol_timing_for_log(ack_fields)
                detail_text = f" ({details})" if details else ""
                self.log(f"{addr} confirmed name: {ack_name}{detail_text}")
            else:
                self.log(f"{addr} sent NAME_OK without a name (no last name sent)")
        elif cmd == "FILE_BEGIN":
            if client.get("pending_file_done_rel"):
                self.log(
                    f"Missing FILE_DONE for previous file from {addr}: "
                    f"{client['pending_file_done_rel']}"
                )
                client["pending_file_done_rel"] = ""
            parts2 = rest.split(" ", 1)
            if len(parts2) != 2:
                return None
            rel_path = parts2[0].strip()
            size_str = parts2[1].strip()
            try:
                size = int(size_str)
            except ValueError:
                return None
            if size < 0:
                self.log(f"Rejected FILE_BEGIN with negative size from {addr}: {line}")
                return None

            save_dir = self.save_dir_getter()
            abs_path = None
            fh = None
            try:
                storage_rel_path = lag_test_storage_relative_path(rel_path)
                abs_path = resolve_safe_transfer_path(save_dir, storage_rel_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                fh = open(abs_path, "wb")
            except ValueError as e:
                self.log(f"Rejected file path from {addr}: {rel_path!r} ({e}); discarding payload")
            except Exception as e:
                self.log(f"Failed to open file for write: {abs_path} err={e}; discarding payload")

            client["file_handle"] = fh
            client["file_path"] = abs_path
            client["file_total"] = size
            client["file_received"] = 0
            client["file_bytes_remaining"] = size
            client["current_file_rel"] = rel_path
            client["rx_mode"] = "file"
            self.transfer_progress_callback(client)
            return "START_FILE"
        elif cmd == "FILE_DONE":
            expected_rel = client.get("pending_file_done_rel") or ""
            if expected_rel:
                if rest and rest != expected_rel:
                    self.log(
                        f"FILE_DONE path mismatch from {addr}: "
                        f"expected '{expected_rel}' got '{rest}'"
                    )
                elif not rest:
                    self.log(f"FILE_DONE without path from {addr}")
                client["pending_file_done_rel"] = ""
            else:
                self.log(f"Unexpected FILE_DONE while not awaiting one: {rest or '<missing path>'}")
            self.message_callback(client, line)
        elif cmd == "TRANSFER_BEGIN":
            parts2 = rest.split(" ")
            if len(parts2) >= 3:
                try:
                    client["transfer_total"] = int(parts2[2])
                except Exception:
                    client["transfer_total"] = 0
            client["transfer_received"] = 0
            client["transfer_active"] = True
            self.transfer_progress_callback(client)
            self.message_callback(client, line)
        elif cmd == "TRANSFER_DONE":
            client["transfer_active"] = False
            self.transfer_progress_callback(client)
            self.message_callback(client, line)
        elif cmd == "TRANSFER_ALL_DONE":
            client["transfer_active"] = False
            self.transfer_progress_callback(client)
            self.message_callback(client, line)
        elif cmd == "TRANSFER_ERR":
            client["transfer_active"] = False
            self.transfer_progress_callback(client)
            self.message_callback(client, line)
        else:
            self.message_callback(client, line)

        return None

    def _configure_keepalive(self, conn: socket.socket):
        if not TCP_KEEPALIVE_ENABLED:
            return
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception as e:
            self.log(f"Keepalive setup failed (SO_KEEPALIVE): {e}")
            return

        idle = int(TCP_KEEPALIVE_IDLE_SEC)
        interval = int(TCP_KEEPALIVE_INTERVAL_SEC)
        count = int(TCP_KEEPALIVE_COUNT)

        if os.name == "nt" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            try:
                conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
            except Exception as e:
                self.log(f"Keepalive setup failed (SIO_KEEPALIVE_VALS): {e}")
            return

        try:
            opt_idle = getattr(socket, "TCP_KEEPIDLE", None)
            opt_keepalive = getattr(socket, "TCP_KEEPALIVE", None)
            if opt_idle is not None:
                conn.setsockopt(socket.IPPROTO_TCP, opt_idle, idle)
            elif opt_keepalive is not None:
                conn.setsockopt(socket.IPPROTO_TCP, opt_keepalive, idle)

            opt_intvl = getattr(socket, "TCP_KEEPINTVL", None)
            if opt_intvl is not None:
                conn.setsockopt(socket.IPPROTO_TCP, opt_intvl, interval)

            opt_cnt = getattr(socket, "TCP_KEEPCNT", None)
            if opt_cnt is not None:
                conn.setsockopt(socket.IPPROTO_TCP, opt_cnt, count)
        except Exception as e:
            self.log(f"Keepalive setup failed (TCP options): {e}")

    def _clients_info(self):
        infos = []
        with self.lock:
            clients = list(self.clients)

        for c in clients:
            ip, port = c["addr"]
            name = c["name"] or "Unknown"
            infos.append((c, f"{name} ({ip}:{port})"))
        return infos

    def _notify_clients_changed(self):
        infos = self._clients_info()
        self.clients_changed_callback(infos)

    def _remove_client(self, client, reason: str):
        conn = client.get("conn")
        addr = client.get("addr")
        removed = False

        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
                removed = True

        if conn:
            try:
                conn.close()
            except Exception:
                pass

        if removed:
            self.log(f"Disconnected client {addr}: {reason}")
            self._notify_clients_changed()
        self._stop_client_sender(client)

    def _stop_client_sender(self, client):
        client["closed"] = True
        send_queue = client.get("send_queue")
        if send_queue is not None:
            try:
                send_queue.put_nowait(None)
            except Exception:
                pass

    def _client_send_loop(self, client):
        send_queue = client["send_queue"]
        while True:
            item = send_queue.get()
            if item is None:
                break

            data = item["data"]
            name_payload = item.get("name_payload")

            try:
                client["conn"].sendall(data)
                if name_payload:
                    client["last_name_sent"] = name_payload
            except Exception as e:
                self.log(f"Send error {client['addr']}: {e}")
                self._remove_client(client, f"send failure: {e}")
                break

    def _enqueue_send(self, client, data: bytes, name_payload: Optional[str] = None):
        if client.get("closed"):
            return
        send_queue = client.get("send_queue")
        if send_queue is None:
            return
        send_queue.put({"data": data, "name_payload": name_payload})

    def broadcast(self, msg: str):
        data = (msg + "\n").encode("utf-8")
        name_payload = None
        if msg.upper().startswith("NAME "):
            name_payload = msg[5:].strip()

        with self.lock:
            clients = list(self.clients)

        for client in clients:
            self._enqueue_send(client, data, name_payload=name_payload)

    def send_to_client(self, client, msg: str):
        data = (msg + "\n").encode("utf-8")
        self._enqueue_send(client, data)

    def send_name_to_unacked(self, name: str) -> int:
        data = f"NAME {name}\n".encode("utf-8")
        send_count = 0

        with self.lock:
            clients = [c for c in self.clients if c.get("last_name_ok") != name]

        for client in clients:
            self._enqueue_send(client, data, name_payload=name)
            send_count += 1

        return send_count

    def close(self):
        self._running = False
        try:
            self.server_sock.close()
        except Exception:
            pass

        with self.lock:
            clients = list(self.clients)
            self.clients.clear()

        for client in clients:
            self._close_open_file(client)
            conn = client.get("conn")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            self._stop_client_sender(client)

        if clients:
            self._notify_clients_changed()


# -----------------------------------
# UDP discovery responder
# -----------------------------------
class UdpDiscoveryResponder:
    def __init__(self, listen_port: int, reply_tcp_port: int, log_callback):
        self.listen_port = listen_port
        self.reply_tcp_port = reply_tcp_port
        self.log = log_callback
        self._running = True

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", self.listen_port))
        self.sock.settimeout(1.0)

        threading.Thread(target=self._loop, daemon=True).start()
        self.log(f"UDP discovery listening on 0.0.0.0:{self.listen_port}")

    def _loop(self):
        while self._running:
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            msg = data.decode("utf-8", errors="ignore").strip()
            if msg == DISCOVERY_REQUEST:
                reply = f"{DISCOVERY_RESPONSE_PREFIX} {self.reply_tcp_port}\n"
                try:
                    self.sock.sendto(reply.encode("utf-8"), addr)
                    self.log(f"Discovery request from {addr}, replied with TCP port {self.reply_tcp_port}")
                except Exception as e:
                    self.log(f"UDP discovery reply error to {addr}: {e}")

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass


# -----------------------------------
# GUI
# -----------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.app_config, self.config_messages = load_app_config()
        self.app_state, state_messages = load_app_state()
        self.config_messages.extend(state_messages)
        self.camera_defaults = self.app_config["camera_defaults"]
        saved_camera_profile = normalize_camera_profile(
            self.app_state.get("camera_profile")
        )
        if self.app_state.get("camera_profile") and saved_camera_profile is None:
            self.config_messages.append(
                "Saved camera profile was invalid; using first-run camera defaults"
            )
        phone_stream_config_raw = self.app_config.get("phone_stream")
        if not isinstance(phone_stream_config_raw, dict):
            phone_stream_config_raw = {}
        self.phone_stream_config = PhoneStreamConfig.from_mapping(
            phone_stream_config_raw,
            APP_ROOT,
        )
        self.phone_stream_enabled = bool(self.phone_stream_config.enabled)
        self.phone_stream_available = (
            self.phone_stream_enabled and PhoneStreamPane is not None
        )
        if self.phone_stream_enabled and not self.phone_stream_available:
            self.config_messages.append(
                "Phone stream is enabled but unavailable: "
                f"{PHONE_STREAM_IMPORT_ERROR}"
            )
        self.name_separator = self.app_config["name_separator"]
        self.name_field_defs = self.app_config["name_fields"]
        self.name_field_defs_by_key = {
            field["key"]: field for field in self.name_field_defs
        }
        self.naming_vars = {}
        self.naming_widgets = {}
        self.naming_lock_buttons = {}
        self.naming_lock_states = {}
        self._is_fullscreen = False
        self.root.title("CaptureBridge Hub")
        self.root.geometry("2480x1240")
        self.root.minsize(1120, 560)
        self.open_window_maximized()

        self.is_running = False
        self.transfer_in_progress = False
        self.serial_arm_enabled = False
        self._naming_update_after_id = None
        self._name_keepalive_after_id = None
        self._camera_apply_after_id = None
        self._camera_verify_after_id = None
        self._save_dir_update_after_id = None
        self._last_name_resend_log_time = 0.0
        self._delete_watchdog_after_id = None
        self._pending_delete_client = None
        self._pending_delete_command = None
        self._progress_ui_after_id = None
        self._latest_progress_client = None
        self._progress_lock = threading.Lock()
        self._progress_dispatch_pending = False
        self._syncing_client_selection = False
        self._suppress_camera_change_events = False
        self._camera_form_dirty = False
        self._tooltips = []
        self._log_line_count = 0
        self._local_file_match_cache = {}
        self._save_dir_lock = threading.Lock()
        self._save_dir_path = resolve_configured_path(self.app_config["default_save_path"])

        self.client_entries = []
        self.selected_client = None
        self.captures_by_client = {}
        self.transfer_error_by_client = {}
        self.camera_settings_by_client = {}
        self.camera_error_by_client = {}
        self.phone_stream_states_by_client = {}
        self.camera_desired_profile = saved_camera_profile
        self.active_capture_name = None
        self.last_completed_capture_name = None
        self._pending_capture_completion_name = None
        self._pending_capture_client_keys = set()
        self._pending_capture_stop_ok_keys = set()
        self._pending_capture_preview_keys = set()
        self._pending_capture_ready_keys = set()
        self._pending_capture_finalize_after_ids = []
        self.phone_selector_labels = []
        self.delete_unlock_states = {}
        self.delete_unlock_buttons = {}
        self.delete_action_buttons = {}
        self.phone_stream_pane = None
        self._lag_test_session = None
        self.tcp = None
        self.discovery = None
        self.phone_connection_enabled = False

        # Main 3-column layout: controls and logs on the left, shared settings in the
        # middle, and selected-phone controls on the right.
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        main_frame.columnconfigure(0, weight=2, minsize=300)
        main_frame.columnconfigure(1, weight=4, minsize=430)
        main_frame.columnconfigure(2, weight=6, minsize=560)
        main_frame.rowconfigure(0, weight=1)

        left_frame = tk.Frame(main_frame)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)

        # Logging area
        self.log_box = scrolledtext.ScrolledText(
            left_frame,
            wrap=tk.WORD,
            width=42,
            height=10,
            state=tk.DISABLED,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", pady=(0, 4))

        # Connected clients list
        clients_frame = tk.Frame(left_frame)
        clients_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        clients_header_frame = tk.Frame(clients_frame)
        clients_header_frame.pack(fill=tk.X)
        tk.Label(clients_header_frame, text="Connected phones:").pack(side=tk.LEFT, anchor="w")

        self.phone_disconnect_btn = tk.Button(
            clients_header_frame,
            text="Disconnect",
            width=10,
            command=self.toggle_phone_connection,
        )
        self.phone_disconnect_btn.pack(side=tk.RIGHT, padx=(4, 0))

        tk.Label(clients_header_frame, text="ms").pack(side=tk.RIGHT, padx=(2, 6))
        initial_lead_ms = self._normalize_phone_start_lead_ms(
            self.app_state.get("phone_start_lead_ms", 0.0),
            persist=False,
        )
        self.phone_start_lead_ms_var = tk.StringVar(
            value=compact_float_text(initial_lead_ms, precision=1)
        )
        self.phone_start_lead_ms_entry = tk.Entry(
            clients_header_frame,
            width=5,
            textvariable=self.phone_start_lead_ms_var,
        )
        self.phone_start_lead_ms_entry.pack(side=tk.RIGHT)
        self.phone_start_lead_ms_entry.bind(
            "<FocusOut>",
            lambda _event: self._normalize_phone_start_lead_ms(
                self.phone_start_lead_ms_var.get()
            ),
        )
        self.phone_start_lead_ms_entry.bind(
            "<Return>",
            lambda _event: (
                self._normalize_phone_start_lead_ms(self.phone_start_lead_ms_var.get()),
                "break",
            )[-1],
        )
        tk.Label(clients_header_frame, text="Lead:").pack(side=tk.RIGHT, padx=(6, 2))

        self.lag_test_btn = tk.Button(
            clients_header_frame,
            text="Lag Test",
            width=8,
            command=self.on_lag_test,
        )
        self.lag_test_btn.pack(side=tk.RIGHT)

        self.clients_list = tk.Listbox(clients_frame, height=4)
        self.clients_list.pack(fill=tk.X)
        self.clients_list.bind("<<ListboxSelect>>", self.on_client_selected)

        self.lag_test_status_var = tk.StringVar(value="Lag test idle")
        self.lag_test_status_label = tk.Label(
            clients_frame,
            textvariable=self.lag_test_status_var,
            anchor="w",
            justify=tk.LEFT,
        )
        self.lag_test_status_label.pack(fill=tk.X, pady=(2, 0))

        # Info label
        info_frame = tk.Frame(left_frame)
        info_frame.grid(row=2, column=0, sticky="ew")

        self.info_label = tk.Label(
            info_frame,
            text=f"Server: {SERVER_HOST}:{SERVER_PORT}",
            anchor="w",
        )
        self.info_label.pack(side=tk.LEFT)

        # Buttons
        btn_frame = tk.Frame(left_frame)
        btn_frame.grid(row=3, column=0, pady=8, sticky="w")

        self.start_btn = tk.Button(btn_frame, text="START", width=8, command=self.on_start)
        self.start_btn.grid(row=0, column=0, padx=(0, 5))

        self.stop_btn = tk.Button(btn_frame, text="STOP", width=8, command=self.on_stop)
        self.stop_btn.grid(row=0, column=1, padx=5)

        self.arm_btn = tk.Button(btn_frame, text="ARM", width=8, command=self.on_toggle_arm)
        self.arm_btn.grid(row=0, column=2, padx=5)

        general_frame = tk.LabelFrame(main_frame, text="General", padx=8, pady=8)
        general_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        general_frame.columnconfigure(0, weight=1)
        general_frame.rowconfigure(1, weight=1)

        general_transfer_frame = tk.LabelFrame(general_frame, text="Transfer", padx=8, pady=8)
        general_transfer_frame.grid(row=0, column=0, sticky="ew")
        general_transfer_frame.columnconfigure(1, weight=1)

        tk.Label(general_transfer_frame, text="Save path:").grid(row=0, column=0, sticky="w", pady=2)
        self.save_dir_var = tk.StringVar(value=self.app_config["default_save_path"])
        self.save_dir_entry = tk.Entry(general_transfer_frame, textvariable=self.save_dir_var)
        self.save_dir_entry.grid(row=0, column=1, sticky="ew", pady=2)
        self.browse_btn = tk.Button(general_transfer_frame, text="Browse", command=self.on_browse)
        self.browse_btn.grid(row=0, column=2, padx=(6, 0))
        self.save_dir_var.trace_add("write", self._on_save_dir_changed)

        general_transfer_actions_frame = tk.Frame(general_transfer_frame)
        general_transfer_actions_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        general_transfer_actions_frame.columnconfigure(0, weight=1)
        general_transfer_actions_frame.columnconfigure(1, weight=1)

        self.transfer_current_all_btn = tk.Button(
            general_transfer_actions_frame,
            text="Transfer Current On All",
            command=self.on_transfer_current_all,
        )
        self.transfer_current_all_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.transfer_all_all_btn = tk.Button(
            general_transfer_actions_frame,
            text="Transfer All On All",
            command=self.on_transfer_all_all,
        )
        self.transfer_all_all_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        global_delete_current_frame = tk.Frame(general_transfer_frame)
        global_delete_current_frame.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.global_delete_current_unlock_btn = tk.Button(
            global_delete_current_frame,
            text="Unlock",
            width=8,
            command=lambda: self.toggle_delete_unlock("global_delete_current"),
        )
        self.global_delete_current_unlock_btn.pack(side=tk.LEFT)
        self.global_delete_current_btn = tk.Button(
            global_delete_current_frame,
            text="Delete Current On All",
            command=self.on_delete_current_all,
            state=tk.DISABLED,
        )
        self.global_delete_current_btn.pack(side=tk.LEFT, padx=(6, 0))

        global_delete_all_frame = tk.Frame(general_transfer_frame)
        global_delete_all_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.global_delete_all_unlock_btn = tk.Button(
            global_delete_all_frame,
            text="Unlock",
            width=8,
            command=lambda: self.toggle_delete_unlock("global_delete_all"),
        )
        self.global_delete_all_unlock_btn.pack(side=tk.LEFT)
        self.global_delete_all_btn = tk.Button(
            global_delete_all_frame,
            text="Delete All On All",
            command=self.on_delete_all_all,
            state=tk.DISABLED,
        )
        self.global_delete_all_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.transfer_sync_status_var = tk.StringVar(value="No completed capture yet")
        self.transfer_sync_status_label = tk.Label(
            general_transfer_frame,
            textvariable=self.transfer_sync_status_var,
            anchor="w",
            justify=tk.LEFT,
        )
        self.transfer_sync_status_label.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        self.transfer_sync_details_var = tk.StringVar(value="No phone connected")
        self.transfer_sync_details_label = tk.Label(
            general_transfer_frame,
            textvariable=self.transfer_sync_details_var,
            anchor="w",
            justify=tk.LEFT,
        )
        self.transfer_sync_details_label.grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(4, 0)
        )

        naming_frame = tk.LabelFrame(general_frame, text="Naming", padx=8, pady=8)
        naming_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        naming_frame.columnconfigure(1, weight=1)
        naming_frame.columnconfigure(2, weight=0)

        self.generated_name_var = tk.StringVar(value="")
        generated_row = self.build_naming_fields_ui(naming_frame)

        tk.Label(naming_frame, text="Generated:").grid(
            row=generated_row, column=0, sticky="w", pady=(8, 2)
        )
        self.generated_entry = tk.Entry(
            naming_frame,
            textvariable=self.generated_name_var,
            state="readonly",
        )
        self.generated_entry.grid(
            row=generated_row, column=1, columnspan=2, sticky="ew", pady=(8, 2)
        )

        camera_frame = tk.LabelFrame(general_frame, text="Camera Settings", padx=8, pady=8)
        camera_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        camera_frame.columnconfigure(1, weight=1)

        self.resolution_var = tk.StringVar(value="")
        self.fps_var = tk.StringVar(value="")
        initial_iso = self.camera_defaults["iso"]
        initial_shutter = default_shutter_seconds_for_fps(
            60.0,
            self.camera_defaults["shutter_fps_multiplier"],
        )
        if self.camera_desired_profile is not None:
            initial_iso = self.camera_desired_profile["iso"]
            initial_shutter = self.camera_desired_profile["shutterSeconds"]
        self.iso_var = tk.StringVar(value=compact_float_text(initial_iso, precision=2))
        self.shutter_var = tk.StringVar(value=format_shutter_seconds(initial_shutter))
        self.camera_status_var = tk.StringVar(value="No phone connected")
        self.resolution_groups = {}
        self.fps_option_map = {}
        self.resolution_var.trace_add("write", self._on_camera_setting_changed)
        self.fps_var.trace_add("write", self._on_camera_setting_changed)
        self.iso_var.trace_add("write", self._on_camera_setting_changed)
        self.shutter_var.trace_add("write", self._on_camera_setting_changed)

        tk.Label(camera_frame, text="Resolution:").grid(row=0, column=0, sticky="w", pady=2)
        self.resolution_combo = ttk.Combobox(
            camera_frame,
            textvariable=self.resolution_var,
            state="disabled",
            values=[],
        )
        self.resolution_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.resolution_combo.bind("<<ComboboxSelected>>", self.on_resolution_selected)

        tk.Label(camera_frame, text="FPS:").grid(row=1, column=0, sticky="w", pady=2)
        self.fps_combo = ttk.Combobox(
            camera_frame,
            textvariable=self.fps_var,
            state="disabled",
            values=[],
        )
        self.fps_combo.grid(row=1, column=1, sticky="ew", pady=2)

        self.iso_label = tk.Label(camera_frame, text="ISO:")
        self.iso_label.grid(row=2, column=0, sticky="w", pady=2)
        self.iso_entry = tk.Entry(camera_frame, textvariable=self.iso_var)
        self.iso_entry.grid(row=2, column=1, sticky="ew", pady=2)

        self.shutter_label = tk.Label(camera_frame, text="Shutter:")
        self.shutter_label.grid(row=3, column=0, sticky="w", pady=2)
        self.shutter_entry = tk.Entry(camera_frame, textvariable=self.shutter_var)
        self.shutter_entry.grid(row=3, column=1, sticky="ew", pady=2)
        self._register_tooltip(
            self.iso_label,
            "Camera sensitivity. Higher ISO makes the image brighter but can add more noise. Enter a positive number, e.g. 2000.",
        )
        self._register_tooltip(
            self.shutter_label,
            "Exposure time. Faster shutter reduces motion blur but makes the image darker. Enter seconds like 0.004 or a fraction like 1/240.",
        )

        self.refresh_camera_btn = tk.Button(
            camera_frame, text="Refresh", command=self.on_refresh_camera_settings
        )
        self.refresh_camera_btn.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 2))

        self.camera_status_label = tk.Label(
            camera_frame, textvariable=self.camera_status_var, anchor="w", justify=tk.LEFT
        )
        self.camera_status_label.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        phone_frame = tk.LabelFrame(main_frame, text="Phone Specific", padx=8, pady=8)
        phone_frame.grid(row=0, column=2, sticky="nsew")
        phone_frame.columnconfigure(0, weight=1)
        phone_frame.rowconfigure(1, weight=5)
        phone_frame.rowconfigure(2, weight=1)

        phone_selector_frame = tk.Frame(phone_frame)
        phone_selector_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        phone_selector_frame.columnconfigure(1, weight=1)

        tk.Label(phone_selector_frame, text="Phone:").grid(row=0, column=0, sticky="w", pady=2)
        self.phone_selector_var = tk.StringVar(value="")
        self.phone_selector_combo = ttk.Combobox(
            phone_selector_frame,
            textvariable=self.phone_selector_var,
            state="disabled",
            values=[],
        )
        self.phone_selector_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.phone_selector_combo.bind("<<ComboboxSelected>>", self.on_phone_selector_changed)

        stream_row = 1
        transfer_row = 2 if self.phone_stream_enabled else 1
        camera_sync_row = transfer_row + 1
        if self.phone_stream_enabled:
            if self.phone_stream_available:
                try:
                    self.phone_stream_pane = PhoneStreamPane(
                        root=self.root,
                        parent=phone_frame,
                        config=self.phone_stream_config,
                        clients_getter=lambda: list(self.client_entries),
                        client_label_getter=self.get_phone_stream_client_label,
                        start_stream_callback=self.send_phone_stream_start,
                        stop_stream_callback=self.send_phone_stream_stop,
                        log_callback=self.log,
                    )
                    self.phone_stream_pane.frame.grid(row=stream_row, column=0, sticky="nsew")
                except Exception as exc:
                    self.phone_stream_pane = None
                    self.phone_stream_available = False
                    self.config_messages.append(f"Phone stream could not start: {exc}")
            if self.phone_stream_pane is None:
                stream_unavailable_frame = tk.LabelFrame(
                    phone_frame,
                    text="Live Preview",
                    padx=8,
                    pady=8,
                )
                stream_unavailable_frame.grid(row=stream_row, column=0, sticky="nsew")
                stream_unavailable_frame.columnconfigure(0, weight=1)
                stream_unavailable_frame.rowconfigure(0, weight=1)
                tk.Label(
                    stream_unavailable_frame,
                    text="Phone stream unavailable",
                    anchor="center",
                    justify=tk.CENTER,
                ).grid(row=0, column=0, sticky="nsew")

        transfer_frame = tk.LabelFrame(phone_frame, text="Transfer", padx=8, pady=8)
        transfer_frame.grid(row=transfer_row, column=0, sticky="nsew", pady=(8, 0))
        transfer_frame.columnconfigure(1, weight=1)
        transfer_frame.rowconfigure(3, weight=1)

        self.refresh_btn = tk.Button(transfer_frame, text="Refresh List", command=self.on_refresh_list)
        self.refresh_btn.grid(row=0, column=0, pady=4, sticky="w")

        self.transfer_selected_btn = tk.Button(
            transfer_frame, text="Transfer Selected", command=self.on_transfer_selected
        )
        self.transfer_selected_btn.grid(row=0, column=1, pady=4, sticky="w")

        self.transfer_all_btn = tk.Button(transfer_frame, text="Transfer All", command=self.on_transfer_all)
        self.transfer_all_btn.grid(row=0, column=2, pady=4, sticky="e")

        delete_selected_frame = tk.Frame(transfer_frame)
        delete_selected_frame.grid(row=1, column=1, pady=4, sticky="w")
        self.delete_selected_unlock_btn = tk.Button(
            delete_selected_frame,
            text="Unlock",
            width=8,
            command=lambda: self.toggle_delete_unlock("selected_delete"),
        )
        self.delete_selected_unlock_btn.pack(side=tk.LEFT)
        self.delete_selected_btn = tk.Button(
            delete_selected_frame,
            text="Delete Selected",
            command=self.on_delete_selected,
            state=tk.DISABLED,
        )
        self.delete_selected_btn.pack(side=tk.LEFT, padx=(6, 0))

        delete_all_frame = tk.Frame(transfer_frame)
        delete_all_frame.grid(row=1, column=2, pady=4, sticky="e")
        self.delete_all_unlock_btn = tk.Button(
            delete_all_frame,
            text="Unlock",
            width=8,
            command=lambda: self.toggle_delete_unlock("selected_delete_all"),
        )
        self.delete_all_unlock_btn.pack(side=tk.LEFT)
        self.delete_all_btn = tk.Button(
            delete_all_frame,
            text="Delete All",
            command=self.on_delete_all,
            state=tk.DISABLED,
        )
        self.delete_all_btn.pack(side=tk.LEFT, padx=(6, 0))

        tk.Label(transfer_frame, text="Captures [local/phone files]:").grid(row=2, column=0, sticky="w")
        self.captures_list = tk.Listbox(transfer_frame, height=7)
        self.captures_list.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(2, 6))

        self.progress_label = tk.Label(transfer_frame, text="No transfer")
        self.progress_label.grid(row=4, column=0, columnspan=3, sticky="w")

        self.progress_bar = ttk.Progressbar(transfer_frame, length=260)
        self.progress_bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        camera_sync_frame = tk.LabelFrame(phone_frame, text="Camera Sync", padx=8, pady=8)
        camera_sync_frame.grid(row=camera_sync_row, column=0, sticky="ew", pady=(8, 0))
        camera_sync_frame.columnconfigure(0, weight=1)

        self.camera_sync_details_var = tk.StringVar(value="No phone connected")
        self.camera_sync_details_label = tk.Label(
            camera_sync_frame,
            textvariable=self.camera_sync_details_var,
            anchor="w",
            justify=tk.LEFT,
        )
        self.camera_sync_details_label.grid(row=0, column=0, sticky="ew")

        self.register_delete_action(
            "global_delete_current",
            self.global_delete_current_unlock_btn,
            self.global_delete_current_btn,
        )
        self.register_delete_action(
            "global_delete_all",
            self.global_delete_all_unlock_btn,
            self.global_delete_all_btn,
        )
        self.register_delete_action(
            "selected_delete",
            self.delete_selected_unlock_btn,
            self.delete_selected_btn,
        )
        self.register_delete_action(
            "selected_delete_all",
            self.delete_all_unlock_btn,
            self.delete_all_btn,
        )

        # TCP/UDP phone connection + Arduino
        self.connect_phone_network(log_success=False)
        self.arduino = ArduinoController(self.log, command_callback=self.on_arduino_serial_command)

        self.update_start_stop_buttons()
        self.apply_initial_field_locks()
        self.update_generated_name()
        self.schedule_name_keepalive()
        self.flush_config_messages()

        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ------- naming helpers -------

    def _register_tooltip(self, widget, text: str, delay_ms: int = 400):
        tooltip = HoverTooltip(widget, text=text, delay_ms=delay_ms)
        self._tooltips.append(tooltip)
        return tooltip

    def open_window_maximized(self):
        try:
            self.root.state("zoomed")
        except tk.TclError:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            self.root.geometry(f"{screen_width}x{screen_height}+0+0")

    def toggle_fullscreen(self, _event=None):
        self._is_fullscreen = not self._is_fullscreen
        self.root.attributes("-fullscreen", self._is_fullscreen)
        return "break"

    def exit_fullscreen(self, _event=None):
        if not self._is_fullscreen:
            return None

        self._is_fullscreen = False
        self.root.attributes("-fullscreen", False)
        self.open_window_maximized()
        return "break"

    def flush_config_messages(self):
        for message in self.config_messages:
            self.log(message)

    def build_naming_fields_ui(self, naming_frame) -> int:
        row = 0

        for field_def in self.name_field_defs:
            key = field_def["key"]
            var = tk.StringVar(value=field_def["default"])
            self.naming_vars[key] = var
            var.trace_add("write", self._on_naming_field_changed)

            tk.Label(naming_frame, text=f"{field_def['label']}:").grid(
                row=row, column=0, sticky="w", pady=2
            )

            if field_def["type"] == "choice":
                widget = ttk.Combobox(
                    naming_frame,
                    textvariable=var,
                    values=field_def["options"],
                    state="normal" if field_def.get("allow_custom") else "readonly",
                    width=12,
                )
            elif field_def["type"] == "number":
                validate_numeric = (
                    self.root.register(
                        lambda proposed, field_key=key: self._validate_numeric_field_input(
                            proposed, field_key
                        )
                    ),
                    "%P",
                )
                widget = tk.Entry(
                    naming_frame,
                    textvariable=var,
                    width=max(field_def.get("pad_to", 3) + 2, 10),
                    validate="key",
                    validatecommand=validate_numeric,
                )
            else:
                widget = tk.Entry(naming_frame, textvariable=var, width=16)

            widget.grid(row=row, column=1, sticky="ew", pady=2)
            self.naming_widgets[key] = widget

            if field_def.get("lockable"):
                button = tk.Button(
                    naming_frame,
                    text="Lock",
                    width=8,
                    command=lambda field_key=key: self.toggle_field_lock(field_key),
                )
                button.grid(row=row, column=2, sticky="e", padx=(6, 0), pady=2)
                self.naming_lock_buttons[key] = button
                self.naming_lock_states[key] = False

            row += 1

        return row

    def sync_selected_client_controls(self):
        selected_index = None
        if self.selected_client in self.client_entries:
            selected_index = self.client_entries.index(self.selected_client)

        self._syncing_client_selection = True
        try:
            self.clients_list.selection_clear(0, tk.END)
            if selected_index is not None:
                self.clients_list.selection_set(selected_index)
                self.clients_list.activate(selected_index)
                self.clients_list.see(selected_index)
                if selected_index < len(self.phone_selector_labels):
                    self.phone_selector_var.set(self.phone_selector_labels[selected_index])
                else:
                    self.phone_selector_var.set("")
            else:
                self.phone_selector_var.set("")
        finally:
            self._syncing_client_selection = False

    def set_selected_client(self, client):
        if client not in self.client_entries:
            client = None

        self.selected_client = client
        self.sync_selected_client_controls()
        self.refresh_captures_list()

    def on_phone_selector_changed(self, _event=None):
        if self._syncing_client_selection:
            return

        idx = self.phone_selector_combo.current()
        if 0 <= idx < len(self.client_entries):
            self.set_selected_client(self.client_entries[idx])

    def register_delete_action(self, action_key: str, unlock_button, action_button):
        self.delete_unlock_states[action_key] = False
        self.delete_unlock_buttons[action_key] = unlock_button
        self.delete_action_buttons[action_key] = action_button
        self.set_delete_action_unlocked(action_key, False)

    def set_delete_action_unlocked(self, action_key: str, unlocked: bool):
        self.delete_unlock_states[action_key] = unlocked
        unlock_button = self.delete_unlock_buttons.get(action_key)
        action_button = self.delete_action_buttons.get(action_key)
        if unlock_button:
            unlock_button.configure(text="Lock" if unlocked else "Unlock")
        if action_button:
            action_button.configure(state=tk.NORMAL if unlocked else tk.DISABLED)

    def toggle_delete_unlock(self, action_key: str):
        self.set_delete_action_unlocked(
            action_key, not self.delete_unlock_states.get(action_key, False)
        )

    def relock_delete_actions(self, *action_keys: str):
        for action_key in action_keys:
            if action_key in self.delete_unlock_states:
                self.set_delete_action_unlocked(action_key, False)

    def apply_initial_field_locks(self):
        for field_def in self.name_field_defs:
            if field_def.get("lockable"):
                self.set_field_locked(
                    field_def["key"],
                    field_def.get("locked_by_default", False),
                    log_errors=False,
                )

    def _validate_numeric_field_input(self, proposed: str, field_key: str) -> bool:
        if proposed == "":
            return True
        field_def = self.name_field_defs_by_key.get(field_key, {})
        return proposed.isdigit() and len(proposed) <= field_def.get("max_digits", 12)

    def _on_naming_field_changed(self, *_):
        self.schedule_naming_update()

    def schedule_naming_update(self):
        if self._naming_update_after_id is not None:
            self.root.after_cancel(self._naming_update_after_id)
        self._naming_update_after_id = self.root.after(
            NAME_EDIT_DEBOUNCE_MS, self.flush_naming_update
        )

    def flush_pending_naming_update(self):
        if self._naming_update_after_id is None:
            return
        try:
            self.root.after_cancel(self._naming_update_after_id)
        except tk.TclError:
            pass
        self._naming_update_after_id = None
        self.flush_naming_update()

    def schedule_name_keepalive(self):
        if self._name_keepalive_after_id is not None:
            self.root.after_cancel(self._name_keepalive_after_id)
        self._name_keepalive_after_id = self.root.after(
            NAME_RESEND_INTERVAL_MS, self._name_keepalive_tick
        )

    def _name_keepalive_tick(self):
        self._name_keepalive_after_id = None
        if self.tcp is None:
            self.schedule_name_keepalive()
            return
        if self._lag_test_session is not None:
            self.schedule_name_keepalive()
            return
        if self._pending_capture_completion_name is not None:
            self.schedule_name_keepalive()
            return

        generated = self.build_generated_name(strict=True, log_errors=False)
        if generated:
            send_count = self.tcp.send_name_to_unacked(generated)
            if send_count:
                now = time.time()
                if now - self._last_name_resend_log_time >= NAME_RESEND_LOG_INTERVAL_SEC:
                    self.log(f"Resent NAME to {send_count} unacked client(s)")
                    self._last_name_resend_log_time = now

        self.schedule_name_keepalive()

    def flush_naming_update(self):
        self._naming_update_after_id = None
        self.update_generated_name()
        self.broadcast_generated_name_on_change()

    def _format_numeric_in_range(self, value: str, field_def: dict, log_errors=True):
        field_name = field_def["label"]
        if not value or not value.isdigit():
            if log_errors:
                self.log(f"{field_name} must be numeric")
            return None
        number = int(value)
        if not field_def["min"] <= number <= field_def["max"]:
            if log_errors:
                min_value = format_number_value(field_def["min"], field_def["pad_to"])
                max_value = format_number_value(field_def["max"], field_def["pad_to"])
                self.log(f"{field_name} must be between {min_value} and {max_value}")
            return None
        return format_number_value(number, field_def["pad_to"])

    def _placeholder_for_field(self, field_def: dict) -> str:
        if field_def["type"] == "number":
            return "?" * max(field_def.get("pad_to", 1), 1)
        return "?"

    def _validate_filename_part(
        self, value: str, field_def: dict, strict=True, log_errors=True
    ) -> Optional[str]:
        field_name = field_def["label"]
        text = str(value or "").strip()

        if not text:
            if strict and log_errors:
                self.log(f"{field_name} is required")
            return None if strict else self._placeholder_for_field(field_def)

        invalid_chars = sorted(
            {ch for ch in text if ch in INVALID_FILENAME_CHARS or ord(ch) < 32}
        )
        if invalid_chars:
            if strict and log_errors:
                self.log(
                    f"{field_name} contains invalid filename characters: {' '.join(invalid_chars)}"
                )
            return None if strict else self._placeholder_for_field(field_def)

        if text.endswith((" ", ".")):
            if strict and log_errors:
                self.log(f"{field_name} cannot end with a space or dot")
            return None if strict else self._placeholder_for_field(field_def)

        return text

    def _build_name_part(self, field_def: dict, strict=True, log_errors=True) -> Optional[str]:
        raw_value = self.naming_vars[field_def["key"]].get().strip()
        field_type = field_def["type"]

        if field_type == "number":
            if strict:
                formatted = self._format_numeric_in_range(
                    raw_value, field_def, log_errors=log_errors
                )
                if formatted is None:
                    return None
                return self._format_name_field_output(
                    formatted, field_def, strict=strict, log_errors=log_errors
                )
            if raw_value.isdigit():
                number = int(raw_value)
                if field_def["min"] <= number <= field_def["max"]:
                    formatted = format_number_value(number, field_def["pad_to"])
                    return self._format_name_field_output(
                        formatted, field_def, strict=strict, log_errors=log_errors
                    )
            return self._format_name_field_output(
                self._placeholder_for_field(field_def),
                field_def,
                strict=strict,
                log_errors=log_errors,
            )

        if field_type == "choice":
            if not raw_value:
                if strict and log_errors:
                    self.log(f"{field_def['label']} is required")
                if strict:
                    return None
                return self._format_name_field_output(
                    self._placeholder_for_field(field_def),
                    field_def,
                    strict=strict,
                    log_errors=log_errors,
                )

            if not field_def.get("allow_custom") and raw_value not in field_def["options"]:
                if strict and log_errors:
                    self.log(
                        f"{field_def['label']} must be one of: {', '.join(field_def['options'])}"
                    )
                if strict:
                    return None
                return self._format_name_field_output(
                    self._placeholder_for_field(field_def),
                    field_def,
                    strict=strict,
                    log_errors=log_errors,
                )

            mapped_value = field_def.get("value_map", {}).get(raw_value, raw_value)
            return self._format_name_field_output(
                mapped_value, field_def, strict=strict, log_errors=log_errors
            )

        return self._format_name_field_output(
            raw_value, field_def, strict=strict, log_errors=log_errors
        )

    def _format_name_field_output(
        self, value: str, field_def: dict, strict=True, log_errors=True
    ) -> Optional[str]:
        validated_value = self._validate_filename_part(
            value, field_def, strict=strict, log_errors=log_errors
        )
        if validated_value is None:
            return None

        output_value = (
            f"{field_def.get('output_prefix', '')}"
            f"{validated_value}"
            f"{field_def.get('output_suffix', '')}"
        )
        if output_value == validated_value:
            return validated_value

        return self._validate_filename_part(
            output_value, field_def, strict=strict, log_errors=log_errors
        )

    def build_generated_name(self, strict=True, log_errors=True):
        parts = []
        for field_def in self.name_field_defs:
            part = self._build_name_part(field_def, strict=strict, log_errors=log_errors)
            if part is None:
                return None
            parts.append(part)
        return self.name_separator.join(parts)

    def update_generated_name(self):
        generated = self.build_generated_name(strict=False)
        self.generated_name_var.set(generated if generated else "")

    def broadcast_generated_name_on_change(self):
        if self.tcp is None:
            return
        generated = self.build_generated_name(strict=True, log_errors=False)
        if generated is None:
            return
        payload = f"NAME {generated}"
        self.tcp.broadcast(payload)
        self.log(f"Sent to phones: {payload}")

    def increment_auto_increment_fields(self):
        for field_def in self.name_field_defs:
            if not field_def.get("auto_increment_on_stop"):
                continue

            key = field_def["key"]
            if field_def.get("lockable") and not self.naming_lock_states.get(key, False):
                self.log(f"{field_def['label']} is unlocked; skipping auto-increment")
                continue

            current_text = self.naming_vars[key].get().strip()
            current = int(current_text) if current_text.isdigit() else field_def["min"]
            next_value = current + 1
            if next_value > field_def["max"]:
                next_value = field_def["min"]
            self.naming_vars[key].set(format_number_value(next_value, field_def["pad_to"]))

    def toggle_field_lock(self, field_key: str):
        self.set_field_locked(field_key, not self.naming_lock_states.get(field_key, False))

    def set_field_locked(self, field_key: str, locked: bool, log_errors=True):
        field_def = self.name_field_defs_by_key.get(field_key)
        widget = self.naming_widgets.get(field_key)
        button = self.naming_lock_buttons.get(field_key)
        if not field_def or not widget or not button or not field_def.get("lockable"):
            return

        if locked:
            formatted = self._format_numeric_in_range(
                self.naming_vars[field_key].get().strip(), field_def, log_errors=log_errors
            )
            if formatted is None:
                self.naming_lock_states[field_key] = False
                widget.configure(state=tk.NORMAL)
                button.configure(text="Lock")
                return
            self.naming_vars[field_key].set(formatted)
            widget.configure(state="readonly")
            button.configure(text="Unlock")
            self.naming_lock_states[field_key] = True
        else:
            widget.configure(state=tk.NORMAL)
            button.configure(text="Lock")
            self.naming_lock_states[field_key] = False
            widget.focus_set()
            widget.selection_range(0, tk.END)

    def _is_ui_thread(self) -> bool:
        return threading.current_thread() is threading.main_thread()

    def _call_on_ui_thread(self, callback: Callable, *args):
        if self._is_ui_thread():
            callback(*args)
            return True
        try:
            self.root.after(0, lambda: callback(*args))
        except tk.TclError:
            pass
        return False

    def has_active_transfer(self) -> bool:
        return any(client.get("transfer_active") for client in self.client_entries)

    def invalidate_local_file_match_cache(self):
        self._local_file_match_cache.clear()

    def update_start_stop_buttons(self):
        self.transfer_in_progress = self.has_active_transfer()
        disabled = self.transfer_in_progress
        camera_block_reason = self.get_camera_start_block_reason()
        capture_finalizing = self._pending_capture_completion_name is not None
        start_disabled = (
            self.tcp is None
            or self.is_running
            or capture_finalizing
            or disabled
            or self.serial_arm_enabled
            or camera_block_reason is not None
        )
        self.start_btn.configure(state=tk.DISABLED if start_disabled else tk.NORMAL)
        self.stop_btn.configure(
            state=tk.NORMAL if self.tcp is not None and self.is_running and not disabled else tk.DISABLED
        )
        self.arm_btn.configure(text="DISARM" if self.serial_arm_enabled else "ARM")
        if hasattr(self, "lag_test_btn"):
            lag_disabled = (
                self.tcp is None
                or self.is_running
                or capture_finalizing
                or disabled
                or self._lag_test_session is not None
                or not self.client_entries
            )
            self.lag_test_btn.configure(state=tk.DISABLED if lag_disabled else tk.NORMAL)

    # ------- camera settings helpers -------

    def camera_mode_key(self, option: dict) -> Tuple[int, int, int]:
        width = int(option.get("width", 0) or 0)
        height = int(option.get("height", 0) or 0)
        fps_key = int(round(float(option.get("fps", 0.0) or 0.0) * 100))
        return width, height, fps_key

    def format_resolution_option(self, option: dict) -> str:
        width = int(option.get("width", 0) or 0)
        height = int(option.get("height", 0) or 0)
        return f"{width} x {height}"

    def format_fps_option(self, option: dict) -> str:
        fps = float(option.get("fps", 0.0) or 0.0)
        fps_text = f"{fps:.2f}".rstrip("0").rstrip(".")
        return f"{fps_text} fps"

    def build_camera_profile_from_current(self, payload) -> Optional[dict]:
        current = payload.get("current", {})
        width = current.get("width")
        height = current.get("height")
        if width is None or height is None:
            return None

        profile = {
            "width": int(width),
            "height": int(height),
            "fps": None,
            "iso": None,
            "shutterSeconds": None,
        }
        if current.get("fps") is not None:
            profile["fps"] = float(current.get("fps"))
        if current.get("iso") is not None:
            profile["iso"] = float(current.get("iso"))
        if current.get("shutterSeconds") is not None:
            profile["shutterSeconds"] = float(current.get("shutterSeconds"))
        return profile

    def camera_profiles_match(self, left: Optional[dict], right: Optional[dict]) -> bool:
        if not left or not right:
            return False
        if int(left.get("width", 0) or 0) != int(right.get("width", 0) or 0):
            return False
        if int(left.get("height", 0) or 0) != int(right.get("height", 0) or 0):
            return False

        for field_name, tolerance in (("fps", 0.01), ("iso", 0.01), ("shutterSeconds", 1e-6)):
            left_value = left.get(field_name)
            right_value = right.get(field_name)
            if left_value is None or right_value is None:
                continue
            if abs(float(left_value) - float(right_value)) > tolerance:
                return False
        return True

    def get_supported_camera_modes(self, payload) -> Dict[Tuple[int, int, int], dict]:
        supported = {}
        for option in payload.get("resolutions", []):
            key = self.camera_mode_key(option)
            if key not in supported:
                supported[key] = {
                    "width": int(option.get("width", 0) or 0),
                    "height": int(option.get("height", 0) or 0),
                    "fps": float(option.get("fps", 0.0) or 0.0),
                }
        return supported

    def format_camera_modes_summary(self, payload, max_resolutions: int = 4) -> str:
        grouped = {}
        for option in payload.get("resolutions", []):
            resolution_label = self.format_resolution_option(option)
            fps_label = self.format_fps_option(option)
            grouped.setdefault(resolution_label, set()).add(fps_label)

        if not grouped:
            return "no reported modes"

        def resolution_sort_key(item):
            label, _fps_labels = item
            try:
                width_text, height_text = label.split(" x ", 1)
                return int(width_text) * int(height_text), int(width_text), int(height_text)
            except Exception:
                return 0, 0, 0

        parts = []
        for resolution_label, fps_labels in sorted(
            grouped.items(),
            key=resolution_sort_key,
            reverse=True,
        )[:max_resolutions]:
            def fps_sort_key(label):
                try:
                    return float(label.split(" ", 1)[0])
                except Exception:
                    return 0.0

            fps_text = "/".join(sorted(fps_labels, key=fps_sort_key))
            parts.append(f"{resolution_label}@{fps_text}")

        omitted = max(0, len(grouped) - max_resolutions)
        if omitted:
            parts.append(f"+{omitted} more")
        return ", ".join(parts)

    def payload_supports_profile(self, payload, profile: Optional[dict]) -> bool:
        if profile is None:
            return False
        return self.camera_mode_key(profile) in self.get_supported_camera_modes(payload)

    def get_common_camera_options(self) -> List[dict]:
        loaded_clients = [client for client in self.client_entries if client["key"] in self.camera_settings_by_client]
        if not loaded_clients:
            return []

        common_keys = None
        supported_by_client = {}
        for client in loaded_clients:
            supported = self.get_supported_camera_modes(self.camera_settings_by_client[client["key"]])
            supported_by_client[client["key"]] = supported
            keys = set(supported.keys())
            common_keys = keys if common_keys is None else common_keys & keys

        if not common_keys:
            return []

        common_options = []
        seen = set()
        first_payload = self.camera_settings_by_client[loaded_clients[0]["key"]]
        for option in first_payload.get("resolutions", []):
            key = self.camera_mode_key(option)
            if key in common_keys and key not in seen:
                seen.add(key)
                common_options.append(supported_by_client[loaded_clients[0]["key"]][key])
        return common_options

    def build_resolution_groups(self, options: List[dict]) -> Dict[str, List[dict]]:
        resolution_groups = {}
        for option in options:
            resolution_label = self.format_resolution_option(option)
            resolution_groups.setdefault(resolution_label, [])
            fps_label = self.format_fps_option(option)
            if all(self.format_fps_option(existing) != fps_label for existing in resolution_groups[resolution_label]):
                resolution_groups[resolution_label].append(option)
        for grouped_options in resolution_groups.values():
            grouped_options.sort(
                key=lambda option: float(option.get("fps", 0.0) or 0.0),
                reverse=True,
            )
        return resolution_groups

    def sort_resolution_labels(self, resolution_groups: Dict[str, List[dict]]) -> List[str]:
        preferred_width = int(self.camera_defaults["preferred_width"])
        preferred_height = int(self.camera_defaults["preferred_height"])

        def sort_key(label: str):
            options = resolution_groups.get(label, [])
            if options:
                width = int(options[0].get("width", 0) or 0)
                height = int(options[0].get("height", 0) or 0)
                max_fps = max(float(option.get("fps", 0.0) or 0.0) for option in options)
            else:
                width = height = 0
                max_fps = 0.0
            preferred = (
                (width == preferred_width and height == preferred_height)
                or (width == preferred_height and height == preferred_width)
            )
            return preferred, width * height, max_fps

        return sorted(resolution_groups.keys(), key=sort_key, reverse=True)

    def select_default_camera_option(self, options: List[dict]) -> Optional[dict]:
        if not options:
            return None

        preferred_width = int(self.camera_defaults["preferred_width"])
        preferred_height = int(self.camera_defaults["preferred_height"])

        def is_preferred_1080p(option: dict) -> bool:
            width = int(option.get("width", 0) or 0)
            height = int(option.get("height", 0) or 0)
            return (
                (width == preferred_width and height == preferred_height)
                or (width == preferred_height and height == preferred_width)
            )

        preferred_options = [option for option in options if is_preferred_1080p(option)]
        candidate_options = preferred_options or options

        return max(
            candidate_options,
            key=lambda option: (
                float(option.get("fps", 0.0) or 0.0),
                int(option.get("width", 0) or 0) * int(option.get("height", 0) or 0),
            ),
        )

    def build_default_camera_profile(self, option: dict) -> dict:
        fps = float(option.get("fps", 0.0) or 0.0)
        shutter_seconds = default_shutter_seconds_for_fps(
            fps,
            self.camera_defaults["shutter_fps_multiplier"],
        )
        return {
            "width": int(option["width"]),
            "height": int(option["height"]),
            "fps": fps,
            "iso": float(self.camera_defaults["iso"]),
            "shutterSeconds": shutter_seconds,
        }

    def persist_camera_profile(self, profile: Optional[dict] = None):
        profile = profile if profile is not None else self.camera_desired_profile
        normalized = normalize_camera_profile(profile)
        if normalized is None:
            return

        self.app_state["camera_profile"] = normalized
        try:
            save_app_state(self.app_state)
        except Exception as exc:
            self.log(f"Could not save camera settings for next startup: {exc}")

    def send_camera_profile_to_supported_clients(self, profile: dict) -> int:
        payload_text = json.dumps(profile)
        sent_count = 0
        for client in self.client_entries:
            payload = self.camera_settings_by_client.get(client["key"])
            if payload and self.payload_supports_profile(payload, profile):
                self.tcp.send_to_client(client, f"SETTINGS {payload_text}")
                sent_count += 1
        return sent_count

    def ensure_initial_camera_profile(self) -> bool:
        if (
            self.camera_desired_profile is not None
            or self._camera_form_dirty
            or not self.client_entries
            or len(self.camera_settings_by_client) < len(self.client_entries)
        ):
            return False

        option = self.select_default_camera_option(self.get_common_camera_options())
        if option is None:
            return False

        self.camera_desired_profile = self.build_default_camera_profile(option)
        self.persist_camera_profile(self.camera_desired_profile)
        selected_text = (
            f"{self.format_resolution_option(self.camera_desired_profile)} @ "
            f"{self.format_fps_option(self.camera_desired_profile)}, "
            f"ISO {compact_float_text(self.camera_desired_profile['iso'], precision=2)}, "
            f"shutter {format_shutter_seconds(self.camera_desired_profile['shutterSeconds'])}"
        )
        self.log(f"Selected first-run camera default: {selected_text}")

        sent_count = self.send_camera_profile_to_supported_clients(
            self.camera_desired_profile
        )
        if sent_count:
            self.log(f"Sent default camera settings to {sent_count} phone(s)")
            self.schedule_camera_settings_refresh()
        return True

    def set_camera_controls_from_profile(self, profile: dict):
        self.resolution_var.set(self.format_resolution_option(profile))
        self.populate_fps_choices(
            self.resolution_var.get().strip(),
            preferred_fps=profile.get("fps"),
            keep_current_selection=False,
        )
        self.fps_var.set(self.format_fps_option(profile))
        if profile.get("iso") is not None:
            self.iso_var.set(compact_float_text(float(profile["iso"]), precision=2))
        if profile.get("shutterSeconds") is not None:
            self.shutter_var.set(format_shutter_seconds(float(profile["shutterSeconds"])))

    def infer_global_camera_profile_from_clients(self) -> Optional[dict]:
        if not self.client_entries:
            return self.camera_desired_profile
        if len(self.camera_settings_by_client) < len(self.client_entries):
            return None

        inferred_profile = None
        for client in self.client_entries:
            payload = self.camera_settings_by_client.get(client["key"])
            if not payload:
                return None
            current_profile = self.build_camera_profile_from_current(payload)
            if current_profile is None:
                return None
            if inferred_profile is None:
                inferred_profile = current_profile
            elif not self.camera_profiles_match(inferred_profile, current_profile):
                return None
        return inferred_profile

    def refresh_camera_controls(self):
        common_options = self.get_common_camera_options()
        resolution_groups = self.build_resolution_groups(common_options)
        resolution_labels = self.sort_resolution_labels(resolution_groups)
        preserve_current_form = self._camera_form_dirty

        self._suppress_camera_change_events = True
        try:
            self.resolution_groups = resolution_groups
            self.resolution_combo.configure(
                values=resolution_labels,
                state="readonly" if resolution_labels else "disabled",
            )

            if self.camera_desired_profile is not None and not preserve_current_form:
                self.set_camera_controls_from_profile(self.camera_desired_profile)
            else:
                current_resolution = self.resolution_var.get().strip()
                if not current_resolution and resolution_labels:
                    current_resolution = resolution_labels[0]
                    self.resolution_var.set(current_resolution)
                self.populate_fps_choices(current_resolution, keep_current_selection=True)
        finally:
            self._suppress_camera_change_events = False

    def populate_fps_choices(
        self, resolution_label: str, preferred_fps=None, keep_current_selection=False
    ):
        options = self.resolution_groups.get(resolution_label, [])
        self.fps_option_map = {}
        for option in options:
            self.fps_option_map.setdefault(self.format_fps_option(option), option)

        fps_labels = list(self.fps_option_map.keys())
        self.fps_combo.configure(
            values=fps_labels,
            state="readonly" if fps_labels else "disabled",
        )

        selected_fps_label = ""
        if preferred_fps is not None:
            for label, option in self.fps_option_map.items():
                try:
                    if abs(float(option.get("fps", 0.0) or 0.0) - float(preferred_fps)) < 0.01:
                        selected_fps_label = label
                        break
                except (TypeError, ValueError):
                    continue

        if (
            not selected_fps_label
            and keep_current_selection
            and self.fps_var.get().strip() in self.fps_option_map
        ):
            selected_fps_label = self.fps_var.get().strip()
        if not selected_fps_label and fps_labels:
            selected_fps_label = fps_labels[0]
        if not selected_fps_label and preferred_fps is not None:
            selected_fps_label = self.format_fps_option({"fps": preferred_fps})

        self.fps_var.set(selected_fps_label)

    def get_selected_camera_option(self) -> Optional[dict]:
        resolution_label = self.resolution_var.get().strip()
        fps_label = self.fps_var.get().strip()
        if not resolution_label or not fps_label:
            return None

        for option in self.resolution_groups.get(resolution_label, []):
            if self.format_fps_option(option) == fps_label:
                return option
        return None

    def build_desired_camera_profile(self, log_errors=True) -> Optional[dict]:
        selected_resolution = self.resolution_var.get().strip()
        if not selected_resolution:
            if log_errors:
                self.log("Select a resolution first")
            return None

        selected_fps = self.fps_var.get().strip()
        if not selected_fps:
            if log_errors:
                self.log("Select an FPS first")
            return None

        option = self.get_selected_camera_option()
        if option is None and self.camera_desired_profile is not None:
            desired_resolution = self.format_resolution_option(self.camera_desired_profile)
            desired_fps = self.format_fps_option(self.camera_desired_profile)
            if selected_resolution == desired_resolution and selected_fps == desired_fps:
                option = self.camera_desired_profile
        if option is None:
            if log_errors:
                self.log("Selected resolution/FPS combination is not shared by all loaded phones")
            return None

        iso = self.parse_iso_value(log_errors=log_errors)
        if iso is None:
            return None
        shutter_seconds = self.parse_shutter_seconds(log_errors=log_errors)
        if shutter_seconds is None:
            return None

        return {
            "width": int(option["width"]),
            "height": int(option["height"]),
            "fps": float(option.get("fps", 0.0) or 0.0),
            "iso": iso,
            "shutterSeconds": shutter_seconds,
        }

    def _on_camera_setting_changed(self, *_):
        if self._suppress_camera_change_events:
            return
        self._camera_form_dirty = True
        self.schedule_camera_apply()

    def schedule_camera_apply(self):
        if self._camera_apply_after_id is not None:
            self.root.after_cancel(self._camera_apply_after_id)
        self._camera_apply_after_id = self.root.after(
            CAMERA_APPLY_DEBOUNCE_MS, self.flush_camera_apply
        )

    def flush_camera_apply(self):
        self._camera_apply_after_id = None
        self.apply_camera_settings(log_errors=False)

    def schedule_camera_settings_refresh(self, delay_ms=500):
        if self._camera_verify_after_id is not None:
            self.root.after_cancel(self._camera_verify_after_id)
        self._camera_verify_after_id = self.root.after(
            delay_ms, self._refresh_all_camera_settings_after_delay
        )

    def _refresh_all_camera_settings_after_delay(self):
        self._camera_verify_after_id = None
        self.request_all_camera_settings(log_request=False)

    def parse_iso_value(self, log_errors=True):
        value = self.iso_var.get().strip()
        try:
            iso = float(value)
        except ValueError:
            if log_errors:
                self.log("ISO must be numeric")
            return None
        if iso <= 0:
            if log_errors:
                self.log("ISO must be greater than 0")
            return None
        return iso

    def parse_shutter_seconds(self, log_errors=True):
        value = self.shutter_var.get().strip()
        if not value:
            if log_errors:
                self.log("Shutter is required")
            return None

        try:
            if "/" in value:
                num_text, den_text = value.split("/", 1)
                shutter = float(num_text.strip()) / float(den_text.strip())
            else:
                shutter = float(value)
        except (ValueError, ZeroDivisionError):
            if log_errors:
                self.log("Shutter must be a number like 0.004 or a fraction like 1/240")
            return None

        if shutter <= 0:
            if log_errors:
                self.log("Shutter must be greater than 0")
            return None
        return shutter

    def on_resolution_selected(self, _event=None):
        self.populate_fps_choices(self.resolution_var.get().strip(), keep_current_selection=True)

    def request_camera_settings(self, client, log_request=False) -> bool:
        if not client:
            return False

        self.tcp.send_to_client(client, "SETTINGS_LIST")
        if log_request:
            self.log(f"Sent SETTINGS_LIST to {client['addr'][0]}")
        return True

    def request_all_camera_settings(self, log_request=False) -> bool:
        if not self.client_entries:
            self.refresh_camera_state_ui()
            return False

        for client in self.client_entries:
            self.request_camera_settings(client, log_request=False)
        if log_request:
            self.log(f"Sent SETTINGS_LIST to {len(self.client_entries)} phone(s)")
        self.refresh_camera_state_ui()
        return True

    def auto_apply_camera_profile_to_client(self, client):
        if self.camera_desired_profile is None:
            return

        payload = self.camera_settings_by_client.get(client["key"])
        if not payload or not self.payload_supports_profile(payload, self.camera_desired_profile):
            return

        current_profile = self.build_camera_profile_from_current(payload)
        if self.camera_profiles_match(current_profile, self.camera_desired_profile):
            return

        payload_text = json.dumps(self.camera_desired_profile)
        self.tcp.send_to_client(client, f"SETTINGS {payload_text}")
        self.log(f"Sent camera settings to {client['addr'][0]}: {payload_text}")
        self.schedule_camera_settings_refresh()

    def update_camera_sync_status(self):
        if not self.client_entries:
            if self.camera_desired_profile is None:
                self.camera_status_var.set("No phone connected")
                self.camera_sync_details_var.set("No phone connected")
            else:
                self.camera_status_var.set("No phone connected. Stored global camera profile is ready.")
                self.camera_sync_details_var.set("No phone connected")
            return

        total_clients = len(self.client_entries)
        loaded_count = 0
        synced_count = 0
        pending_count = 0
        unsupported_count = 0
        error_count = 0
        details = []

        for client in self.client_entries:
            payload = self.camera_settings_by_client.get(client["key"])
            position = payload.get("position") if payload else None
            client_name = client.get("name") or client["addr"][0]
            display_name = f"{position} - {client_name}" if position else client_name
            error_text = self.camera_error_by_client.get(client["key"])

            if payload:
                loaded_count += 1

            if error_text:
                error_count += 1
                details.append(f"{display_name}: error ({error_text})")
                continue

            if not payload:
                details.append(f"{display_name}: loading camera settings...")
                continue

            if self.camera_desired_profile is None:
                details.append(f"{display_name}: ready, waiting for a shared camera profile")
                continue

            if not self.payload_supports_profile(payload, self.camera_desired_profile):
                unsupported_count += 1
                details.append(f"{display_name}: does not support the selected mode")
                continue

            current_profile = self.build_camera_profile_from_current(payload)
            if self.camera_profiles_match(current_profile, self.camera_desired_profile):
                synced_count += 1
                details.append(f"{display_name}: synced")
            else:
                pending_count += 1
                details.append(f"{display_name}: pending apply")

        if self._camera_apply_after_id is not None:
            summary = "Queued camera update..."
        elif self.camera_desired_profile is None:
            if loaded_count < total_clients:
                summary = f"Loading camera settings ({loaded_count}/{total_clients})..."
            else:
                summary = "Choose a shared camera profile for all connected phones"
        elif unsupported_count:
            summary = f"Selected camera mode unsupported on {unsupported_count}/{total_clients} phone(s)"
        elif error_count:
            summary = f"Camera settings error on {error_count}/{total_clients} phone(s)"
        elif loaded_count < total_clients:
            summary = f"Applying global profile... waiting for {total_clients - loaded_count} phone(s)"
        elif pending_count:
            summary = f"{synced_count}/{total_clients} phones synced"
        else:
            summary = f"All {total_clients} phones synced"

        self.camera_status_var.set(summary)
        self.camera_sync_details_var.set("\n".join(details) if details else "No phone connected")

    def refresh_camera_state_ui(self):
        self.ensure_initial_camera_profile()
        self.refresh_camera_controls()
        self.update_camera_sync_status()
        self.update_start_stop_buttons()

    def get_camera_start_block_reason(self) -> Optional[str]:
        if self._camera_apply_after_id is not None:
            return "Camera settings update is still queued"
        if not self.client_entries:
            return None
        if self.build_desired_camera_profile(log_errors=False) is None:
            return "Camera settings form is incomplete or invalid"
        if self.camera_desired_profile is None:
            return "Shared camera settings have not been established yet"

        for client in self.client_entries:
            payload = self.camera_settings_by_client.get(client["key"])
            client_name = client.get("name") or client["addr"][0]
            if not payload:
                return f"Waiting for camera settings from {client_name}"
            if self.camera_error_by_client.get(client["key"]):
                return f"Camera settings error on {client_name}"
            if not self.payload_supports_profile(payload, self.camera_desired_profile):
                return f"{client_name} does not support the selected camera mode"
            current_profile = self.build_camera_profile_from_current(payload)
            if not self.camera_profiles_match(current_profile, self.camera_desired_profile):
                return f"Camera settings are not synced on {client_name}"
        return None

    def apply_camera_settings(self, log_errors=True) -> bool:
        desired_profile = self.build_desired_camera_profile(log_errors=log_errors)
        if desired_profile is None:
            self.update_camera_sync_status()
            self.update_start_stop_buttons()
            return False

        unsupported_clients = []
        for client in self.client_entries:
            payload = self.camera_settings_by_client.get(client["key"])
            if payload and not self.payload_supports_profile(payload, desired_profile):
                unsupported_clients.append(client.get("name") or client["addr"][0])

        if unsupported_clients:
            if log_errors:
                self.log(
                    "Selected camera mode is not supported on: "
                    + ", ".join(unsupported_clients)
                )
            self.update_camera_sync_status()
            self.update_start_stop_buttons()
            return False

        self.camera_desired_profile = desired_profile
        self._camera_form_dirty = False
        self.persist_camera_profile(desired_profile)
        sent_count = self.send_camera_profile_to_supported_clients(desired_profile)

        if sent_count:
            self.log(
                "Sent camera settings to "
                f"{sent_count} phone(s): {json.dumps(desired_profile)}"
            )
            self.schedule_camera_settings_refresh()

        self.refresh_camera_state_ui()
        return True

    def on_refresh_camera_settings(self):
        self.request_all_camera_settings(log_request=True)

    # ------- phone stream helpers -------

    def get_phone_stream_client_label(self, client) -> str:
        if not client:
            return "Unknown phone"

        payload = self.camera_settings_by_client.get(client["key"], {})
        position = payload.get("position")
        display_name = client.get("name") or client["addr"][0]
        if position:
            return f"{display_name} [{position}]"
        return display_name

    def get_phone_stream_host_for_client(self, client) -> str:
        try:
            host = client["conn"].getsockname()[0]
        except Exception:
            host = ""
        if host and host != "0.0.0.0":
            return host
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((client["addr"][0], 9))
                host = probe.getsockname()[0]
            finally:
                probe.close()
            if host and host != "0.0.0.0":
                return host
        except Exception:
            pass
        return SERVER_HOST

    def send_phone_stream_start(self, client):
        if not client:
            return

        host = self.get_phone_stream_host_for_client(client)
        if host == SERVER_HOST:
            self.phone_stream_states_by_client[client["key"]] = "Unable to resolve PC LAN address"
            self.log(f"Phone stream host resolution failed for {client['addr'][0]}")
            if self.phone_stream_pane is not None:
                self.phone_stream_pane.set_stream_state(
                    client["key"],
                    "Unable to resolve PC LAN address",
                )
            return

        payload = self.phone_stream_config.build_start_payload(host)
        self.phone_stream_states_by_client[client["key"]] = "Requested"
        self.tcp.send_to_client(client, f"LIVE_PREVIEW_START {payload}")
        self.log(f"Sent to {client['addr'][0]}: LIVE_PREVIEW_START {payload}")
        if self.phone_stream_pane is not None:
            self.phone_stream_pane.set_stream_state(client["key"], "Requested")

    def send_phone_stream_stop(self, client):
        if not client:
            return

        self.phone_stream_states_by_client[client["key"]] = "Stopping"
        self.tcp.send_to_client(client, "LIVE_PREVIEW_STOP")
        self.log(f"Sent to {client['addr'][0]}: LIVE_PREVIEW_STOP")
        if self.phone_stream_pane is not None:
            self.phone_stream_pane.set_stream_state(client["key"], "Stopping")

    # ------- logging helper -------

    def log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}\n"

        def append():
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, line)
            self._log_line_count += 1
            if self._log_line_count > MAX_LOG_LINES + LOG_TRIM_BATCH_LINES:
                self.log_box.delete("1.0", f"{LOG_TRIM_BATCH_LINES + 1}.0")
                self._log_line_count -= LOG_TRIM_BATCH_LINES
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)

        if threading.current_thread() is threading.main_thread():
            append()
        else:
            try:
                self.root.after(0, append)
            except tk.TclError:
                pass

    # ------- client list updates -------

    def on_clients_changed(self, infos):
        def update():
            self.clients_list.delete(0, tk.END)
            self.client_entries = []
            self.phone_selector_labels = []
            for c, s in infos:
                self.clients_list.insert(tk.END, s)
                self.client_entries.append(c)
                self.phone_selector_labels.append(s)

            active_client_keys = {client["key"] for client in self.client_entries}
            self.camera_settings_by_client = {
                key: value
                for key, value in self.camera_settings_by_client.items()
                if key in active_client_keys
            }
            self.camera_error_by_client = {
                key: value
                for key, value in self.camera_error_by_client.items()
                if key in active_client_keys
            }
            self.captures_by_client = {
                key: value
                for key, value in self.captures_by_client.items()
                if key in active_client_keys
            }
            self.transfer_error_by_client = {
                key: value
                for key, value in self.transfer_error_by_client.items()
                if key in active_client_keys
            }
            self.phone_stream_states_by_client = {
                key: value
                for key, value in self.phone_stream_states_by_client.items()
                if key in active_client_keys
            }
            if self._pending_capture_completion_name is not None:
                disconnected_keys = self._pending_capture_client_keys - active_client_keys
                for key in list(disconnected_keys):
                    self._mark_pending_capture_ready(key, "phone disconnected")

            self.phone_selector_combo.configure(
                values=self.phone_selector_labels,
                state="readonly" if self.phone_selector_labels else "disabled",
            )

            if self.client_entries:
                if self.selected_client not in self.client_entries:
                    self.selected_client = self.client_entries[0]
                self.sync_selected_client_controls()
                self.refresh_captures_list()
                self.schedule_all_capture_list_refresh(delay_ms=0)
                self.request_all_camera_settings(log_request=False)
            else:
                self.selected_client = None
                self.sync_selected_client_controls()
                self.captures_list.delete(0, tk.END)
                self.refresh_camera_state_ui()

            self.update_transfer_sync_status()
            self.update_start_stop_buttons()
            if self.phone_stream_pane is not None:
                self.phone_stream_pane.update_clients(list(self.client_entries))

        try:
            self.root.after(0, update)
        except tk.TclError:
            pass

    def connect_phone_network(self, log_success=True) -> bool:
        if self.tcp is not None:
            self.phone_connection_enabled = True
            self.update_phone_connection_ui()
            return True

        tcp = None
        discovery = None
        try:
            tcp = TcpServer(
                host=SERVER_HOST,
                port=SERVER_PORT,
                log_callback=self.log,
                clients_changed_callback=self.on_clients_changed,
                message_callback=self.on_client_message,
                transfer_progress_callback=self.on_transfer_progress,
                save_dir_getter=self.get_save_dir,
            )
            discovery = UdpDiscoveryResponder(
                listen_port=DISCOVERY_UDP_PORT,
                reply_tcp_port=SERVER_PORT,
                log_callback=self.log,
            )
        except Exception as exc:
            try:
                if tcp is not None:
                    tcp.close()
            except Exception:
                pass
            try:
                if discovery is not None:
                    discovery.close()
            except Exception:
                pass
            self.tcp = None
            self.discovery = None
            self.phone_connection_enabled = False
            self.update_phone_connection_ui(error_text=str(exc))
            self.log(f"Phone connection could not start: {exc}")
            return False

        self.tcp = tcp
        self.discovery = discovery
        self.phone_connection_enabled = True
        self.update_phone_connection_ui()
        if log_success:
            self.log("Phone connection enabled")
        self.broadcast_generated_name_on_change()
        return True

    def disconnect_phone_network(self) -> bool:
        if self.is_running:
            self.log("Stop the active capture before disabling phone connection")
            return False

        if self._lag_test_session is not None:
            self._finish_lag_test_error("Phone connection was disabled")

        discovery = self.discovery
        tcp = self.tcp
        self.discovery = None
        self.tcp = None
        self.phone_connection_enabled = False

        if discovery is not None:
            discovery.close()
        if tcp is not None:
            tcp.close()

        self.on_clients_changed([])
        self.update_phone_connection_ui()
        self.log("Phone connection disabled")
        return True

    def toggle_phone_connection(self):
        if self.tcp is None:
            self.connect_phone_network()
        else:
            self.disconnect_phone_network()

    def update_phone_connection_ui(self, error_text: str = ""):
        if hasattr(self, "phone_disconnect_btn"):
            self.phone_disconnect_btn.configure(text="Disconnect" if self.tcp is not None else "Connect")
        if hasattr(self, "info_label"):
            if self.tcp is not None:
                self.info_label.configure(text=f"Server: {SERVER_HOST}:{SERVER_PORT}")
            elif error_text:
                self.info_label.configure(text=f"Phone connection off: {error_text}")
            else:
                self.info_label.configure(text="Phone connection off")
        self.update_start_stop_buttons()

    def on_client_selected(self, _event=None):
        if self._syncing_client_selection:
            return

        sel = self.clients_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self.client_entries):
            self.set_selected_client(self.client_entries[idx])

    # ------- TCP incoming messages -------

    def on_client_message(self, client, line: str):
        if not self._is_ui_thread():
            self._call_on_ui_thread(self.on_client_message, client, line)
            return

        parts = line.split(" ", 1)
        cmd = parts[0].upper()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "LIST_OK":
            try:
                payload = json.loads(rest)
                captures = payload.get("captures", [])
                self.invalidate_local_file_match_cache()
                self.captures_by_client[client["key"]] = captures
                if client == self.selected_client:
                    self.refresh_captures_list()
                self.update_transfer_sync_status()
                self.log(f"Received capture list: {len(captures)} items")
            except Exception as e:
                self.log(f"LIST_OK parse failed: {e}")
        elif cmd in ("SETTINGS_LIST_OK", "SETTINGS_OK"):
            try:
                payload = json.loads(rest)
                self.camera_settings_by_client[client["key"]] = payload
                self.camera_error_by_client.pop(client["key"], None)
                if cmd == "SETTINGS_LIST_OK":
                    self.auto_apply_camera_profile_to_client(client)
                self.refresh_camera_state_ui()
                summary = self.format_camera_modes_summary(payload)
                current_profile = self.build_camera_profile_from_current(payload)
                current_text = (
                    f"{self.format_resolution_option(current_profile)} @ "
                    f"{self.format_fps_option(current_profile)}"
                    if current_profile
                    else "unknown"
                )
                self.log(
                    f"Received camera settings from {client['addr'][0]}: "
                    f"current {current_text}; modes {summary}"
                )
            except Exception as e:
                self.log(f"{cmd} parse failed: {e}")
        elif cmd == "SETTINGS_ERR":
            self.log(line)
            self.camera_error_by_client[client["key"]] = rest or "unknown"
            self.refresh_camera_state_ui()
        elif cmd == "DELETE_OK":
            self.cancel_delete_watchdog()
            self.transfer_error_by_client.pop(client["key"], None)
            self.log(line)
            self.apply_delete_success(client, rest)
            self.schedule_capture_list_refresh(client, delay_ms=500)
        elif cmd == "DELETE_ERR":
            self.cancel_delete_watchdog()
            self.transfer_error_by_client[client["key"]] = rest or "delete failed"
            self.log(line)
            self.update_transfer_sync_status()
        elif cmd == "FILE_DONE":
            self.invalidate_local_file_match_cache()
            if client == self.selected_client:
                self.refresh_captures_list()
            self.update_transfer_sync_status()
        elif cmd in ("TRANSFER_ACCEPTED", "TRANSFER_BEGIN", "TRANSFER_DONE", "TRANSFER_ALL_DONE", "TRANSFER_ERR"):
            if cmd in ("TRANSFER_ACCEPTED", "TRANSFER_BEGIN", "TRANSFER_DONE", "TRANSFER_ALL_DONE"):
                self.transfer_error_by_client.pop(client["key"], None)
            elif cmd == "TRANSFER_ERR":
                self.transfer_error_by_client[client["key"]] = rest or "transfer failed"
            self.log(line)
            if client == self.selected_client and cmd == "TRANSFER_ACCEPTED":
                self.show_transfer_status("Transfer accepted, waiting for data...")
            if cmd in ("TRANSFER_DONE", "TRANSFER_ALL_DONE") and client == self.selected_client:
                self.refresh_captures_list()
            if cmd in ("TRANSFER_DONE", "TRANSFER_ALL_DONE"):
                self.schedule_capture_list_refresh(client, delay_ms=500)
            self.update_transfer_sync_status()
        elif cmd in (
            "START_OK",
            "STOP_MARKED",
            "STOP_OK",
            "READY",
            "READY_ERR",
            "PREPARE_OK",
            "PREPARE_ERR",
            "BUSY",
            "ERR_UNKNOWN",
        ):
            self.notify_capture_lifecycle_message(client, cmd, rest)
            if client == self.selected_client and cmd == "BUSY":
                reason = rest or "UNKNOWN"
                self.show_transfer_status(f"Phone busy: {reason}")
        elif cmd == "LIVE_PREVIEW_STATE":
            state_message = rest or "unknown"
            try:
                payload = json.loads(rest)
                active = bool(payload.get("active"))
                message = payload.get("message") or ("streaming" if active else "stopped")
                host = payload.get("host")
                port = payload.get("port")
                if active and host and port:
                    state_message = f"{message} -> {host}:{port}"
                else:
                    state_message = message
                if payload.get("error"):
                    state_message = f"{state_message} ({payload['error']})"
            except Exception:
                pass
            self.phone_stream_states_by_client[client["key"]] = state_message
            self.log(f"Phone stream {client.get('name') or client['addr'][0]}: {state_message}")
            if self.phone_stream_pane is not None:
                self.phone_stream_pane.set_stream_state(client["key"], state_message)
        else:
            self.log(f"Client msg: {line}")

        self.notify_lag_test_client_message(client, cmd, rest, line)

    def on_transfer_progress(self, client):
        if not self._is_ui_thread():
            with self._progress_lock:
                self._latest_progress_client = client
                if self._progress_dispatch_pending or self._progress_ui_after_id is not None:
                    return
                self._progress_dispatch_pending = True
            try:
                self.root.after(0, self._schedule_progress_flush)
            except tk.TclError:
                pass
            return

        with self._progress_lock:
            self._latest_progress_client = client
        self._schedule_progress_flush()

    def _schedule_progress_flush(self):
        with self._progress_lock:
            self._progress_dispatch_pending = False
            client = self._latest_progress_client

        active = self.has_active_transfer()
        delay_ms = 0 if not active or not client or client.get("transfer_received", 0) == 0 else 75
        if self._progress_ui_after_id is None:
            self._progress_ui_after_id = self.root.after(delay_ms, self._flush_progress_ui)

    def _flush_progress_ui(self):
        self._progress_ui_after_id = None
        with self._progress_lock:
            client = self._latest_progress_client
        if not client:
            return

        self.transfer_in_progress = self.has_active_transfer()
        self.update_start_stop_buttons()
        self.update_transfer_sync_status()

        if client.get("transfer_total", 0) > 0:
            total = client["transfer_total"]
            received = client["transfer_received"]
            pct = int((received / max(total, 1)) * 100)
            progress_value = pct
            if received > 0 and progress_value == 0:
                progress_value = 1
            self.progress_bar["value"] = progress_value
            self.progress_label.configure(
                text=f"Transfer {pct}%  {human_bytes(received)} / {human_bytes(total)}"
            )
        else:
            if client.get("transfer_active"):
                self.progress_label.configure(text="Transfer in progress...")
            else:
                self.progress_label.configure(text="No transfer")
            self.progress_bar["value"] = 0

    def show_transfer_status(self, text: str, progress_value=0):
        def update():
            self.progress_label.configure(text=text)
            self.progress_bar["value"] = progress_value

        try:
            self.root.after(0, update)
        except tk.TclError:
            pass

    # ------- transfer UI -------

    def get_save_dir(self) -> str:
        if not self._is_ui_thread():
            with self._save_dir_lock:
                return self._save_dir_path

        raw_path = self.save_dir_var.get().strip()
        if not raw_path:
            raw_path = self.app_config["default_save_path"]
        resolved = resolve_configured_path(raw_path)
        with self._save_dir_lock:
            self._save_dir_path = resolved
        return resolved

    def _on_save_dir_changed(self, *_):
        self.schedule_save_dir_update()

    def schedule_save_dir_update(self):
        if self._save_dir_update_after_id is not None:
            self.root.after_cancel(self._save_dir_update_after_id)
        self._save_dir_update_after_id = self.root.after(
            SAVE_DIR_EDIT_DEBOUNCE_MS, self.flush_save_dir_update
        )

    def flush_save_dir_update(self):
        if self._save_dir_update_after_id is not None:
            try:
                self.root.after_cancel(self._save_dir_update_after_id)
            except tk.TclError:
                pass
        self._save_dir_update_after_id = None
        self.get_save_dir()
        self.invalidate_local_file_match_cache()
        self.refresh_captures_list()
        self.update_transfer_sync_status()

    def get_transfer_client_label(self, client) -> str:
        payload = self.camera_settings_by_client.get(client["key"], {})
        position = payload.get("position")
        client_name = client.get("name") or client["addr"][0]
        return f"{position} - {client_name}" if position else client_name

    @staticmethod
    def capture_name_matches(requested_name: Optional[str], actual_name: Optional[str]) -> bool:
        requested = str(requested_name or "").strip()
        actual = str(actual_name or "").strip()
        if not requested or not actual:
            return False
        return actual == requested or actual.startswith(f"{requested}_")

    def get_capture_by_name(self, client, capture_name: Optional[str]) -> Optional[dict]:
        if not client or not capture_name:
            return None
        captures = self.captures_by_client.get(client["key"], [])
        for capture in captures:
            if self.capture_name_matches(capture_name, capture.get("name")):
                return capture
        return None

    def get_current_transfer_capture_name(self, log_errors=True) -> Optional[str]:
        if self.last_completed_capture_name:
            return self.last_completed_capture_name
        if log_errors:
            self.log("No completed capture available yet")
        return None

    def is_client_transferring_capture(self, client, capture_name: Optional[str]) -> bool:
        if not client or not capture_name or not client.get("transfer_active"):
            return False
        rel_path = client.get("current_file_rel") or client.get("pending_file_done_rel") or ""
        normalized = rel_path.replace("\\", "/")
        return normalized.startswith(f"{capture_name}/") or normalized.startswith(f"{capture_name}_")

    def update_transfer_sync_status(self):
        capture_name = self.last_completed_capture_name
        pending_name = self._pending_capture_completion_name
        if pending_name:
            self.transfer_sync_status_var.set(f"Finishing '{pending_name}'")
            if not self.client_entries:
                detail = "No phone connected"
            else:
                waiting_stop_ok = self._pending_capture_client_keys - self._pending_capture_stop_ok_keys
                waiting_preview = self._pending_capture_stop_ok_keys - self._pending_capture_preview_keys
                waiting_rearm = self._pending_capture_preview_keys - self._pending_capture_ready_keys
                if waiting_stop_ok:
                    detail = f"Waiting for {len(waiting_stop_ok)} phone(s) to finish MP4 muxing..."
                elif waiting_preview:
                    detail = f"Waiting for {len(waiting_preview)} phone(s) to restore preview..."
                elif waiting_rearm:
                    detail = f"Waiting for {len(waiting_rearm)} phone(s) to arm the recorder..."
                else:
                    detail = "Refreshing capture lists..."
            self.transfer_sync_details_var.set(detail)
            return

        if not self.client_entries:
            if capture_name:
                self.transfer_sync_status_var.set(
                    f"No phone connected. Last completed capture: {capture_name}"
                )
            else:
                self.transfer_sync_status_var.set("No completed capture yet")
            self.transfer_sync_details_var.set("No phone connected")
            return

        if not capture_name:
            self.transfer_sync_status_var.set("No completed capture yet")
            self.transfer_sync_details_var.set(
                "Capture status will appear here after the first completed recording."
            )
            return

        stored_count = 0
        phone_count = len(self.client_entries)
        transferring_count = 0
        error_count = 0
        details = []

        for client in self.client_entries:
            client_label = self.get_transfer_client_label(client)
            error_text = self.transfer_error_by_client.get(client["key"])
            capture = self.get_capture_by_name(client, capture_name)

            if error_text:
                error_count += 1
                details.append(f"{client_label}: error ({error_text})")
                continue

            if self.is_client_transferring_capture(client, capture_name):
                transferring_count += 1
                details.append(f"{client_label}: transferring")
                continue

            if client["key"] not in self.captures_by_client:
                details.append(f"{client_label}: loading capture list...")
                continue

            if not capture:
                details.append(f"{client_label}: capture not found on phone")
                continue

            matched_files, total_files = self.get_local_capture_match_counts(capture)
            if total_files > 0 and matched_files == total_files:
                stored_count += 1
                details.append(f"{client_label}: stored locally")
            elif matched_files > 0:
                details.append(f"{client_label}: partially local ({matched_files}/{total_files})")
            else:
                details.append(f"{client_label}: ready on phone")

        if transferring_count:
            summary = f"Transferring '{capture_name}' ({stored_count}/{phone_count} phones stored locally)"
        elif error_count:
            summary = f"Transfer issue on {error_count}/{phone_count} phone(s) for '{capture_name}'"
        else:
            summary = f"Current capture '{capture_name}': {stored_count}/{phone_count} phones stored locally"

        self.transfer_sync_status_var.set(summary)
        self.transfer_sync_details_var.set("\n".join(details))

    def transfer_capture_from_client(self, client, capture_name: str) -> bool:
        if not client or not capture_name:
            return False
        capture = self.get_capture_by_name(client, capture_name)
        request_name = capture.get("name") if capture else capture_name
        self.tcp.send_to_client(client, f"GET {request_name}")
        self.transfer_error_by_client.pop(client["key"], None)
        self.log(f"Sent to {client['addr'][0]}: GET {request_name}")
        return True

    def send_delete_to_client(self, client, command: str, deleted_name: str):
        self.tcp.send_to_client(client, command)
        self.transfer_error_by_client.pop(client["key"], None)
        self.log(f"Sent to {client['addr'][0]}: {command}")
        self.schedule_capture_list_refresh(client, delay_ms=500)

    def on_transfer_current_all(self):
        capture_name = self.get_current_transfer_capture_name()
        if not capture_name:
            return

        sent_count = 0
        for client in self.client_entries:
            if self.transfer_capture_from_client(client, capture_name):
                sent_count += 1

        if not sent_count:
            self.log(f"Current capture '{capture_name}' was not found on any connected phone")
        self.update_transfer_sync_status()

    def on_transfer_all_all(self):
        if not self.client_entries:
            self.log("No phone connected")
            return

        for client in self.client_entries:
            self.tcp.send_to_client(client, "GET_ALL")
            self.transfer_error_by_client.pop(client["key"], None)
            self.log(f"Sent to {client['addr'][0]}: GET_ALL")
        self.update_transfer_sync_status()

    def on_delete_current_all(self):
        capture_name = self.get_current_transfer_capture_name()
        self.relock_delete_actions("global_delete_current")
        if not capture_name:
            return

        if not messagebox.askyesno(
            "Delete Current Capture On All Phones",
            f"Delete '{capture_name}' from all connected phones?",
        ):
            return

        sent_count = 0
        for client in self.client_entries:
            capture = self.get_capture_by_name(client, capture_name)
            if capture:
                actual_name = capture.get("name") or capture_name
                self.send_delete_to_client(client, f"DELETE {actual_name}", actual_name)
                sent_count += 1

        if not sent_count:
            self.log(f"Current capture '{capture_name}' was not found on any connected phone")
        self.update_transfer_sync_status()

    def on_delete_all_all(self):
        self.relock_delete_actions("global_delete_all")
        if not self.client_entries:
            self.log("No phone connected")
            return

        if not messagebox.askyesno(
            "Delete All Captures On All Phones",
            "Delete all captures from all connected phones?",
        ):
            return

        for client in self.client_entries:
            self.send_delete_to_client(client, "DELETE_ALL", "ALL")
        self.update_transfer_sync_status()

    def on_browse(self):
        initial_dir = self.get_save_dir()
        if not os.path.isdir(initial_dir):
            initial_dir = APP_ROOT
        path = filedialog.askdirectory(initialdir=initial_dir)
        if path:
            self.save_dir_var.set(path)
            self.flush_save_dir_update()

    def refresh_captures_list(self):
        selected = self.captures_list.curselection()
        selected_idx = selected[0] if selected else None
        self.captures_list.delete(0, tk.END)
        if not self.selected_client:
            return
        captures = self.captures_by_client.get(self.selected_client["key"], [])
        for idx, capture in enumerate(captures):
            self.captures_list.insert(tk.END, self.format_capture_list_entry(capture))
            if selected_idx == idx:
                self.captures_list.selection_set(idx)
                self.captures_list.activate(idx)

    def format_capture_list_entry(self, capture: dict) -> str:
        name = capture.get("name", "?")
        total = capture.get("totalBytes", 0)
        matched_files, total_files = self.get_local_capture_match_counts(capture)
        if total_files > 0:
            prefix = f"[{matched_files}/{total_files}]"
        else:
            prefix = "[?]"
        return f"{prefix} {name}  ({human_bytes(total)})"

    def get_local_capture_match_counts(self, capture: dict) -> Tuple[int, int]:
        files = capture.get("files", [])
        total_files = len(files)
        if total_files == 0:
            return 0, 0

        save_dir = self.get_save_dir()
        if not os.path.isdir(save_dir):
            return 0, total_files

        capture_name = capture.get("name", "")
        matched_files = 0
        for file_info in files:
            if self.capture_file_exists_locally(save_dir, capture_name, file_info):
                matched_files += 1

        return matched_files, total_files

    def capture_file_exists_locally(self, save_dir: str, capture_name: str, file_info: dict) -> bool:
        file_name = file_info.get("name", "")
        if not file_name:
            return False

        expected_size = file_info.get("sizeBytes")
        cache_key = (
            os.path.normcase(os.path.normpath(os.path.abspath(save_dir))),
            str(capture_name or ""),
            str(file_name),
            None if expected_size is None else str(expected_size),
        )
        if cache_key in self._local_file_match_cache:
            return self._local_file_match_cache[cache_key]

        candidate_rel_paths = []
        if capture_name:
            candidate_rel_paths.append(f"{capture_name}/{file_name}")
        candidate_rel_paths.append(file_name)

        seen = set()
        for rel_path in candidate_rel_paths:
            try:
                path = resolve_safe_transfer_path(save_dir, rel_path)
            except ValueError:
                continue

            normalized = os.path.normcase(os.path.normpath(path))
            if normalized in seen:
                continue
            seen.add(normalized)

            try:
                if not os.path.isfile(path):
                    continue
                if expected_size is None:
                    self._local_file_match_cache[cache_key] = True
                    return True
                if os.path.getsize(path) == int(expected_size):
                    self._local_file_match_cache[cache_key] = True
                    return True
            except (OSError, ValueError, TypeError):
                continue

        self._local_file_match_cache[cache_key] = False
        return False

    def apply_delete_success(self, client, deleted_name: str):
        key = client["key"]
        captures = list(self.captures_by_client.get(key, []))

        if deleted_name.upper() == "ALL":
            captures = []
        elif deleted_name:
            captures = [c for c in captures if c.get("name") != deleted_name]

        self.captures_by_client[key] = captures
        if client == self.selected_client:
            self.refresh_captures_list()
        self.update_transfer_sync_status()

    def get_selected_capture_name(self) -> Optional[str]:
        if not self.selected_client:
            self.log("No client selected")
            return None
        sel = self.captures_list.curselection()
        if not sel:
            self.log("No capture selected")
            return None
        captures = self.captures_by_client.get(self.selected_client["key"], [])
        idx = sel[0]
        if idx >= len(captures):
            return None
        return captures[idx].get("name", "") or None

    def request_capture_list(self, client=None, log_request=False) -> bool:
        target_client = client or self.selected_client
        if not target_client:
            if log_request:
                self.log("No client selected")
            return False
        self.tcp.send_to_client(target_client, "LIST")
        if log_request:
            self.log("Sent: LIST")
        return True

    def schedule_capture_list_refresh(self, client, delay_ms=300):
        if not client:
            return

        def refresh():
            if client in self.client_entries:
                self.request_capture_list(client=client, log_request=False)

        self.root.after(delay_ms, refresh)

    def schedule_all_capture_list_refresh(self, delay_ms=300):
        for client in list(self.client_entries):
            self.schedule_capture_list_refresh(client, delay_ms=delay_ms)

    def _clear_pending_capture_finalize_timers(self):
        for after_id in self._pending_capture_finalize_after_ids:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._pending_capture_finalize_after_ids = []

    def _schedule_pending_capture_timeout(self, delay_ms: int, callback: Callable):
        try:
            after_id = self.root.after(delay_ms, callback)
        except tk.TclError:
            return
        self._pending_capture_finalize_after_ids.append(after_id)

    def _capture_client_label_by_key(self, client_key: str) -> str:
        for client in self.client_entries:
            if client.get("key") == client_key:
                return self.get_transfer_client_label(client)
        return client_key

    def _begin_pending_capture_completion(self, capture_name: Optional[str]):
        self._clear_pending_capture_finalize_timers()
        self._pending_capture_completion_name = capture_name
        self._pending_capture_client_keys = {client["key"] for client in self.client_entries}
        self._pending_capture_stop_ok_keys = set()
        self._pending_capture_preview_keys = set()
        self._pending_capture_ready_keys = set()

        if not capture_name:
            self._finalize_pending_capture_completion()
            return
        if not self._pending_capture_client_keys:
            self.log(f"Capture '{capture_name}' stopped; no connected phones to wait for")
            self._finalize_pending_capture_completion()
            return

        self.log(
            f"Capture '{capture_name}' stopped; waiting for phone STOP_OK and READY"
        )
        for client_key in list(self._pending_capture_client_keys):
            self._schedule_pending_capture_timeout(
                CAPTURE_STOP_OK_TIMEOUT_MS,
                lambda key=client_key: self._on_pending_capture_stop_ok_timeout(key),
            )
        self.update_transfer_sync_status()
        self.update_start_stop_buttons()

    def _on_pending_capture_stop_ok_timeout(self, client_key: str):
        if self._pending_capture_completion_name is None:
            return
        if client_key not in self._pending_capture_client_keys:
            return
        if client_key in self._pending_capture_stop_ok_keys:
            return
        label = self._capture_client_label_by_key(client_key)
        self.log(f"Capture STOP_OK not received from {label}; refreshing anyway")
        self._mark_pending_capture_ready(client_key, "STOP_OK timeout")

    def _on_pending_capture_ready_timeout(self, client_key: str):
        if self._pending_capture_completion_name is None:
            return
        if client_key not in self._pending_capture_client_keys:
            return
        if client_key in self._pending_capture_ready_keys:
            return
        label = self._capture_client_label_by_key(client_key)
        if client_key in self._pending_capture_preview_keys:
            self.log(f"Capture PREPARE_OK READY not received from {label}; refreshing anyway")
        else:
            self.log(f"Capture READY not received from {label}; refreshing anyway")
        self._mark_pending_capture_ready(client_key, "READY timeout")

    def _mark_pending_capture_ready(self, client_key: str, reason: str = ""):
        if self._pending_capture_completion_name is None:
            return
        if client_key not in self._pending_capture_client_keys:
            return
        self._pending_capture_ready_keys.add(client_key)
        if reason:
            self.transfer_error_by_client.pop(client_key, None)
        self.update_transfer_sync_status()
        self._maybe_finalize_pending_capture()

    def _maybe_finalize_pending_capture(self):
        if self._pending_capture_completion_name is None:
            return
        if self._pending_capture_client_keys <= self._pending_capture_ready_keys:
            self._finalize_pending_capture_completion()

    def _finalize_pending_capture_completion(self):
        capture_name = self._pending_capture_completion_name
        self._clear_pending_capture_finalize_timers()
        self._pending_capture_completion_name = None
        self._pending_capture_client_keys = set()
        self._pending_capture_stop_ok_keys = set()
        self._pending_capture_preview_keys = set()
        self._pending_capture_ready_keys = set()

        if capture_name:
            self.last_completed_capture_name = capture_name
            for client in self.client_entries:
                self.captures_by_client.pop(client["key"], None)
            self.invalidate_local_file_match_cache()
            self.schedule_all_capture_list_refresh(delay_ms=250)
            self.log(f"Capture '{capture_name}' ready for transfer lookup")
        self.update_transfer_sync_status()
        self.increment_auto_increment_fields()
        self.update_start_stop_buttons()

    def notify_capture_lifecycle_message(self, client, cmd: str, rest: str):
        if self._lag_test_session is not None:
            return
        capture_name = self._pending_capture_completion_name
        if capture_name is None:
            return
        client_key = client.get("key")
        if client_key not in self._pending_capture_client_keys:
            return

        label = self.get_transfer_client_label(client)
        if cmd == "STOP_OK":
            if client_key not in self._pending_capture_stop_ok_keys:
                self._pending_capture_stop_ok_keys.add(client_key)
                self.log(f"Capture STOP_OK from {label}; waiting for READY")
            self.update_transfer_sync_status()
            self._schedule_pending_capture_timeout(
                CAPTURE_READY_TIMEOUT_MS,
                lambda key=client_key: self._on_pending_capture_ready_timeout(key),
            )
        elif cmd == "READY":
            self.log(f"Capture READY from {label}: {rest or 'PREVIEW'}")
            self._pending_capture_stop_ok_keys.add(client_key)
            self._pending_capture_preview_keys.add(client_key)
            self.update_transfer_sync_status()
            self._schedule_pending_capture_timeout(
                CAPTURE_READY_TIMEOUT_MS,
                lambda key=client_key: self._on_pending_capture_ready_timeout(key),
            )
        elif cmd == "READY_ERR":
            self.log(f"Capture READY_ERR from {label}: {rest or 'unknown'}")
            self._pending_capture_stop_ok_keys.add(client_key)
            self._pending_capture_preview_keys.add(client_key)
            self._mark_pending_capture_ready(client_key)
        elif cmd == "PREPARE_OK":
            labels, _fields = _parse_protocol_fields(rest)
            if "READY" not in [item.upper() for item in labels]:
                return
            self.log(
                f"Capture PREPARE_OK from {label}: "
                f"{_format_phone_lifecycle_for_log(cmd, rest or 'READY')}"
            )
            self._pending_capture_stop_ok_keys.add(client_key)
            self._pending_capture_preview_keys.add(client_key)
            self._mark_pending_capture_ready(client_key)
        elif cmd == "PREPARE_ERR":
            self.log(f"Capture PREPARE_ERR from {label}: {rest or 'unknown'}")
            self._pending_capture_stop_ok_keys.add(client_key)
            self._pending_capture_preview_keys.add(client_key)
            self._mark_pending_capture_ready(client_key)
        elif cmd == "BUSY":
            self.log(f"Capture busy response from {label}: {rest or 'UNKNOWN'}")
        elif cmd == "ERR_UNKNOWN":
            self.log(f"Capture protocol issue from {label}: {rest or 'ERR_UNKNOWN'}")

    def cancel_delete_watchdog(self):
        if self._delete_watchdog_after_id is not None:
            self.root.after_cancel(self._delete_watchdog_after_id)
            self._delete_watchdog_after_id = None
        self._pending_delete_client = None
        self._pending_delete_command = None

    def start_delete_watchdog(self, client, command_text: str, timeout_ms=2500):
        self.cancel_delete_watchdog()
        self._pending_delete_client = client
        self._pending_delete_command = command_text
        self._delete_watchdog_after_id = self.root.after(timeout_ms, self._on_delete_watchdog_timeout)

    def _on_delete_watchdog_timeout(self):
        client = self._pending_delete_client
        command_text = self._pending_delete_command
        self._delete_watchdog_after_id = None
        self._pending_delete_client = None
        self._pending_delete_command = None

        if command_text:
            self.log(f"No delete response received for: {command_text}")
        if client and client in self.client_entries:
            self.log("Refreshing capture list to verify delete state")
            self.request_capture_list(client=client, log_request=False)

    def on_refresh_list(self):
        self.request_capture_list(log_request=True)

    def on_transfer_selected(self):
        name = self.get_selected_capture_name()
        if not name:
            return
        if self.transfer_capture_from_client(self.selected_client, name):
            self.update_transfer_sync_status()

    def on_transfer_all(self):
        if not self.selected_client:
            self.log("No client selected")
            return
        self.tcp.send_to_client(self.selected_client, "GET_ALL")
        self.transfer_error_by_client.pop(self.selected_client["key"], None)
        self.log("Sent: GET_ALL")
        self.update_transfer_sync_status()

    def on_delete_selected(self):
        self.relock_delete_actions("selected_delete")
        client = self.selected_client
        if not client:
            self.log("No client selected")
            return
        name = self.get_selected_capture_name()
        if not name:
            return
        if not messagebox.askyesno("Delete Capture", f"Delete '{name}' from the selected phone?"):
            return
        self.transfer_error_by_client.pop(client["key"], None)
        self.tcp.send_to_client(client, f"DELETE {name}")
        self.log(f"Sent: DELETE {name}")
        self.start_delete_watchdog(client, f"DELETE {name}")
        self.update_transfer_sync_status()

    def on_delete_all(self):
        self.relock_delete_actions("selected_delete_all")
        client = self.selected_client
        if not client:
            self.log("No client selected")
            return
        if not messagebox.askyesno("Delete All Captures", "Delete all captures from the selected phone?"):
            return
        self.transfer_error_by_client.pop(client["key"], None)
        self.tcp.send_to_client(client, "DELETE_ALL")
        self.log("Sent: DELETE_ALL")
        self.start_delete_watchdog(client, "DELETE_ALL")
        self.update_transfer_sync_status()

    # ------- lag test -------

    def on_lag_test(self):
        if self._lag_test_session is not None:
            self.log("Lag test already running")
            return
        if self.tcp is None:
            self.log("Phone connection is off")
            return
        client = self.selected_client
        if not client:
            self.log("Select one connected phone before running Lag Test")
            return
        if self.is_running:
            self.log("Stop the active capture before running Lag Test")
            return
        if self.has_active_transfer():
            self.log("Wait for the active transfer before running Lag Test")
            return

        label = "lagtest_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        display = LagTimingDisplay(self.root, left_fraction=0.5)
        session = HubLagTestSession(
            label=label,
            client_key=client["key"],
            client_addr=f"{client['addr'][0]}:{client['addr'][1]}",
            client_name=client.get("name") or client["addr"][0],
            display=display,
            duration_s=LAG_TEST_DURATION_S,
            display_started_perf=0.0,
        )
        self._lag_test_session = session
        self._set_phone_preview_suspended_for_lag_test(True)
        self.update_start_stop_buttons()
        self._set_lag_test_status(f"Preparing selected camera mode for {label}")
        self._lag_test_prepare_camera_mode()

    def get_phone_start_lead_ms(self) -> float:
        if hasattr(self, "phone_start_lead_ms_var"):
            return self._normalize_phone_start_lead_ms(self.phone_start_lead_ms_var.get())
        return self._normalize_phone_start_lead_ms(
            self.app_state.get("phone_start_lead_ms", 0.0),
            persist=False,
        )

    def _normalize_phone_start_lead_ms(self, raw, persist: bool = True) -> float:
        try:
            value = float(str(raw).strip())
        except Exception:
            value = 0.0
        value = max(0.0, min(value, 5000.0))
        if hasattr(self, "phone_start_lead_ms_var"):
            self.phone_start_lead_ms_var.set(compact_float_text(value, precision=1))
        if persist:
            self.app_state["phone_start_lead_ms"] = value
            try:
                save_app_state(self.app_state)
            except Exception as exc:
                self.log(f"Could not save camera lead setting: {exc}")
        return value

    def _lag_test_current_camera_profile(self, payload: dict) -> Optional[dict]:
        profile = self.build_camera_profile_from_current(payload)
        if profile is None:
            return None
        fps = float(profile.get("fps") or 0.0)
        if fps <= 0.0:
            fps = float(self.camera_defaults.get("fps") or 60.0)
        return {
            "width": int(profile["width"]),
            "height": int(profile["height"]),
            "fps": fps,
            "iso": float(profile.get("iso") or self.camera_defaults["iso"]),
            "shutterSeconds": float(
                profile.get("shutterSeconds")
                or default_shutter_seconds_for_fps(
                    fps,
                    self.camera_defaults["shutter_fps_multiplier"],
                )
            ),
        }

    def _lag_test_prepare_camera_mode(self):
        session = self._lag_test_session
        client = self._lag_test_client()
        if session is None or client is None or self.tcp is None:
            return
        payload = self.camera_settings_by_client.get(client["key"])
        if not payload:
            session.camera_setup_requested = True
            self.tcp.send_to_client(client, "SETTINGS_LIST")
            self.log(f"Lag test requested camera modes from {session.client_name}")
            self.root.after(
                LAG_TEST_PREPARE_TIMEOUT_MS,
                lambda key=session.client_key, label=session.label: self._lag_test_prepare_timeout(key, label),
            )
            return

        profile = self._lag_test_current_camera_profile(payload)
        if profile is None:
            self._finish_lag_test_error("Phone did not report a current camera mode for lag test")
            return

        current_profile = self.build_camera_profile_from_current(payload)
        if self.camera_profiles_match(current_profile, profile):
            session.camera_setup_applied = True
            self._lag_test_send_label_prepare()
            return

        session.camera_setup_requested = True
        payload_text = json.dumps(profile, separators=(",", ":"))
        self.tcp.send_to_client(client, f"SETTINGS {payload_text}")
        self.log(
            "Lag test camera mode requested for "
            f"{session.client_name}: {profile['width']}x{profile['height']} @ {profile['fps']:.2f} fps"
        )
        self.root.after(
            LAG_TEST_PREPARE_TIMEOUT_MS,
            lambda key=session.client_key, label=session.label: self._lag_test_prepare_timeout(key, label),
        )

    def _lag_test_send_label_prepare(self):
        session = self._lag_test_session
        client = self._lag_test_client()
        if session is None or client is None or self.tcp is None:
            return
        if session.prepare_requested:
            return
        self._set_lag_test_status(f"Preparing phone recorder for {session.label}")
        self.tcp.send_to_client(client, f"NAME {session.label}")
        session.prepare_requested = True
        prepare_payload = json.dumps(
            {
                "prerollMs": LAG_TEST_PHONE_PREROLL_MS,
                "cameraLeadMs": self.get_phone_start_lead_ms(),
            },
            separators=(",", ":"),
        )
        self.tcp.send_to_client(client, f"PREPARE {prepare_payload}")
        self.log(f"Lag test label/prep sent to {session.client_name}: {session.label}")
        self.root.after(
            LAG_TEST_PREPARE_TIMEOUT_MS,
            lambda key=session.client_key, label=session.label: self._lag_test_prepare_timeout(key, label),
        )

    def _lag_test_delay_until_ms(self, session: HubLagTestSession, target_ms: float) -> int:
        return max(0, int(round(float(target_ms) - session.display.elapsed_ms())))

    def _lag_test_start_countdown(self, client_key: str):
        session = self._lag_test_session
        if session is None or session.client_key != client_key:
            return
        if session.display.window is not None:
            return
        try:
            session.display.show()
        except Exception as exc:
            self._finish_lag_test_error(f"Lag test display could not open: {exc}")
            return
        session.display_started_perf = session.display.start_perf or time.perf_counter()
        session.display_refresh_hz = getattr(session.display, "refresh_hz", None)
        session.display_tick_interval_ms = getattr(session.display, "tick_interval_ms", None)
        session.display.set_phase("ARMED")
        self._set_lag_test_status("Phone prepared; START scheduled at 1000 ms")
        if session.display_refresh_hz and session.display_tick_interval_ms:
            self.log(
                "Lag test display refresh: "
                f"{session.display_refresh_hz:.2f} Hz "
                f"({session.display_tick_interval_ms:.2f} ms frame target)"
            )
        self.root.after(
            self._lag_test_delay_until_ms(session, LAG_TEST_START_TARGET_MS),
            lambda key=session.client_key, label=session.label: self._lag_test_send_start(key, label),
        )

    def _lag_test_prepare_timeout(self, client_key: str, label: str):
        session = self._lag_test_session
        if not self._lag_test_session_matches(client_key, label) or session is None or session.prepared:
            return
        if not session.prepare_requested:
            self._finish_lag_test_error("Phone did not confirm lag-test camera setup before countdown")
        else:
            self._finish_lag_test_error("Phone did not confirm PREPARE_OK before lag-test countdown")

    def _lag_test_send_start(self, client_key: str, label: str):
        session = self._lag_test_session
        client = self._lag_test_client()
        if not self._lag_test_session_matches(client_key, label) or session is None or client is None or self.tcp is None:
            return
        session.display.set_phase("START")
        session.actual_start_command_elapsed_ms = session.display.elapsed_ms()
        session.start_command_elapsed_ms = LAG_TEST_START_TARGET_MS
        self.tcp.send_to_client(client, "START")
        self._set_lag_test_status("START target 1000 ms; recording timing target")
        self.log(
            "Lag test START sent to "
            f"{session.client_name} at {session.actual_start_command_elapsed_ms:.1f} ms "
            f"(target {session.start_command_elapsed_ms:.0f} ms)"
        )
        self.root.after(
            self._lag_test_delay_until_ms(session, LAG_TEST_STOP_TARGET_MS),
            lambda key=session.client_key, label=session.label: self._lag_test_send_stop(key, label),
        )

    def _lag_test_send_stop(self, client_key: str, label: str):
        session = self._lag_test_session
        client = self._lag_test_client()
        if not self._lag_test_session_matches(client_key, label) or session is None or client is None or self.tcp is None:
            return
        session.display.set_phase("STOP")
        session.actual_stop_command_elapsed_ms = session.display.elapsed_ms()
        session.stop_command_elapsed_ms = LAG_TEST_STOP_TARGET_MS
        self.tcp.send_to_client(client, "STOP")
        self._set_lag_test_status("STOP target 2000 ms; holding target for late frames")
        self.log(
            "Lag test STOP sent to "
            f"{session.client_name} at {session.actual_stop_command_elapsed_ms:.1f} ms "
            f"(target {session.stop_command_elapsed_ms:.0f} ms)"
        )
        self.root.after(
            LAG_TEST_STOP_MARKED_TIMEOUT_MS,
            lambda key=session.client_key, label=session.label: self._lag_test_stop_marked_timeout(key, label),
        )

    def _lag_test_stop_marked_timeout(self, client_key: str, label: str):
        session = self._lag_test_session
        if (
            not self._lag_test_session_matches(client_key, label)
            or session is None
            or session.stop_marked_elapsed_ms is not None
            or session.stop_ok_elapsed_ms is not None
        ):
            return
        self._finish_lag_test_error("Phone did not confirm STOP_MARKED after STOP")

    def _lag_test_stop_ok_timeout(self, client_key: str, label: str):
        session = self._lag_test_session
        if not self._lag_test_session_matches(client_key, label) or session is None or session.stop_ok_elapsed_ms is not None:
            return
        self._finish_lag_test_error("Phone marked STOP but did not send STOP_OK after muxing")

    def _lag_test_begin_transfer_lookup(self, client_key: str, label: str):
        session = self._lag_test_session
        if not self._lag_test_session_matches(client_key, label) or session is None:
            return
        if session.transfer_lookup_started:
            return
        session.transfer_lookup_started = True
        session.display.set_phase("TRANSFER")
        session.display.close()
        self._set_lag_test_status("Looking for phone capture")
        self._lag_test_find_capture(client_key, label)

    def _lag_test_ready_timeout(self, client_key: str, label: str):
        session = self._lag_test_session
        if (
            not self._lag_test_session_matches(client_key, label)
            or session is None
            or session.ready_elapsed_ms is not None
            or session.transfer_lookup_started
        ):
            return
        self.log("Lag test READY was not received after STOP_OK; continuing with transfer lookup")
        self._lag_test_begin_transfer_lookup(client_key, label)

    def _lag_test_find_capture(self, client_key: Optional[str] = None, label: Optional[str] = None):
        session = self._lag_test_session
        if client_key is not None and label is not None and not self._lag_test_session_matches(client_key, label):
            return
        client = self._lag_test_client()
        if session is None or client is None:
            return

        captures = self.captures_by_client.get(client["key"], [])
        candidates = [
            capture for capture in captures
            if str(capture.get("name", "")).startswith(session.label)
        ]
        if candidates:
            candidates.sort(key=lambda item: str(item.get("name", "")), reverse=True)
            capture = candidates[0]
            session.capture_info = capture
            session.capture_name = capture.get("name")
            if not session.capture_name:
                self._finish_lag_test_error("Lag test capture had no name")
                return
            self.last_completed_capture_name = session.capture_name
            self._set_lag_test_status(f"Transferring {session.capture_name}")
            session.transfer_requested = True
            if self.transfer_capture_from_client(client, session.capture_name):
                self.update_transfer_sync_status()
            else:
                self._finish_lag_test_error("Could not request lag-test transfer")
            return

        session.poll_attempts += 1
        if session.poll_attempts > 18:
            self._finish_lag_test_error(f"No phone capture starting with '{session.label}' appeared")
            return
        self.request_capture_list(client=client, log_request=False)
        self.root.after(
            750,
            lambda key=session.client_key, label=session.label: self._lag_test_find_capture(key, label),
        )

    def notify_lag_test_client_message(self, client, cmd: str, rest: str, line: str):
        del line
        session = self._lag_test_session
        if session is None or client.get("key") != session.client_key:
            return

        now_elapsed = session.display.elapsed_ms()
        if cmd in ("SETTINGS_LIST_OK", "SETTINGS_OK") and session.camera_setup_requested and not session.prepare_requested:
            session.camera_setup_applied = cmd == "SETTINGS_OK"
            self._lag_test_prepare_camera_mode()
        elif cmd == "SETTINGS_ERR" and session.camera_setup_requested and not session.prepare_requested:
            self._finish_lag_test_error(f"Phone camera setup failed: {rest or 'unknown'}")
        elif cmd == "PREPARE_OK" and not session.prepared:
            session.prepared = True
            self.log(
                f"Lag test PREPARE_OK from {session.client_name}: "
                f"{_format_phone_lifecycle_for_log(cmd, rest or 'READY')}"
            )
            self._lag_test_start_countdown(session.client_key)
        elif cmd == "PREPARE_ERR":
            self._finish_lag_test_error(f"Phone prepare failed: {rest or 'unknown'}")
        elif cmd == "START_OK" and session.start_ack_elapsed_ms is None:
            session.start_ack_elapsed_ms = now_elapsed
            self.log(f"Lag test START_OK at {now_elapsed:.1f} ms")
        elif cmd == "STOP_MARKED" and session.stop_marked_elapsed_ms is None:
            session.stop_marked_elapsed_ms = now_elapsed
            session.stop_ack_elapsed_ms = now_elapsed
            self._set_lag_test_status("STOP marked; waiting for muxed MP4")
            self.log(f"Lag test STOP_MARKED at {now_elapsed:.1f} ms; waiting for muxed MP4")
            self.root.after(
                LAG_TEST_STOP_OK_TIMEOUT_MS,
                lambda key=session.client_key, label=session.label: self._lag_test_stop_ok_timeout(key, label),
            )
        elif cmd == "STOP_OK" and session.stop_ok_elapsed_ms is None:
            session.stop_ok_elapsed_ms = now_elapsed
            if session.stop_marked_elapsed_ms is None:
                session.stop_marked_elapsed_ms = now_elapsed
                session.stop_ack_elapsed_ms = now_elapsed
                self.log("Lag test STOP_OK used as fallback stop timing because STOP_MARKED was not received")
            self._set_lag_test_status("MP4 muxed; waiting for READY")
            self.log(f"Lag test STOP_OK at {now_elapsed:.1f} ms; MP4 muxed, waiting for READY")
            self.root.after(
                LAG_TEST_READY_TIMEOUT_MS,
                lambda key=session.client_key, label=session.label: self._lag_test_ready_timeout(key, label),
            )
        elif cmd == "READY":
            session.ready_elapsed_ms = now_elapsed
            self.log(f"Lag test READY at {now_elapsed:.1f} ms: {rest or 'PREVIEW'}")
            self.root.after(
                250,
                lambda key=session.client_key, label=session.label: self._lag_test_begin_transfer_lookup(key, label),
            )
        elif cmd == "READY_ERR":
            session.ready_elapsed_ms = now_elapsed
            self._set_lag_test_status(f"Ready issue after lag test: {rest or 'unknown'}")
            self.log(f"Lag test READY_ERR at {now_elapsed:.1f} ms: {rest or 'unknown'}")
            self.root.after(
                250,
                lambda key=session.client_key, label=session.label: self._lag_test_begin_transfer_lookup(key, label),
            )
        elif cmd == "STOP_ERR":
            self._finish_lag_test_error(f"Phone stop failed: {rest or 'unknown'}")
        elif cmd == "BUSY":
            self._finish_lag_test_error(f"Phone busy during lag test: {rest or 'UNKNOWN'}")
        elif cmd == "TRANSFER_ERR":
            self._finish_lag_test_error(f"Lag test transfer failed: {rest or 'unknown'}")
        elif cmd == "TRANSFER_DONE" and session.capture_name:
            finished_name = rest.split(" ", 1)[0] if rest else ""
            if finished_name == session.capture_name:
                self.root.after(300, self._lag_test_start_analysis)

    def _lag_test_start_analysis(self):
        session = self._lag_test_session
        if session is None or session.analysis_started:
            return
        session.analysis_started = True
        session.display.set_phase("ANALYZING")
        self._set_lag_test_status("Analyzing transferred video")

        video_path = self._lag_test_local_video_path(session)
        if not video_path:
            self._finish_lag_test_error("Transferred lag-test MP4 was not found locally")
            return
        session.segment_metrics = self._lag_test_local_segment_metrics(session)

        timing = LagTiming(
            label=session.label,
            display_started_perf=session.display_started_perf,
            start_command_elapsed_ms=float(session.start_command_elapsed_ms or 0.0),
            stop_command_elapsed_ms=float(session.stop_command_elapsed_ms or 0.0),
            start_ack_elapsed_ms=session.start_ack_elapsed_ms,
            stop_ack_elapsed_ms=session.stop_ack_elapsed_ms,
        )

        def worker():
            analysis = analyze_lag_video(video_path, timing)
            report_paths = {}
            try:
                report_base = os.path.splitext(video_path)[0] + "_lag_report"
                report_extra = {
                    "client": session.client_name,
                    "client_addr": session.client_addr,
                    "capture_name": session.capture_name,
                    "intended_start_ms": LAG_TEST_START_TARGET_MS,
                    "intended_stop_ms": LAG_TEST_STOP_TARGET_MS,
                    "actual_start_command_elapsed_ms": session.actual_start_command_elapsed_ms,
                    "actual_stop_command_elapsed_ms": session.actual_stop_command_elapsed_ms,
                    "display_refresh_hz": session.display_refresh_hz,
                    "display_tick_interval_ms": session.display_tick_interval_ms,
                    "stop_marked_elapsed_ms": session.stop_marked_elapsed_ms,
                    "stop_ok_elapsed_ms": session.stop_ok_elapsed_ms,
                    "ready_elapsed_ms": session.ready_elapsed_ms,
                    "segment": session.segment_metrics,
                }
                report_extra["command_timing"] = _build_lag_test_command_timing(report_extra, analysis)
                report_paths = write_lag_report(
                    report_base,
                    timing,
                    analysis,
                    extra=report_extra,
                )
            except Exception as exc:
                analysis.error = (
                    f"{analysis.error}; report write failed: {exc}"
                    if analysis.error
                    else f"Report write failed: {exc}"
                )
            try:
                self.root.after(0, lambda: self._finish_lag_test_analysis(analysis, report_paths))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_lag_test_analysis(self, analysis, report_paths: dict):
        session = self._lag_test_session
        if session is None:
            return
        if analysis.error:
            self.log(f"Lag test analysis issue: {analysis.error}")
            status = (
                f"Endpoint decode not clean | "
                f"first clean={analysis.first_frame_clean} | "
                f"last clean={analysis.last_frame_clean}"
            )
        else:
            status = (
                f"First {analysis.first_frame_elapsed_ms} ms -> {analysis.start_lag_ms:+.1f} ms | "
                f"Last {analysis.last_frame_elapsed_ms} ms -> {analysis.stop_lag_ms:+.1f} ms | "
                f"confidence {analysis.confidence:.2f}"
            )
        self._set_lag_test_status(status)
        command_timing = _build_lag_test_command_timing(
            {
                "intended_start_ms": LAG_TEST_START_TARGET_MS,
                "intended_stop_ms": LAG_TEST_STOP_TARGET_MS,
                "actual_start_command_elapsed_ms": session.actual_start_command_elapsed_ms,
                "actual_stop_command_elapsed_ms": session.actual_stop_command_elapsed_ms,
                "segment": session.segment_metrics,
            },
            analysis,
        )
        if command_timing.get("late"):
            timing_note = f"Command timing late: {command_timing.get('late_message')}"
            self._set_lag_test_status(f"{status} | {timing_note}")
            self.log(f"Lag test command timing warning: {command_timing.get('late_message')}")
        else:
            self.log("Lag test command timing: phone receive duration matched Hub START/STOP timing")
        self.log(
            "Lag test result: "
            f"first={analysis.first_frame_elapsed_ms} ms vs 1000 ms ({self._fmt_ms(analysis.start_lag_ms)}), "
            f"last={analysis.last_frame_elapsed_ms} ms vs 2000 ms ({self._fmt_ms(analysis.stop_lag_ms)}), "
            f"START_OK={self._fmt_ms(analysis.start_ack_latency_ms)}, "
            f"STOP_MARKED={self._fmt_ms(analysis.stop_ack_latency_ms)}, "
            f"STOP_OK={self._fmt_ms(None if session.stop_ok_elapsed_ms is None else session.stop_ok_elapsed_ms - (session.stop_command_elapsed_ms or 0.0))}, "
            f"endpoint_decoded={analysis.decoded_frame_count}/2, "
            f"first_clean={analysis.first_frame_clean}, "
            f"last_clean={analysis.last_frame_clean}, "
            f"fps={analysis.fps:.2f}, confidence={analysis.confidence:.2f}"
        )
        segment = session.segment_metrics or {}
        if segment:
            self.log(
                "Lag test segment metrics: "
                f"mux_start={self._fmt_us_as_ms(segment.get('chosen_start_offset_us'))}, "
                f"nearest_before={self._fmt_us_as_ms(segment.get('nearest_keyframe_before_start_offset_us'))}, "
                f"nearest_after={self._fmt_us_as_ms(segment.get('nearest_keyframe_after_start_offset_us'))}, "
                f"all_intra={segment.get('all_intra')}, "
                f"keyframes={segment.get('candidate_keyframe_count')}/{segment.get('candidate_sample_count')}"
            )
        if analysis.first_frame_image_path:
            self.log(f"Lag test first frame: {analysis.first_frame_image_path}")
        if analysis.last_frame_image_path:
            self.log(f"Lag test last frame: {analysis.last_frame_image_path}")
        if report_paths:
            self.log(f"Lag test report: {report_paths.get('json', '')}")
        session.display.set_phase("DONE")
        session.display.close()
        self._lag_test_session = None
        self._set_phone_preview_suspended_for_lag_test(False)
        self.update_start_stop_buttons()

    def _lag_test_local_video_path(self, session: HubLagTestSession) -> Optional[str]:
        return self._lag_test_local_capture_file_path(session, ".mp4")

    def _lag_test_local_segment_metrics(self, session: HubLagTestSession) -> Optional[dict]:
        segment_path = self._lag_test_local_capture_file_path(session, ".segment.json")
        if not segment_path:
            return None
        try:
            with open(segment_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            self.log(f"Could not read lag-test segment metrics: {exc}")
            return None

    def _lag_test_local_capture_file_path(self, session: HubLagTestSession, suffix: str) -> Optional[str]:
        capture_name = session.capture_name
        capture = session.capture_info or {}
        if not capture_name:
            return None
        suffix = suffix.lower()
        save_dir = self.get_save_dir()
        for file_info in capture.get("files", []):
            file_name = str(file_info.get("name", ""))
            if not file_name.lower().endswith(suffix):
                continue
            try:
                rel_path = lag_test_storage_relative_path(f"{capture_name}/{file_name}")
                path = resolve_safe_transfer_path(save_dir, rel_path)
            except ValueError:
                continue
            if os.path.isfile(path):
                return path
        capture_dirs = [
            os.path.join(save_dir, LAG_TEST_TRANSFER_ROOT, capture_name),
            os.path.join(save_dir, capture_name),
        ]
        for capture_dir in capture_dirs:
            if not os.path.isdir(capture_dir):
                continue
            for name in os.listdir(capture_dir):
                if name.lower().endswith(suffix):
                    return os.path.join(capture_dir, name)
        return None

    def _lag_test_client(self):
        session = self._lag_test_session
        if session is None:
            return None
        for client in self.client_entries:
            if client.get("key") == session.client_key:
                return client
        self._finish_lag_test_error("Lag test phone disconnected")
        return None

    def _lag_test_session_matches(self, client_key: str, label: str) -> bool:
        session = self._lag_test_session
        return session is not None and session.client_key == client_key and session.label == label

    def _finish_lag_test_error(self, message: str):
        session = self._lag_test_session
        if session is not None:
            session.display.set_phase("ERROR")
            session.display.close()
        self._lag_test_session = None
        self._set_phone_preview_suspended_for_lag_test(False)
        self._set_lag_test_status(f"Error: {message}")
        self.log(f"Lag test stopped: {message}")
        self.update_start_stop_buttons()

    def _set_phone_preview_suspended_for_lag_test(self, suspended: bool):
        pane = getattr(self, "phone_stream_pane", None)
        if pane is None:
            return
        try:
            pane.set_display_suspended(suspended)
        except Exception as exc:
            self.log(f"Could not update phone preview suspension: {exc}")

    def _set_lag_test_status(self, text: str):
        if hasattr(self, "lag_test_status_var"):
            self.lag_test_status_var.set(text)

    @staticmethod
    def _fmt_ms(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:.1f} ms"

    @staticmethod
    def _fmt_us_as_ms(value) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value) / 1000.0:.3f} ms"
        except Exception:
            return "n/a"

    # ------- button callbacks -------

    def start_capture(self, trigger_source="UI", send_to_arduino=True):
        if self.tcp is None:
            self.log(f"Phone connection is off; START blocked ({trigger_source})")
            return False
        if self.is_running:
            return False
        if self._pending_capture_completion_name is not None:
            self.log(f"Capture is still finishing; START blocked ({trigger_source})")
            return False
        self.flush_pending_naming_update()
        self.transfer_in_progress = self.has_active_transfer()
        if self.transfer_in_progress:
            self.log(f"Transfer in progress; START blocked ({trigger_source})")
            return False
        camera_block_reason = self.get_camera_start_block_reason()
        if camera_block_reason:
            self.log(f"START blocked ({trigger_source}): {camera_block_reason}")
            return False
        generated_name = self.build_generated_name(strict=True)
        if generated_name is None:
            return False

        self.tcp.broadcast("START")
        if send_to_arduino:
            self.arduino.start()
        self.active_capture_name = generated_name
        self.is_running = True
        self.update_start_stop_buttons()
        return True

    def stop_capture(self, trigger_source="UI", send_to_arduino=True):
        if self.tcp is None:
            self.log(f"Phone connection is off; STOP blocked ({trigger_source})")
            return False
        if not self.is_running:
            return False
        self.transfer_in_progress = self.has_active_transfer()
        if self.transfer_in_progress:
            self.log(f"Transfer in progress; STOP blocked ({trigger_source})")
            return False

        self.tcp.broadcast("STOP")
        if send_to_arduino:
            self.arduino.stop()
        completed_capture_name = self.active_capture_name or self.build_generated_name(
            strict=False, log_errors=False
        )
        self.active_capture_name = None
        self.is_running = False
        self._begin_pending_capture_completion(completed_capture_name)
        return True

    def on_arduino_serial_command(self, command: str):
        try:
            self.root.after(0, lambda cmd=command: self.handle_arduino_serial_command(cmd))
        except tk.TclError:
            pass

    def handle_arduino_serial_command(self, command: str):
        if not self.serial_arm_enabled:
            return
        if command == "START":
            self.start_capture(trigger_source="USB", send_to_arduino=False)
        elif command == "STOP":
            self.stop_capture(trigger_source="USB", send_to_arduino=False)

    def on_toggle_arm(self):
        if self.serial_arm_enabled:
            self.arduino.disarm_listener()
            self.serial_arm_enabled = False
        else:
            if not self.arduino.arm_listener():
                return
            self.serial_arm_enabled = True
        self.update_start_stop_buttons()

    def on_start(self):
        self.start_capture()

    def on_stop(self):
        self.stop_capture()

    def on_quit(self):
        if self._naming_update_after_id is not None:
            self.root.after_cancel(self._naming_update_after_id)
            self._naming_update_after_id = None
        if self._name_keepalive_after_id is not None:
            self.root.after_cancel(self._name_keepalive_after_id)
            self._name_keepalive_after_id = None
        if self._camera_apply_after_id is not None:
            self.root.after_cancel(self._camera_apply_after_id)
            self._camera_apply_after_id = None
        if self._camera_verify_after_id is not None:
            self.root.after_cancel(self._camera_verify_after_id)
            self._camera_verify_after_id = None
        if self._save_dir_update_after_id is not None:
            self.root.after_cancel(self._save_dir_update_after_id)
            self._save_dir_update_after_id = None
        self.cancel_delete_watchdog()
        if self._lag_test_session is not None:
            self._finish_lag_test_error("Application closed")
        self._clear_pending_capture_finalize_timers()
        if self.phone_stream_pane is not None:
            pane = self.phone_stream_pane
            self.phone_stream_pane = None
            pane.close()
        if self.discovery is not None:
            self.discovery.close()
            self.discovery = None
        if self.tcp is not None:
            self.tcp.close()
            self.tcp = None
        self.arduino.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
