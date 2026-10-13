import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from lag_test.screen_timing import DwmScreenClock, screen_interval_ms, screen_lag_ms  # noqa: E402
from tcp_arduino_sync import _lag_test_screen_timing  # noqa: E402


# Display start at perf 10 000 ms. Value 1000 ms is computed at 11 000 ms but each tick
# reaches the screen 12 ms later; ticks every ~4.2 ms like a 240 Hz monitor.
TIMELINE = [
    (990, 11002.0),
    (995, 11006.2),
    (1000, 11012.0),
    (1000, 11016.2),
    (1005, 11020.4),
    (1995, 12008.0),
    (2000, 12012.3),
    (2005, 12016.5),
]


def analysis(first_ms=1000, last_ms=2000, first_predicted=False, last_predicted=False):
    return SimpleNamespace(
        first_frame_elapsed_ms=first_ms,
        last_frame_elapsed_ms=last_ms,
        first_frame_elapsed_predicted=first_predicted,
        last_frame_elapsed_predicted=last_predicted,
    )


class ScreenIntervalTests(unittest.TestCase):
    def test_interval_runs_from_first_tick_to_next_value(self):
        self.assertEqual(screen_interval_ms(TIMELINE, 1000), (11012.0, 11020.4))

    def test_missing_value_or_last_value_has_no_interval(self):
        self.assertIsNone(screen_interval_ms(TIMELINE, 1500))
        self.assertIsNone(screen_interval_ms(TIMELINE, 2005))
        self.assertIsNone(screen_interval_ms([], 1000))

    def test_time_going_backwards_is_rejected(self):
        self.assertIsNone(screen_interval_ms([(1000, 50.0), (1005, 40.0)], 1000))

    def test_lag_is_interval_midpoint_minus_target(self):
        lag, half_width = screen_lag_ms(TIMELINE, 1000, 11000.0)
        self.assertAlmostEqual(lag, 16.2)
        self.assertAlmostEqual(half_width, 4.2)

    def test_clock_without_dwm_is_unavailable(self):
        clock = DwmScreenClock.__new__(DwmScreenClock)
        clock._dwm = None
        clock._qpc_to_ms = None
        self.assertFalse(clock.available)
        self.assertIsNone(clock.present_time_ms())


class LagTestScreenTimingTests(unittest.TestCase):
    def test_direct_endpoints_get_screen_lag_and_frame_adjusted_residual(self):
        fields = _lag_test_screen_timing(
            analysis(1000, 2000),
            TIMELINE,
            10.0,
            {"chosen_start_offset_us": 600, "end_offset_error_us": 2000},
        )

        self.assertEqual(fields["screen_timing_source"], "dwm_vblank")
        self.assertEqual(fields["screen_timeline_ticks"], len(TIMELINE))
        self.assertAlmostEqual(fields["screen_start_lag_ms"], 16.2)
        self.assertAlmostEqual(fields["screen_start_half_width_ms"], 4.2)
        self.assertAlmostEqual(fields["screen_start_residual_ms"], 15.6)
        self.assertAlmostEqual(fields["screen_stop_lag_ms"], 14.4)
        self.assertAlmostEqual(fields["screen_stop_residual_ms"], 12.4)

    def test_predicted_endpoint_gets_no_screen_lag(self):
        fields = _lag_test_screen_timing(analysis(1000, 2000, last_predicted=True), TIMELINE, 10.0, {})

        self.assertIsNotNone(fields["screen_start_lag_ms"])
        self.assertIsNone(fields["screen_start_residual_ms"])
        self.assertIsNone(fields["screen_stop_lag_ms"])
        self.assertIsNone(fields["screen_stop_half_width_ms"])

    def test_missing_timeline_is_reported_as_unavailable(self):
        fields = _lag_test_screen_timing(analysis(), [], 10.0, None)

        self.assertEqual(fields["screen_timing_source"], "unavailable")
        self.assertEqual(fields["screen_timeline_ticks"], 0)
        for side in ("start", "stop"):
            self.assertIsNone(fields[f"screen_{side}_lag_ms"])

    def test_report_fields_are_flat_scalars(self):
        fields = _lag_test_screen_timing(analysis(), TIMELINE, 10.0, {"chosen_start_offset_us": 0})
        for value in fields.values():
            self.assertTrue(value is None or isinstance(value, (int, float, str)))


if __name__ == "__main__":
    unittest.main()
