"""When each lag-test timecode actually reached the screen.

Windows composes windowed apps through DWM. Between computing a timecode and
scanning it out there are several refresh periods (about 12 ms at 240 Hz on the
lab PC), so a camera frame shows the screen as it was that much earlier. The
display records, per tick, the DWM vertical blank of the frame holding the
timecode; analysis then compares camera frames with real screen time instead of
render time. The monitor's own processing delay is not visible to Windows and
is not included.
"""

import math
import sys
import time
from ctypes import Structure, WinDLL, byref, c_int64, c_uint32, c_uint64, sizeof
from typing import Optional, Sequence, Tuple

# (marker value in ms, screen time as time.perf_counter() milliseconds)
ScreenTick = Tuple[int, float]


class _Ratio(Structure):
    _pack_ = 1
    _fields_ = [("numerator", c_uint32), ("denominator", c_uint32)]


class _DwmTimingInfo(Structure):
    _pack_ = 1
    _fields_ = [
        ("cbSize", c_uint32),
        ("rateRefresh", _Ratio),
        ("qpcRefreshPeriod", c_uint64),
        ("rateCompose", _Ratio),
        ("qpcVBlank", c_uint64),
        ("cRefresh", c_uint64),
        ("cDXRefresh", c_uint32),
        ("qpcCompose", c_uint64),
        ("cFrame", c_uint64),
        ("cDXPresent", c_uint32),
        ("cRefreshFrame", c_uint64),
        ("cFrameSubmitted", c_uint64),
        ("cDXPresentSubmitted", c_uint32),
        ("cFrameConfirmed", c_uint64),
        ("cDXPresentConfirmed", c_uint32),
        ("cRefreshConfirmed", c_uint64),
        ("cDXRefreshConfirmed", c_uint32),
        ("cFramesLate", c_uint64),
        ("cFramesOutstanding", c_uint32),
        ("cFrameDisplayed", c_uint64),
        ("qpcFrameDisplayed", c_uint64),
        ("cRefreshFrameDisplayed", c_uint64),
        ("cFrameComplete", c_uint64),
        ("qpcFrameComplete", c_uint64),
        ("cFramePending", c_uint64),
        ("qpcFramePending", c_uint64),
        ("cFramesDisplayed", c_uint64),
        ("cFramesComplete", c_uint64),
        ("cFramesPending", c_uint64),
        ("cFramesAvailable", c_uint64),
        ("cFramesDropped", c_uint64),
        ("cFramesMissed", c_uint64),
        ("cRefreshNextDisplayed", c_uint64),
        ("cRefreshNextPresented", c_uint64),
        ("cRefreshesDisplayed", c_uint64),
        ("cRefreshesPresented", c_uint64),
        ("cRefreshStarted", c_uint64),
        ("cPixelsReceived", c_uint64),
        ("cPixelsDrawn", c_uint64),
        ("cBuffersEmpty", c_uint64),
    ]


class DwmScreenClock:
    """Waits for DWM to compose the current window content and returns its vblank time."""

    def __init__(self):
        self._dwm = None
        self._qpc_to_ms = None
        if sys.platform != "win32":
            return
        try:
            frequency = c_int64()
            if not WinDLL("kernel32").QueryPerformanceFrequency(byref(frequency)) or frequency.value <= 0:
                return
            self._qpc_to_ms = 1000.0 / frequency.value
            self._dwm = WinDLL("dwmapi")
        except OSError:
            self._dwm = None

    @property
    def available(self) -> bool:
        return self._dwm is not None

    def present_time_ms(self) -> Optional[float]:
        """Screen time (perf_counter ms) of the frame holding everything drawn so far, or None."""
        if self._dwm is None:
            return None
        try:
            if self._dwm.DwmFlush() != 0:
                return None
            flushed_ms = time.perf_counter() * 1000.0
            info = _DwmTimingInfo()
            info.cbSize = sizeof(_DwmTimingInfo)
            if self._dwm.DwmGetCompositionTimingInfo(None, byref(info)) != 0 or not info.qpcRefreshPeriod:
                return None
        except OSError:
            return None
        # time.perf_counter() and DWM both count QueryPerformanceCounter ticks. DWM reports the
        # vblank of the composition that follows the flush; never use one before the flush.
        period_ms = info.qpcRefreshPeriod * self._qpc_to_ms
        vblank_ms = info.qpcVBlank * self._qpc_to_ms
        return vblank_ms + max(0, math.ceil((flushed_ms - vblank_ms) / period_ms)) * period_ms


def screen_interval_ms(timeline: Sequence[ScreenTick], marker_ms: int) -> Optional[Tuple[float, float]]:
    """Screen time span during which marker_ms was shown: its first tick until the next new value."""
    first = next((index for index, (value, _) in enumerate(timeline) if value == marker_ms), None)
    if first is None:
        return None
    following = next(
        (index for index in range(first + 1, len(timeline)) if timeline[index][0] != marker_ms),
        None,
    )
    if following is None:
        return None
    shown_ms, replaced_ms = timeline[first][1], timeline[following][1]
    if replaced_ms < shown_ms:
        return None
    return shown_ms, replaced_ms


def screen_lag_ms(
    timeline: Sequence[ScreenTick],
    decoded_marker_ms: int,
    target_screen_ms: float,
) -> Optional[Tuple[float, float]]:
    """(lag, half-width) of a decoded timecode against real screen time, both in ms."""
    span = screen_interval_ms(timeline, decoded_marker_ms)
    if span is None:
        return None
    shown_ms, replaced_ms = span
    return (shown_ms + replaced_ms) / 2.0 - target_screen_ms, (replaced_ms - shown_ms) / 2.0
