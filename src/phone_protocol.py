from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


TIME_SYNC_MAX_SAMPLE_AGE_SEC = 10.0
TIME_SYNC_MAX_BEST_RTT_MS = 250.0
TIME_SYNC_MIN_SAMPLES_FOR_SCHEDULED = 3
SCHEDULED_COMMAND_MIN_SEND_AHEAD_MS = 150.0
SCHEDULED_COMMAND_SAFETY_MARGIN_MS = 100.0
SCHEDULED_COMMAND_MAX_SEND_AHEAD_MS = 900.0
MAX_PROTOCOL_LINE_BYTES = 1024 * 1024

BATTERY_STATUSES = frozenset(
    {
        "charging",
        "full",
        "discharging",
        "not_charging",
        "unknown",
    }
)
BATTERY_PLUGGED_SOURCES = frozenset(
    {
        "usb",
        "ac",
        "wireless",
        "dock",
        "none",
        "unknown",
    }
)


@dataclass(frozen=True)
class BatteryStatus:
    level_pct: Optional[int]
    status: str
    plugged: str


def parse_protocol_fields(text: str) -> Tuple[List[str], Dict[str, str]]:
    labels = []
    fields = {}
    for token in str(text or "").split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
        else:
            labels.append(token)
    return labels, fields


def field_float(fields: Dict[str, str], key: str) -> Optional[float]:
    try:
        return float(fields[key])
    except (KeyError, TypeError, ValueError):
        return None


def field_int(fields: Dict[str, str], key: str) -> Optional[int]:
    try:
        return int(fields[key])
    except (KeyError, TypeError, ValueError):
        return None


def protocol_line_is_too_long(
    buffer_length: int,
    newline_index: int,
    max_line_bytes: int = MAX_PROTOCOL_LINE_BYTES,
) -> bool:
    if newline_index < 0:
        return buffer_length > max_line_bytes
    return newline_index > max_line_bytes


def parse_battery_status(text: str) -> BatteryStatus:
    labels, fields = parse_protocol_fields(text)
    if labels:
        raise ValueError("BATTERY accepts only key=value fields")
    if not any(key in fields for key in ("level_pct", "status", "plugged")):
        raise ValueError("BATTERY did not contain any supported fields")

    level_pct = None
    if "level_pct" in fields:
        try:
            level_pct = int(fields["level_pct"])
        except (TypeError, ValueError) as exc:
            raise ValueError("level_pct must be an integer from 0 to 100") from exc
        if not 0 <= level_pct <= 100:
            raise ValueError("level_pct must be from 0 to 100")

    status = str(fields.get("status", "unknown")).strip().lower()
    if status not in BATTERY_STATUSES:
        raise ValueError(f"unsupported battery status: {status or '<empty>'}")

    plugged = str(fields.get("plugged", "unknown")).strip().lower()
    if plugged not in BATTERY_PLUGGED_SOURCES:
        raise ValueError(f"unsupported battery power source: {plugged or '<empty>'}")

    return BatteryStatus(level_pct=level_pct, status=status, plugged=plugged)


def format_battery_status(battery: Optional[BatteryStatus], include_power_source: bool = False) -> str:
    if battery is None:
        return ""

    parts = []
    if battery.level_pct is not None:
        parts.append(f"{battery.level_pct}%")

    status_labels = {
        "charging": "charging",
        "full": "full",
        "discharging": "discharging",
        "not_charging": "not charging",
    }
    status_text = status_labels.get(battery.status)
    if status_text:
        parts.append(status_text)

    if include_power_source and battery.plugged not in ("none", "unknown"):
        power_labels = {
            "usb": "USB",
            "ac": "AC",
            "wireless": "wireless",
            "dock": "dock",
        }
        parts.append(f"via {power_labels[battery.plugged]}")

    return " ".join(parts) if parts else "battery unknown"


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


@dataclass(frozen=True)
class TimeSyncSample:
    seq: int
    hub_tx_ns: int
    hub_rx_ns: int
    phone_rx_ns: int
    phone_tx_ns: int
    rtt_ms: float
    offset_ns: int
    recorded_perf: float


class ClientTimeSync:
    def __init__(self, max_samples: int = 120):
        self.max_samples = max(1, int(max_samples))
        self.samples: List[TimeSyncSample] = []
        self.pending: Dict[int, int] = {}
        self.next_seq = 1
        self._lock = threading.RLock()

    def begin_sample(self) -> Tuple[int, int]:
        with self._lock:
            seq = self.next_seq
            self.next_seq += 1
            hub_tx_ns = time.perf_counter_ns()
            self.pending[seq] = hub_tx_ns
            if len(self.pending) > 256:
                stale = sorted(self.pending)[: len(self.pending) - 256]
                for stale_seq in stale:
                    self.pending.pop(stale_seq, None)
            return seq, hub_tx_ns

    def complete_sample(
        self,
        seq: int,
        hub_tx_ns: int,
        phone_rx_ns: int,
        phone_tx_ns: int,
    ) -> Optional[TimeSyncSample]:
        del hub_tx_ns
        with self._lock:
            original_hub_tx_ns = self.pending.pop(seq, None)
            if original_hub_tx_ns is None:
                return None

            hub_rx_ns = time.perf_counter_ns()
            if phone_rx_ns <= 0 or phone_tx_ns <= 0 or phone_tx_ns < phone_rx_ns:
                return None
            phone_processing_ns = phone_tx_ns - phone_rx_ns
            rtt_ns = (hub_rx_ns - original_hub_tx_ns) - phone_processing_ns
            if rtt_ns < 0:
                rtt_ns = 0
            hub_mid_ns = (original_hub_tx_ns + hub_rx_ns) // 2
            phone_mid_ns = (phone_rx_ns + phone_tx_ns) // 2
            sample = TimeSyncSample(
                seq=seq,
                hub_tx_ns=original_hub_tx_ns,
                hub_rx_ns=hub_rx_ns,
                phone_rx_ns=phone_rx_ns,
                phone_tx_ns=phone_tx_ns,
                rtt_ms=rtt_ns / 1_000_000.0,
                offset_ns=phone_mid_ns - hub_mid_ns,
                recorded_perf=time.perf_counter(),
            )
            self.samples.append(sample)
            if len(self.samples) > self.max_samples:
                self.samples = self.samples[-self.max_samples :]
            return sample

    def recent_samples(
        self,
        max_age_sec: float = TIME_SYNC_MAX_SAMPLE_AGE_SEC,
    ) -> List[TimeSyncSample]:
        with self._lock:
            cutoff = time.perf_counter() - max(0.0, float(max_age_sec))
            return [sample for sample in self.samples if sample.recorded_perf >= cutoff]

    def best_sample(self) -> Optional[TimeSyncSample]:
        with self._lock:
            samples = self.recent_samples()
            if not samples:
                samples = list(self.samples[-20:])
            if not samples:
                return None
            return min(samples, key=lambda sample: sample.rtt_ms)

    def best_offset_ns(self) -> Optional[int]:
        sample = self.best_sample()
        return None if sample is None else sample.offset_ns

    def is_usable(self) -> bool:
        with self._lock:
            samples = self.recent_samples()
            best = min(samples, key=lambda sample: sample.rtt_ms) if samples else None
            return (
                len(samples) >= TIME_SYNC_MIN_SAMPLES_FOR_SCHEDULED
                and best is not None
                and best.rtt_ms <= TIME_SYNC_MAX_BEST_RTT_MS
            )

    def scheduled_send_ahead_ms(self) -> float:
        with self._lock:
            samples = self.recent_samples()
            if not samples:
                samples = list(self.samples[-20:])
            rtts = [sample.rtt_ms for sample in samples]
            p95 = _percentile(rtts, 95.0)
            basis = float(p95) if p95 is not None else SCHEDULED_COMMAND_MIN_SEND_AHEAD_MS
            send_ahead = max(
                SCHEDULED_COMMAND_MIN_SEND_AHEAD_MS,
                basis + SCHEDULED_COMMAND_SAFETY_MARGIN_MS,
            )
            return min(SCHEDULED_COMMAND_MAX_SEND_AHEAD_MS, send_ahead)

    def summary(self) -> dict:
        with self._lock:
            samples = self.recent_samples()
            if not samples:
                samples = list(self.samples[-20:])
            rtts = [sample.rtt_ms for sample in samples]
            offsets_ms = [sample.offset_ns / 1_000_000.0 for sample in samples]
            best = min(samples, key=lambda sample: sample.rtt_ms) if samples else None
            median_offset = _median(offsets_ms)
            offset_jitter = None
            if median_offset is not None:
                deviations = [abs(value - median_offset) for value in offsets_ms]
                offset_jitter = _median(deviations)
            return {
                "sample_count": len(samples),
                "pending_count": len(self.pending),
                "usable": self.is_usable() if samples else False,
                "best_seq": None if best is None else best.seq,
                "best_rtt_ms": None if best is None else best.rtt_ms,
                "rtt_min_ms": min(rtts) if rtts else None,
                "rtt_median_ms": _median(rtts),
                "rtt_p95_ms": _percentile(rtts, 95.0),
                "rtt_max_ms": max(rtts) if rtts else None,
                "offset_ms": None if best is None else best.offset_ns / 1_000_000.0,
                "offset_median_ms": median_offset,
                "offset_jitter_median_ms": offset_jitter,
                "send_ahead_ms": (
                    self.scheduled_send_ahead_ms()
                    if samples
                    else SCHEDULED_COMMAND_MIN_SEND_AHEAD_MS
                ),
                "latest_age_sec": (
                    None if not samples else time.perf_counter() - samples[-1].recorded_perf
                ),
            }
