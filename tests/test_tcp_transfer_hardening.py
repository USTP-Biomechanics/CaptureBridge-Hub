import queue
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tcp_arduino_sync import (  # noqa: E402
    MAX_TRANSFER_FILE_BYTES,
    MAX_TRANSFER_TOTAL_BYTES,
    PARTIAL_TRANSFER_SUFFIX,
    TcpServer,
)


class _ClosedConnection:
    @staticmethod
    def recv(_size):
        return b""


class TcpTransferHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.logs = []
        self.messages = []
        self.progress = []
        self.server = TcpServer.__new__(TcpServer)
        self.server.log = self.logs.append
        self.server.message_callback = (
            lambda _client, line: self.messages.append(line)
        )
        self.server.transfer_progress_callback = (
            lambda client: self.progress.append(
                (client.get("transfer_received"), client.get("transfer_active"))
            )
        )
        self.server.save_dir_getter = lambda: self.temp_dir.name
        self.client = self._make_client()

    @staticmethod
    def _make_client():
        return {
            "addr": ("127.0.0.1", 54321),
            "closed": False,
            "send_queue": queue.Queue(),
            "rx_mode": "line",
            "file_bytes_remaining": 0,
            "file_handle": None,
            "file_path": None,
            "file_final_path": None,
            "file_total": 0,
            "file_received": 0,
            "transfer_total": 0,
            "transfer_received": 0,
            "transfer_active": False,
            "transfer_authorized": False,
            "transfer_request_command": "",
            "current_file_rel": "",
            "pending_file_done_rel": "",
            "pending_file_temp_path": None,
            "pending_file_final_path": None,
            "pending_file_size": 0,
        }

    def _authorize_and_begin(self, total_size):
        self.server.send_to_client(self.client, "GET capture")
        self.server.handle_line(
            self.client,
            f"TRANSFER_BEGIN capture 1 {total_size}",
        )

    def test_complete_file_is_published_only_after_matching_file_done(self):
        self._authorize_and_begin(5)

        result = self.server.handle_line(
            self.client,
            "FILE_BEGIN capture/file.bin 5",
        )
        self.assertEqual(result, "START_FILE")

        final_path = Path(self.temp_dir.name) / "capture" / "file.bin"
        temp_path = Path(f"{final_path}{PARTIAL_TRANSFER_SUFFIX}")
        self.assertFalse(final_path.exists())
        self.assertTrue(temp_path.exists())

        self.server._receive_file_payload(self.client, None, bytearray(b"hello"))
        self.assertFalse(final_path.exists())
        self.assertEqual(temp_path.read_bytes(), b"hello")

        self.server.handle_line(self.client, "FILE_DONE capture/file.bin")
        self.assertEqual(final_path.read_bytes(), b"hello")
        self.assertFalse(temp_path.exists())

        self.server.handle_line(self.client, "TRANSFER_DONE capture")
        self.assertFalse(self.client["transfer_active"])
        self.assertFalse(self.client["transfer_authorized"])

    def test_unsolicited_file_begin_is_rejected(self):
        with self.assertRaisesRegex(ConnectionError, "Unsolicited FILE_BEGIN"):
            self.server.handle_line(
                self.client,
                "FILE_BEGIN capture/file.bin 5",
            )

    def test_per_file_limit_is_enforced(self):
        self._authorize_and_begin(0)
        with self.assertRaisesRegex(ConnectionError, "per-file limit"):
            self.server.handle_line(
                self.client,
                f"FILE_BEGIN capture/file.bin {MAX_TRANSFER_FILE_BYTES + 1}",
            )

    def test_total_request_limit_is_enforced_across_files(self):
        self._authorize_and_begin(0)
        self.client["transfer_received"] = MAX_TRANSFER_TOTAL_BYTES - 1
        with self.assertRaisesRegex(ConnectionError, "request limit"):
            self.server.handle_line(
                self.client,
                "FILE_BEGIN capture/file.bin 2",
            )

    def test_declared_transfer_total_is_enforced(self):
        self._authorize_and_begin(4)
        with self.assertRaisesRegex(ConnectionError, "size declared"):
            self.server.handle_line(
                self.client,
                "FILE_BEGIN capture/file.bin 5",
            )

    def test_transfer_done_requires_exact_declared_byte_count(self):
        self._authorize_and_begin(5)
        self.client["transfer_received"] = 4

        with self.assertRaisesRegex(ConnectionError, "byte count mismatch"):
            self.server.handle_line(self.client, "TRANSFER_DONE capture")

        self.assertFalse(self.client["transfer_authorized"])

    def test_file_done_path_mismatch_removes_staged_file(self):
        self._authorize_and_begin(5)
        self.server.handle_line(self.client, "FILE_BEGIN capture/file.bin 5")
        self.server._receive_file_payload(self.client, None, bytearray(b"hello"))
        temp_path = Path(self.client["pending_file_temp_path"])

        with self.assertRaisesRegex(ConnectionError, "path mismatch"):
            self.server.handle_line(self.client, "FILE_DONE capture/other.bin")

        self.assertFalse(temp_path.exists())

    def test_interrupted_payload_cleanup_removes_partial_file(self):
        self._authorize_and_begin(5)
        self.server.handle_line(self.client, "FILE_BEGIN capture/file.bin 5")
        temp_path = Path(self.client["file_path"])

        with self.assertRaisesRegex(ConnectionError, "Socket closed"):
            self.server._receive_file_payload(
                self.client,
                _ClosedConnection(),
                bytearray(b"hi"),
            )
        self.server._abort_incoming_transfer(self.client)

        self.assertFalse(temp_path.exists())
        self.assertFalse(self.client["transfer_authorized"])


if __name__ == "__main__":
    unittest.main()
