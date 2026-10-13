import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tcp_arduino_sync import (  # noqa: E402
    App,
    _format_client_line_for_log,
    _lag_test_camera_report_fields,
    normalize_camera_profile,
)


SAVED_PROFILE = {
    "width": 1920,
    "height": 1080,
    "fps": 240.0,
    "iso": 800.0,
    "shutterSeconds": 1 / 480,
}


def _current(**overrides):
    """A phone's SETTINGS_OK current object; 1.0.16 adds the automatic high-speed EV fields."""
    current = {
        "width": 1920,
        "height": 1080,
        "fps": 240.0,
        "highSpeed": True,
        "iso": 800,
        "shutterSeconds": 1 / 480,
    }
    current.update(overrides)
    return current


def _settings_payload(current):
    return {
        "position": "back",
        "resolutions": [{"width": 1920, "height": 1080, "fps": 240.0, "highSpeed": True}],
        "current": current,
    }


class CameraProfileTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)

    def test_saved_profile_keeps_only_the_camera_mode_iso_and_shutter(self):
        profile = normalize_camera_profile({**SAVED_PROFILE, "highSpeedAeCompensationEv": -2.0})

        self.assertEqual(profile, SAVED_PROFILE)

    def test_automatic_ev_does_not_affect_settings_sync(self):
        payload = _settings_payload(
            _current(highSpeedAeCompensationMode="auto", highSpeedAeCompensationAppliedEv=-1.7)
        )

        profile = self.app.build_camera_profile_from_current(payload)

        self.assertNotIn("highSpeedAeCompensationEv", profile)
        self.assertTrue(self.app.camera_profiles_match(profile, SAVED_PROFILE))

    def test_lag_test_profile_sends_no_ev(self):
        payload = _settings_payload(_current(highSpeedAeCompensationAppliedEv=-1.7))

        self.assertNotIn("highSpeedAeCompensationEv", self.app._lag_test_current_camera_profile(payload))


class CameraReportAndLogTests(unittest.TestCase):
    def test_lag_report_fields_are_flat_scalars(self):
        payload = _settings_payload(
            _current(highSpeedAeCompensationMode="auto", highSpeedAeCompensationAppliedEv=-1.7)
        )

        self.assertEqual(
            _lag_test_camera_report_fields(payload),
            {
                "camera_width": 1920,
                "camera_height": 1080,
                "camera_fps": 240.0,
                "camera_high_speed": True,
                "camera_iso": 800,
                "camera_shutter_seconds": 1 / 480,
                "camera_high_speed_ae_compensation_mode": "auto",
                "camera_high_speed_ae_compensation_applied_ev": -1.7,
            },
        )

    def test_lag_report_fields_are_none_without_phone_values(self):
        fields = _lag_test_camera_report_fields(_settings_payload(_current()))
        self.assertIsNone(fields["camera_high_speed_ae_compensation_mode"])
        self.assertIsNone(fields["camera_high_speed_ae_compensation_applied_ev"])

        self.assertEqual(set(_lag_test_camera_report_fields(None).values()), {None})

    def test_settings_log_summary_shows_applied_ev(self):
        payload = _settings_payload(_current(highSpeedAeCompensationAppliedEv=-1.7))
        old_payload = _settings_payload(_current())

        self.assertEqual(
            _format_client_line_for_log(f"SETTINGS_OK {json.dumps(payload)}"),
            "SETTINGS_OK current=1920 x 1080 @ 240 fps ev=-1.7 modes=1 options=1 position=back",
        )
        self.assertEqual(
            _format_client_line_for_log(f"SETTINGS_LIST_OK {json.dumps(old_payload)}"),
            "SETTINGS_LIST_OK current=1920 x 1080 @ 240 fps modes=1 options=1 position=back",
        )


if __name__ == "__main__":
    unittest.main()
