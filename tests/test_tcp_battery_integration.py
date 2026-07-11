import sys
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from phone_protocol import BatteryStatus  # noqa: E402
from tcp_arduino_sync import TcpServer, _format_client_display_label  # noqa: E402


class TcpBatteryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.membership_updates = []
        self.metadata_updates = []
        self.server = TcpServer.__new__(TcpServer)
        self.server.log = self.logs.append
        self.server.clients_changed_callback = self.membership_updates.append
        self.server.client_metadata_changed_callback = (
            lambda infos, kind: self.metadata_updates.append((infos, kind))
        )
        self.server.message_callback = lambda _client, _line: None
        self.server.transfer_progress_callback = lambda _client: None
        self.server.lock = threading.Lock()
        self.client = {
            "addr": ("127.0.0.1", 54321),
            "key": "127.0.0.1:54321",
            "name": "TestPhone",
            "transport": "usb_adb_reverse",
            "transport_details": {},
            "battery": None,
        }
        self.server.clients = [self.client]

    def test_battery_updates_metadata_without_membership_refresh(self):
        self.server.handle_line(
            self.client,
            "BATTERY level_pct=87 status=charging plugged=usb",
        )

        self.assertEqual(
            self.client["battery"],
            BatteryStatus(level_pct=87, status="charging", plugged="usb"),
        )
        self.assertEqual(self.membership_updates, [])
        self.assertEqual(len(self.metadata_updates), 1)
        self.assertEqual(self.metadata_updates[0][1], "battery")
        self.assertEqual(
            _format_client_display_label(self.client),
            "TestPhone (USB) | 87% charging",
        )

        self.server.handle_line(
            self.client,
            "BATTERY level_pct=87 status=charging plugged=usb",
        )
        self.assertEqual(len(self.metadata_updates), 1)

    def test_invalid_battery_is_ignored(self):
        self.server.handle_line(
            self.client,
            "BATTERY level_pct=150 status=charging plugged=usb",
        )

        self.assertIsNone(self.client["battery"])
        self.assertEqual(self.metadata_updates, [])
        self.assertTrue(any("Ignored invalid BATTERY" in line for line in self.logs))

    def test_hello_and_transport_are_metadata_only(self):
        self.server.handle_line(self.client, "HELLO RenamedPhone")
        self.server.handle_line(self.client, "TRANSPORT wifi host=192.168.1.10")

        self.assertEqual(self.membership_updates, [])
        self.assertEqual(
            [kind for _infos, kind in self.metadata_updates],
            ["hello", "transport"],
        )


if __name__ == "__main__":
    unittest.main()
