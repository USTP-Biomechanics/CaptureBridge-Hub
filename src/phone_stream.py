import importlib
import io
import json
import socket
import struct
import threading
import time
import tkinter as tk
from typing import Callable, Dict, Optional


PHONE_PREVIEW_MAGICS = (bytes((70, 76, 51, 68)),)
UDP_VERSION = 1
UDP_HEADER_BE = struct.Struct("!4sBBIHHHHIQ")
UDP_HEADER_LE = struct.Struct("<4sBBIHHHHIQ")


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class PhoneStreamConfig:
    def __init__(
        self,
        enabled: bool = True,
        udp_port: int = 6101,
        max_fps: int = 20,
        jpeg_quality: int = 70,
        max_dimension: int = 1280,
        socket_buffer_bytes: int = 4 * 1024 * 1024,
    ):
        self.enabled = enabled
        self.udp_port = udp_port
        self.max_fps = max_fps
        self.jpeg_quality = jpeg_quality
        self.max_dimension = max_dimension
        self.socket_buffer_bytes = socket_buffer_bytes

    @classmethod
    def from_mapping(cls, raw: Optional[dict], _base_dir: str = "") -> "PhoneStreamConfig":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            udp_port=_clamp_int(raw.get("udp_port"), 6101, 1024, 65535),
            max_fps=_clamp_int(raw.get("max_fps"), 20, 1, 240),
            jpeg_quality=_clamp_int(raw.get("jpeg_quality"), 70, 20, 95),
            max_dimension=_clamp_int(raw.get("max_dimension"), 1280, 0, 8192),
            socket_buffer_bytes=_clamp_int(
                raw.get("socket_buffer_bytes"),
                4 * 1024 * 1024,
                256 * 1024,
                64 * 1024 * 1024,
            ),
        )

    def build_start_payload(self, host: str, protocol: str = "udp", stream_key: str = "") -> str:
        payload = {
            "host": host,
            "port": self.udp_port,
            "protocol": protocol,
            "maxFps": self.max_fps,
            "jpegQuality": self.jpeg_quality,
            "maxDimension": self.max_dimension,
        }
        if stream_key:
            payload["streamKey"] = stream_key
        return json.dumps(payload, separators=(",", ":"))


class UdpPhoneStreamReceiver:
    def __init__(
        self,
        port: int,
        socket_buffer_bytes: int,
        on_frame: Callable[[str, bytes, dict], None],
        log_callback: Callable[[str], None],
    ):
        self.port = port
        self.socket_buffer_bytes = socket_buffer_bytes
        self.on_frame = on_frame
        self.log = log_callback
        self._running = True
        self._assemblies: Dict[str, dict] = {}
        self._stats_lock = threading.Lock()
        self._stats = {
            "packets": 0,
            "tcp_connections": 0,
            "valid_packets": 0,
            "invalid_packets": 0,
            "completed_frames": 0,
            "stale_frames": 0,
            "last_packet_time": 0.0,
            "last_valid_packet_time": 0.0,
            "last_frame_time": 0.0,
            "last_source_ip": "",
            "last_valid_source_ip": "",
            "last_invalid_size": 0,
            "last_header_endian": "",
        }
        self._last_invalid_log = 0.0
        self._last_stale_log = 0.0
        self._last_little_endian_log = 0.0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.socket_buffer_bytes)
        except OSError:
            pass
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(1.0)

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log(f"Phone stream UDP receiver listening on 0.0.0.0:{self.port}")

        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind(("0.0.0.0", self.port))
        self.tcp_sock.listen()
        self.tcp_sock.settimeout(1.0)
        self._tcp_thread = threading.Thread(target=self._tcp_loop, daemon=True)
        self._tcp_thread.start()
        self.log(f"Phone stream TCP receiver listening on 0.0.0.0:{self.port}")

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.tcp_sock.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._tcp_thread.is_alive():
            self._tcp_thread.join(timeout=1.0)

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def _update_stats(self, **changes):
        with self._stats_lock:
            self._stats.update(changes)

    def _increment_stat(self, key: str, amount: int = 1):
        with self._stats_lock:
            self._stats[key] = int(self._stats.get(key, 0)) + amount

    def _loop(self):
        while self._running:
            try:
                packet, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                self._cleanup_stale()
                continue
            except OSError:
                break

            now = time.time()
            self._increment_stat("packets")
            self._update_stats(last_packet_time=now, last_source_ip=addr[0])
            parsed = self._parse_packet(packet)
            if parsed is None:
                self._increment_stat("invalid_packets")
                self._update_stats(last_invalid_size=len(packet))
                if now - self._last_invalid_log >= 3.0:
                    self._last_invalid_log = now
                    self.log(
                        "Phone stream received UDP packets that do not match "
                        f"the expected preview format. Last source={addr[0]}, bytes={len(packet)}"
                    )
                continue

            source_ip = addr[0]
            self._increment_stat("valid_packets")
            self._update_stats(
                last_valid_packet_time=now,
                last_valid_source_ip=source_ip,
                last_header_endian=parsed.get("header_endian", ""),
            )
            if parsed.get("header_endian") == "little" and now - self._last_little_endian_log >= 5.0:
                self._last_little_endian_log = now
                self.log("Phone stream detected little-endian preview headers; accepting them.")
            self._handle_packet(source_ip, parsed)

    def _tcp_loop(self):
        while self._running:
            try:
                conn, addr = self.tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._increment_stat("tcp_connections")
            threading.Thread(
                target=self._tcp_client_loop,
                args=(conn, addr),
                daemon=True,
            ).start()

    def _tcp_client_loop(self, conn: socket.socket, addr):
        source_id = addr[0]
        try:
            conn.settimeout(5.0)
            hello = self._read_tcp_line(conn, max_bytes=512)
            if hello.startswith("STREAM "):
                requested_source = hello[7:].strip()
                if requested_source:
                    source_id = requested_source
            self.log(f"Phone stream TCP client connected from {addr[0]} as {source_id}")

            while self._running:
                header = self._read_exact(conn, 4)
                if header is None:
                    break
                packet_size = struct.unpack("!I", header)[0]
                if packet_size <= 0 or packet_size > 64 * 1024 * 1024:
                    self.log(f"Phone stream TCP invalid packet size from {source_id}: {packet_size}")
                    break
                packet = self._read_exact(conn, packet_size)
                if packet is None:
                    break

                now = time.time()
                self._increment_stat("packets")
                self._update_stats(last_packet_time=now, last_source_ip=source_id)
                parsed = self._parse_packet(packet)
                if parsed is None:
                    self._increment_stat("invalid_packets")
                    self._update_stats(last_invalid_size=len(packet))
                    if now - self._last_invalid_log >= 3.0:
                        self._last_invalid_log = now
                        self.log(
                            "Phone stream received TCP packets that do not match "
                            f"the expected preview format. Last source={source_id}, bytes={len(packet)}"
                        )
                    continue

                self._increment_stat("valid_packets")
                self._update_stats(
                    last_valid_packet_time=now,
                    last_valid_source_ip=source_id,
                    last_header_endian=parsed.get("header_endian", ""),
                )
                self._handle_packet(source_id, parsed)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_tcp_line(conn: socket.socket, max_bytes: int) -> str:
        data = bytearray()
        while len(data) < max_bytes:
            chunk = conn.recv(1)
            if not chunk:
                break
            if chunk == b"\n":
                break
            data.extend(chunk)
        return data.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _read_exact(conn: socket.socket, size: int) -> Optional[bytes]:
        data = bytearray()
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _handle_packet(self, source_ip: str, parsed: dict):
        frame_id = parsed["frame_id"]
        assembly = self._assemblies.get(source_ip)

        if assembly is None or frame_id > assembly["frame_id"]:
            assembly = self._new_assembly(parsed)
            self._assemblies[source_ip] = assembly
        elif frame_id < assembly["frame_id"]:
            return
        elif (
            parsed["chunk_count"] != assembly["chunk_count"]
            or parsed["jpeg_size"] != assembly["jpeg_size"]
        ):
            assembly = self._new_assembly(parsed)
            self._assemblies[source_ip] = assembly

        chunk_index = parsed["chunk_index"]
        if chunk_index >= assembly["chunk_count"]:
            return

        assembly["last_update"] = time.time()
        assembly["chunks"][chunk_index] = parsed["payload"]

        if len(assembly["chunks"]) < assembly["chunk_count"]:
            return

        try:
            payload = b"".join(
                assembly["chunks"][index] for index in range(assembly["chunk_count"])
            )
        except KeyError:
            return

        payload = payload[: assembly["jpeg_size"]]
        meta = {
            "frame_id": assembly["frame_id"],
            "width": assembly["width"],
            "height": assembly["height"],
            "timestamp_ms": assembly["timestamp_ms"],
            "rotation_quarter_turns": assembly["flags"] & 0x03,
        }
        self._assemblies.pop(source_ip, None)
        self._increment_stat("completed_frames")
        self._update_stats(last_frame_time=time.time())
        self.on_frame(source_ip, payload, meta)

    @staticmethod
    def _new_assembly(parsed: dict) -> dict:
        return {
            "frame_id": parsed["frame_id"],
            "chunk_count": parsed["chunk_count"],
            "width": parsed["width"],
            "height": parsed["height"],
            "jpeg_size": parsed["jpeg_size"],
            "timestamp_ms": parsed["timestamp_ms"],
            "flags": parsed["flags"],
            "chunks": {},
            "last_update": time.time(),
        }

    @staticmethod
    def _parse_packet(packet: bytes) -> Optional[dict]:
        if len(packet) < UDP_HEADER_BE.size:
            return None

        for endian_name, header in (("big", UDP_HEADER_BE), ("little", UDP_HEADER_LE)):
            try:
                (
                    magic,
                    version,
                    flags,
                    frame_id,
                    chunk_index,
                    chunk_count,
                    width,
                    height,
                    jpeg_size,
                    timestamp_ms,
                ) = header.unpack(packet[: header.size])
            except struct.error:
                continue

            if magic not in PHONE_PREVIEW_MAGICS or version != UDP_VERSION:
                continue
            if not UdpPhoneStreamReceiver._header_values_plausible(
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                width=width,
                height=height,
                jpeg_size=jpeg_size,
            ):
                continue

            return {
                "flags": flags,
                "frame_id": frame_id,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
                "width": width,
                "height": height,
                "jpeg_size": jpeg_size,
                "timestamp_ms": timestamp_ms,
                "payload": packet[header.size :],
                "header_endian": endian_name,
            }

        return None

    @staticmethod
    def _header_values_plausible(
        chunk_index: int,
        chunk_count: int,
        width: int,
        height: int,
        jpeg_size: int,
    ) -> bool:
        return (
            0 <= chunk_index < chunk_count <= 4096
            and 0 < width <= 8192
            and 0 < height <= 8192
            and 0 < jpeg_size <= 64 * 1024 * 1024
        )

    def _cleanup_stale(self):
        cutoff = time.time() - 1.0
        stale_sources = [
            source_ip
            for source_ip, assembly in self._assemblies.items()
            if assembly["last_update"] < cutoff
        ]
        for source_ip in stale_sources:
            self._assemblies.pop(source_ip, None)
        if stale_sources:
            self._increment_stat("stale_frames", len(stale_sources))
            now = time.time()
            if now - self._last_stale_log >= 3.0:
                self._last_stale_log = now
                self.log(
                    "Phone stream is receiving preview chunks, but some frames "
                    f"are incomplete. Dropped stale assemblies from: {', '.join(stale_sources)}"
                )


class PhoneStreamPanel:
    def __init__(self, root, parent, stream_key: str, client_label: str):
        self.root = root
        self.stream_key = stream_key
        self.client_label = client_label
        self.frame = tk.LabelFrame(parent, text=client_label, padx=6, pady=6)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.image_label = tk.Label(
            self.frame,
            text="Waiting for stream frames...",
            anchor="center",
            justify=tk.CENTER,
            bg="black",
            fg="white",
        )
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(
            self.frame,
            textvariable=self.status_var,
            anchor="w",
            justify=tk.LEFT,
            wraplength=720,
        )
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._photo_image = None
        self._current_image = None
        self._stream_state_text = "Idle"
        self._last_display_time = None
        self._smoothed_display_fps = None
        self._resize_after_id = None
        self._frame_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._pending_frame = None
        self._stop_event = threading.Event()
        self._pil_image_module = None
        self._pil_image_tk_module = None
        self._resampling_filter = None
        self.image_label.bind("<Configure>", self._on_image_label_configure, add="+")
        self.frame.bind("<Configure>", self._on_image_label_configure, add="+")
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def destroy(self):
        self._stop_event.set()
        self._frame_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)
        try:
            self.frame.destroy()
        except tk.TclError:
            pass

    def set_stream_state(self, text: str):
        self._stream_state_text = text
        if self.frame.winfo_exists() and not self._pending_frame:
            self.status_var.set(text)

    def submit_frame(self, jpeg_bytes: bytes, meta: dict):
        with self._frame_lock:
            self._pending_frame = (jpeg_bytes, meta)
        self._frame_event.set()

    def _worker_loop(self):
        while not self._stop_event.is_set():
            self._frame_event.wait(timeout=0.5)
            self._frame_event.clear()
            if self._stop_event.is_set():
                return

            with self._frame_lock:
                item = self._pending_frame
                self._pending_frame = None

            if item is None:
                continue

            jpeg_bytes, meta = item
            rendered_image = None
            status_text = self._stream_state_text
            try:
                if self._pil_image_module is None:
                    self._pil_image_module = importlib.import_module("PIL.Image")
                pil_image_module = self._pil_image_module
                pil_image = pil_image_module.open(io.BytesIO(jpeg_bytes))
                pil_image.load()
                pil_image = pil_image.convert("RGB")
                rendered_image = self._rotate_image(
                    pil_image_module,
                    pil_image,
                    meta.get("rotation_quarter_turns", 0),
                )
                fps_text = self._record_and_format_fps()
                status_text = (
                    f"{self._stream_state_text} | "
                    f"{meta['width']}x{meta['height']} | {fps_text}"
                )
            except ModuleNotFoundError as exc:
                status_text = f"{self._stream_state_text} | Missing dependency: {exc.name}"
            except Exception as exc:
                status_text = f"{self._stream_state_text} | Preview decode failed: {exc}"

            try:
                self.root.after(
                    0,
                    lambda image=rendered_image, text=status_text: self._apply_render_result(
                        image,
                        text,
                    ),
                )
            except tk.TclError:
                return

    def _apply_render_result(self, image, status_text: str):
        if not self.frame.winfo_exists():
            return
        self.status_var.set(status_text)
        if image is None:
            self.image_label.configure(image="", text="No frame available")
            self._photo_image = None
            self._current_image = None
            return

        self._current_image = image.copy()
        self._refresh_image_display()

    def _on_image_label_configure(self, _event=None):
        self._update_status_wraplength()
        if self._current_image is None or not self.frame.winfo_exists():
            return
        if self._resize_after_id is not None:
            try:
                self.frame.after_cancel(self._resize_after_id)
            except tk.TclError:
                pass
        self._resize_after_id = self.frame.after(20, self._refresh_image_display)

    def _update_status_wraplength(self):
        wraplength = max(self.frame.winfo_width() - 24, 260)
        self.status_label.configure(wraplength=wraplength)

    def _record_and_format_fps(self) -> str:
        now = time.perf_counter()
        previous = self._last_display_time
        self._last_display_time = now
        if previous is None:
            self._smoothed_display_fps = None
            return "fps --"

        elapsed = now - previous
        if elapsed <= 0:
            return "fps --"
        instant_fps = 1.0 / elapsed
        if elapsed > 2.0 or self._smoothed_display_fps is None:
            self._smoothed_display_fps = instant_fps
        else:
            self._smoothed_display_fps = (0.35 * instant_fps) + (
                0.65 * self._smoothed_display_fps
            )
        return f"{self._smoothed_display_fps:.1f} fps"

    @staticmethod
    def _rotate_image(pil_image_module, pil_image, rotation_quarter_turns: int):
        rotation = int(rotation_quarter_turns or 0) % 4
        if rotation == 0:
            return pil_image

        transpose_module = getattr(pil_image_module, "Transpose", pil_image_module)
        if rotation == 1:
            return pil_image.transpose(getattr(transpose_module, "ROTATE_270"))
        if rotation == 2:
            return pil_image.transpose(getattr(transpose_module, "ROTATE_180"))
        return pil_image.transpose(getattr(transpose_module, "ROTATE_90"))

    def _refresh_image_display(self):
        self._resize_after_id = None
        if self._current_image is None or not self.frame.winfo_exists():
            return

        if self._pil_image_tk_module is None:
            self._pil_image_tk_module = importlib.import_module("PIL.ImageTk")
        if self._pil_image_module is None:
            self._pil_image_module = importlib.import_module("PIL.Image")
        image_tk_module = self._pil_image_tk_module
        pil_image_module = self._pil_image_module
        display_image = self._current_image.copy()

        available_width = self.image_label.winfo_width()
        available_height = self.image_label.winfo_height()
        if available_width <= 1 or available_height <= 1:
            available_width, available_height = 720, 480

        if self._resampling_filter is None:
            self._resampling_filter = getattr(
                getattr(pil_image_module, "Resampling", pil_image_module),
                "LANCZOS",
            )
        display_image.thumbnail((available_width, available_height), self._resampling_filter)
        self._photo_image = image_tk_module.PhotoImage(display_image)
        self.image_label.configure(image=self._photo_image, text="")


class PhoneStreamPane:
    def __init__(
        self,
        root,
        parent,
        config: PhoneStreamConfig,
        clients_getter: Callable[[], list],
        client_label_getter: Callable[[dict], str],
        start_stream_callback: Callable[[dict], None],
        stop_stream_callback: Callable[[dict], None],
        log_callback: Callable[[str], None],
    ):
        self.root = root
        self.config = config
        self.get_clients = clients_getter
        self.get_client_label = client_label_getter
        self.start_stream_callback = start_stream_callback
        self.stop_stream_callback = stop_stream_callback
        self.log = log_callback
        self.clients_by_key: Dict[str, dict] = {}
        self.client_vars: Dict[str, tk.BooleanVar] = {}
        self.client_state_labels: Dict[str, tk.Label] = {}
        self.stream_states: Dict[str, str] = {}
        self.panels: Dict[str, PhoneStreamPanel] = {}
        self.closed = False
        self.display_suspended = False
        self._status_after_id = None
        self._last_status_log = 0.0
        self._last_unknown_source_log = 0.0
        self._last_missing_panel_log = 0.0

        self.frame = tk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        stream_selector = tk.LabelFrame(self.frame, text="Streams", padx=6, pady=6)
        stream_selector.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        stream_selector.columnconfigure(0, weight=1)
        self.clients_frame = tk.Frame(stream_selector)
        self.clients_frame.grid(row=0, column=0, sticky="ew")

        preview_frame = tk.LabelFrame(self.frame, text="Live Preview", padx=6, pady=6)
        preview_frame.grid(row=1, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.previews_frame = tk.Frame(preview_frame)
        self.previews_frame.grid(row=0, column=0, sticky="nsew")

        self.receiver = UdpPhoneStreamReceiver(
            port=self.config.udp_port,
            socket_buffer_bytes=self.config.socket_buffer_bytes,
            on_frame=self._on_frame,
            log_callback=self.log,
        )
        self.update_clients(self.get_clients())
        self._poll_receiver_status()

    def close(self):
        if self.closed:
            return
        self.closed = True

        for client_key, selected_var in list(self.client_vars.items()):
            if selected_var.get():
                client = self.clients_by_key.get(client_key)
                if client is not None:
                    self.stop_stream_callback(client)

        for panel in list(self.panels.values()):
            panel.destroy()
        self.panels.clear()

        if self._status_after_id is not None:
            try:
                self.frame.after_cancel(self._status_after_id)
            except tk.TclError:
                pass
            self._status_after_id = None

        self.receiver.close()
        try:
            self.frame.destroy()
        except tk.TclError:
            pass

    def update_clients(self, clients: list):
        if self.closed:
            return

        next_clients_by_key = {client["key"]: client for client in clients}
        for client_key in list(self.clients_by_key.keys()):
            if client_key not in next_clients_by_key:
                selected_var = self.client_vars.get(client_key)
                if selected_var is not None:
                    selected_var.set(False)
                panel = self.panels.pop(client_key, None)
                if panel is not None:
                    panel.destroy()
                self.stream_states.pop(client_key, None)
                self.client_vars.pop(client_key, None)

        self.clients_by_key = next_clients_by_key
        self._rebuild_clients_ui()
        self._rebuild_preview_grid()

    def set_stream_state(self, client_key: str, text: str):
        self.stream_states[client_key] = text
        label = self.client_state_labels.get(client_key)
        if label is not None:
            label.configure(text=text)
        panel = self.panels.get(client_key)
        if panel is not None:
            panel.set_stream_state(text)

    def set_display_suspended(self, suspended: bool):
        self.display_suspended = bool(suspended)
        state = "Preview paused for lag test" if self.display_suspended else "Preview resumed"
        for client_key in self._selected_client_keys():
            self.set_stream_state(client_key, state)

    def _rebuild_clients_ui(self):
        for widget in self.clients_frame.winfo_children():
            widget.destroy()
        self.client_state_labels.clear()

        if not self.clients_by_key:
            tk.Label(self.clients_frame, text="No phone connected").grid(row=0, column=0, sticky="w")
            return

        ordered_clients = list(self.clients_by_key.values())
        for row, client in enumerate(ordered_clients):
            client_key = client["key"]
            current_var = self.client_vars.get(client_key)
            if current_var is None:
                current_var = tk.BooleanVar(value=False)
                self.client_vars[client_key] = current_var

            column = row % 2
            item_row = row // 2
            item = tk.Frame(self.clients_frame)
            item.grid(row=item_row, column=column, sticky="ew", padx=(0, 10), pady=2)
            item.columnconfigure(1, weight=1)
            self.clients_frame.columnconfigure(column, weight=1)

            checkbox = tk.Checkbutton(
                item,
                text=self.get_client_label(client),
                variable=current_var,
                command=lambda key=client_key: self._on_toggle_client(key),
                anchor="w",
                justify=tk.LEFT,
            )
            checkbox.grid(row=0, column=0, sticky="w")

            state_text = self.stream_states.get(client_key, "Idle")
            state_label = tk.Label(
                item,
                text=state_text,
                anchor="w",
                justify=tk.LEFT,
                wraplength=220,
            )
            state_label.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.client_state_labels[client_key] = state_label

    def _rebuild_preview_grid(self):
        selected_keys = self._selected_client_keys()

        panel_frames = {panel.frame for panel in self.panels.values()}
        for child in list(self.previews_frame.winfo_children()):
            if child not in panel_frames:
                child.destroy()

        for client_key in list(self.panels.keys()):
            if client_key not in selected_keys:
                self.panels.pop(client_key).destroy()

        for client_key in selected_keys:
            if client_key not in self.panels:
                panel = PhoneStreamPanel(
                    root=self.root,
                    parent=self.previews_frame,
                    stream_key=client_key,
                    client_label=self.get_client_label(self.clients_by_key[client_key]),
                )
                panel.set_stream_state(self.stream_states.get(client_key, "Waiting for stream"))
                self.panels[client_key] = panel

        for child in self.previews_frame.winfo_children():
            child.grid_forget()

        if not selected_keys:
            self.previews_frame.rowconfigure(0, weight=1)
            self.previews_frame.columnconfigure(0, weight=1)
            tk.Label(
                self.previews_frame,
                text="Select one or more phones above to start streaming.",
                anchor="center",
                justify=tk.CENTER,
            ).grid(row=0, column=0, sticky="nsew")
            return

        columns = 1 if len(selected_keys) == 1 else 2
        rows = (len(selected_keys) + columns - 1) // columns
        for column in range(columns):
            self.previews_frame.columnconfigure(column, weight=1)
        for row in range(rows):
            self.previews_frame.rowconfigure(row, weight=1)

        for index, client_key in enumerate(selected_keys):
            row = index // columns
            column = index % columns
            panel = self.panels[client_key]
            panel.frame.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)

    def _on_toggle_client(self, client_key: str):
        client = self.clients_by_key.get(client_key)
        if client is None:
            return

        selected = self.client_vars[client_key].get()
        if selected:
            self.set_stream_state(client_key, "Requesting stream...")
            self.start_stream_callback(client)
        else:
            self.set_stream_state(client_key, "Stopping stream...")
            self.stop_stream_callback(client)
        self._rebuild_preview_grid()

    def _selected_client_keys(self):
        return [
            client_key
            for client_key, selected_var in self.client_vars.items()
            if selected_var.get() and client_key in self.clients_by_key
        ]

    def _poll_receiver_status(self):
        if self.closed:
            return

        selected_keys = self._selected_client_keys()
        if selected_keys:
            stats = self.receiver.get_stats()
            now = time.time()
            packet_age = now - stats["last_packet_time"] if stats["last_packet_time"] else None
            frame_age = now - stats["last_frame_time"] if stats["last_frame_time"] else None

            if stats["packets"] == 0:
                detail = "No preview packets have reached this PC yet"
            elif stats["valid_packets"] == 0:
                detail = (
                    "Preview packets are arriving, but not in the expected preview format "
                    f"(last from {stats['last_source_ip']}, {stats['last_invalid_size']} bytes)"
                )
            elif stats["completed_frames"] == 0:
                detail = (
                    "Preview chunks are arriving, but no complete JPEG frame has assembled "
                    f"yet (last from {stats['last_valid_source_ip']})"
                )
            else:
                detail = (
                    f"Preview ok: {stats['completed_frames']} frames, "
                    f"last from {stats['last_valid_source_ip']}"
                )

            if packet_age is not None and packet_age > 3.0:
                detail += f"; last packet {packet_age:.1f}s ago"
            if frame_age is not None and frame_age > 3.0:
                detail += f"; last frame {frame_age:.1f}s ago"

            for client_key in selected_keys:
                panel = self.panels.get(client_key)
                if panel is not None and panel._current_image is None:
                    panel.set_stream_state(
                        f"{self.stream_states.get(client_key, 'Waiting for stream')} | {detail}"
                    )

            if now - self._last_status_log >= 5.0 and stats["completed_frames"] == 0:
                self._last_status_log = now
                self.log(f"Phone stream diagnostic: {detail}")

        self._status_after_id = self.frame.after(1000, self._poll_receiver_status)

    def _on_frame(self, source_id: str, jpeg_bytes: bytes, meta: dict):
        if self.closed or self.display_suspended:
            return
        try:
            self.root.after(
                0,
                lambda source_id=source_id, jpeg_bytes=jpeg_bytes, meta=meta: (
                    self._dispatch_frame(source_id, jpeg_bytes, meta)
                ),
            )
        except tk.TclError:
            pass

    def _dispatch_frame(self, source_id: str, jpeg_bytes: bytes, meta: dict):
        if self.closed:
            return

        client = next(
            (
                client
                for client in self.clients_by_key.values()
                if client.get("key") == source_id
                or client.get("addr", ("", 0))[0] == source_id
            ),
            None,
        )
        if client is None:
            now = time.time()
            if now - self._last_unknown_source_log >= 3.0:
                self._last_unknown_source_log = now
                known_sources = ", ".join(
                    sorted(
                        {
                            client.get("addr", ("", 0))[0]
                            for client in self.clients_by_key.values()
                            if client.get("addr")
                        }
                    )
                )
                self.log(
                    "Phone stream received a complete frame from an unknown source "
                    f"{source_id}. Connected phone sources: {known_sources or 'none'}"
                )
            return

        panel = self.panels.get(client["key"])
        if panel is None:
            now = time.time()
            if now - self._last_missing_panel_log >= 3.0:
                self._last_missing_panel_log = now
                self.log(
                    "Phone stream received a frame, but that phone is not enabled: "
                    f"{source_id}"
                )
            return
        panel.submit_frame(jpeg_bytes, meta)
