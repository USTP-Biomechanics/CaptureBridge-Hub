import sys
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from phone_protocol import (  # noqa: E402
    BatteryStatus,
    ClientTimeSync,
    MAX_PROTOCOL_LINE_BYTES,
    format_battery_status,
    parse_battery_status,
    protocol_line_is_too_long,
)


class BatteryStatusTests(unittest.TestCase):
    def test_parse_complete_status(self):
        battery = parse_battery_status("level_pct=87 status=charging plugged=usb")
        self.assertEqual(
            battery,
            BatteryStatus(level_pct=87, status="charging", plugged="usb"),
        )

    def test_parse_is_case_insensitive_and_level_is_optional(self):
        battery = parse_battery_status("status=FULL plugged=AC")
        self.assertEqual(
            battery,
            BatteryStatus(level_pct=None, status="full", plugged="ac"),
        )

    def test_format_concise_status(self):
        battery = BatteryStatus(level_pct=19, status="not_charging", plugged="none")
        self.assertEqual(format_battery_status(battery), "19% not charging")

    def test_format_can_include_power_source(self):
        battery = BatteryStatus(level_pct=87, status="charging", plugged="usb")
        self.assertEqual(
            format_battery_status(battery, include_power_source=True),
            "87% charging via USB",
        )

    def test_reject_invalid_level(self):
        for text in ("level_pct=-1", "level_pct=101", "level_pct=87.5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_battery_status(text)

    def test_reject_invalid_enums_and_bare_labels(self):
        for text in (
            "level_pct=50 status=fast_charging plugged=usb",
            "level_pct=50 status=charging plugged=cable",
            "50 status=charging plugged=usb",
            "extra=value",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_battery_status(text)


class ClientTimeSyncTests(unittest.TestCase):
    def test_unknown_sequence_is_rejected(self):
        sync = ClientTimeSync()
        self.assertIsNone(
            sync.complete_sample(
                seq=99,
                hub_tx_ns=1,
                phone_rx_ns=2,
                phone_tx_ns=3,
            )
        )
        self.assertEqual(sync.summary()["sample_count"], 0)

    def test_echoed_hub_timestamp_cannot_replace_pending_timestamp(self):
        sync = ClientTimeSync()
        seq, hub_tx_ns = sync.begin_sample()
        sample = sync.complete_sample(
            seq=seq,
            hub_tx_ns=hub_tx_ns + 10**12,
            phone_rx_ns=hub_tx_ns + 1_000_000,
            phone_tx_ns=hub_tx_ns + 1_100_000,
        )
        self.assertIsNotNone(sample)
        self.assertEqual(sample.hub_tx_ns, hub_tx_ns)

    def test_three_recent_low_rtt_samples_are_usable(self):
        sync = ClientTimeSync()
        for _ in range(3):
            seq, hub_tx_ns = sync.begin_sample()
            sample = sync.complete_sample(
                seq=seq,
                hub_tx_ns=hub_tx_ns,
                phone_rx_ns=hub_tx_ns + 100_000,
                phone_tx_ns=hub_tx_ns + 110_000,
            )
            self.assertIsNotNone(sample)

        self.assertTrue(sync.is_usable())
        self.assertEqual(sync.summary()["sample_count"], 3)

    def test_concurrent_begin_and_complete_are_safe(self):
        sync = ClientTimeSync(max_samples=500)
        errors = []

        def worker():
            try:
                for _ in range(50):
                    seq, hub_tx_ns = sync.begin_sample()
                    sync.complete_sample(
                        seq=seq,
                        hub_tx_ns=hub_tx_ns,
                        phone_rx_ns=hub_tx_ns + 100_000,
                        phone_tx_ns=hub_tx_ns + 110_000,
                    )
            except Exception as exc:  # pragma: no cover - assertion captures thread failures
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(sync.samples), 200)


class ProtocolLineLimitTests(unittest.TestCase):
    def test_limit_boundary_with_and_without_newline(self):
        limit = MAX_PROTOCOL_LINE_BYTES
        self.assertFalse(protocol_line_is_too_long(limit, -1))
        self.assertTrue(protocol_line_is_too_long(limit + 1, -1))
        self.assertFalse(protocol_line_is_too_long(limit + 100, limit))
        self.assertTrue(protocol_line_is_too_long(limit + 100, limit + 1))


if __name__ == "__main__":
    unittest.main()
