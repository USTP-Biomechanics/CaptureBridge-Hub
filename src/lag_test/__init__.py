"""Automated CaptureBridge phone start/stop lag test helpers."""

from .analyzer import LagAnalysisResult, LagTiming, analyze_lag_video
from .display import LagTimingDisplay
from .report import write_lag_report
from .screen_timing import screen_lag_ms

__all__ = [
    "LagAnalysisResult",
    "LagTiming",
    "LagTimingDisplay",
    "analyze_lag_video",
    "screen_lag_ms",
    "write_lag_report",
]
