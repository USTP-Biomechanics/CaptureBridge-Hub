import time
import tkinter as tk
import sys
from ctypes import Structure, WinDLL, byref, c_short, c_wchar, sizeof
from ctypes.wintypes import DWORD, LONG, WORD

from .marker_timecode import (
    ARUCO_MAX_ID,
    ARUCO_MODULES,
    MARKER_STEP_MS,
    encode_marker_time,
    marker_matrix,
    prewarm_marker_matrices,
)


DEFAULT_REFRESH_HZ = 60.0
MIN_REFRESH_HZ = 24.0
MAX_REFRESH_HZ = 360.0


class _PointL(Structure):
    _fields_ = [
        ("x", LONG),
        ("y", LONG),
    ]


class _DevMode(Structure):
    _fields_ = [
        ("dmDeviceName", c_wchar * 32),
        ("dmSpecVersion", WORD),
        ("dmDriverVersion", WORD),
        ("dmSize", WORD),
        ("dmDriverExtra", WORD),
        ("dmFields", DWORD),
        ("dmPosition", _PointL),
        ("dmDisplayOrientation", DWORD),
        ("dmDisplayFixedOutput", DWORD),
        ("dmColor", c_short),
        ("dmDuplex", c_short),
        ("dmYResolution", c_short),
        ("dmTTOption", c_short),
        ("dmCollate", c_short),
        ("dmFormName", c_wchar * 32),
        ("dmLogPixels", WORD),
        ("dmBitsPerPel", DWORD),
        ("dmPelsWidth", DWORD),
        ("dmPelsHeight", DWORD),
        ("dmDisplayFlags", DWORD),
        ("dmDisplayFrequency", DWORD),
        ("dmICMMethod", DWORD),
        ("dmICMIntent", DWORD),
        ("dmMediaType", DWORD),
        ("dmDitherType", DWORD),
        ("dmReserved1", DWORD),
        ("dmReserved2", DWORD),
        ("dmPanningWidth", DWORD),
        ("dmPanningHeight", DWORD),
    ]


def _detect_display_refresh_hz() -> float:
    if sys.platform != "win32":
        return DEFAULT_REFRESH_HZ
    try:
        user32 = WinDLL("user32", use_last_error=True)
        mode = _DevMode()
        mode.dmSize = WORD(sizeof(_DevMode))
        if not user32.EnumDisplaySettingsW(None, DWORD(-1).value, byref(mode)):
            return DEFAULT_REFRESH_HZ
        hz = float(mode.dmDisplayFrequency)
        if hz < MIN_REFRESH_HZ or hz > MAX_REFRESH_HZ:
            return DEFAULT_REFRESH_HZ
        return hz
    except Exception:
        return DEFAULT_REFRESH_HZ


class LagTimingDisplay:
    """Left-side timing target with a human clock and ArUco marker timecode."""

    def __init__(self, root: tk.Tk, left_fraction: float = 0.5):
        self.root = root
        self.left_fraction = max(0.25, min(float(left_fraction), 1.0))
        self.window = None
        self.canvas = None
        self.start_perf = None
        self._after_id = None
        self._phase = "READY"
        self._marker_cells = []
        self._last_marker_id = None
        self._last_clock_text = None
        self._last_phase = None
        self.refresh_hz = DEFAULT_REFRESH_HZ
        self.tick_interval_ms = 1000.0 / DEFAULT_REFRESH_HZ

    def show(self):
        if self.window is not None:
            return
        self.refresh_hz = _detect_display_refresh_hz()
        self.tick_interval_ms = 1000.0 / self.refresh_hz
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
        except tk.TclError as exc:
            raise RuntimeError(f"Could not query display size: {exc}") from exc
        width = int(screen_w * self.left_fraction)
        height = screen_h

        window = None
        try:
            window = tk.Toplevel(self.root)
            window.title("CaptureBridge Lag Test")
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.geometry(f"{width}x{height}+0+0")
            window.configure(background="black")
            window.bind("<Escape>", lambda _event: self.close())
            window.protocol("WM_DELETE_WINDOW", self.close)

            canvas = tk.Canvas(window, width=width, height=height, highlightthickness=0, bg="black")
            canvas.pack(fill=tk.BOTH, expand=True)
            window.update_idletasks()
            window.lift()
        except tk.TclError as exc:
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    pass
            raise RuntimeError(f"Could not open lag-test display: {exc}") from exc

        self.window = window
        self.canvas = canvas
        prewarm_marker_matrices(ARUCO_MAX_ID)
        self.start_perf = time.perf_counter()
        self._draw_static()
        self._tick()

    def elapsed_ms(self) -> float:
        if self.start_perf is None:
            return 0.0
        return (time.perf_counter() - self.start_perf) * 1000.0

    def set_phase(self, phase: str):
        self._phase = str(phase or "").upper()[:24] or "READY"

    def close(self):
        if self.window is None:
            return
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None
        self.canvas = None
        self._marker_cells = []
        self._last_marker_id = None
        self._last_clock_text = None
        self._last_phase = None

    def _draw_static(self):
        if self.canvas is None:
            return
        self.canvas.delete("all")
        width = max(1, int(self.canvas.winfo_reqwidth()))
        height = max(1, int(self.canvas.winfo_reqheight()))

        self.canvas.create_rectangle(0, 0, width, height, fill="black", outline="")
        border = max(8, width // 140)
        self.canvas.create_rectangle(
            border,
            border,
            width - border,
            height - border,
            outline="#00ff66",
            width=border,
        )

        marker_side = max(96, int(min(width * 0.34, height * 0.30)))
        center_y = int(height * 0.225)
        gap = max(24, int(width * 0.075))
        total_width = marker_side * 2 + gap
        left_center_x = max(marker_side // 2 + border * 3, (width - total_width) // 2 + marker_side // 2)
        right_center_x = min(width - marker_side // 2 - border * 3, left_center_x + marker_side + gap)
        self._marker_cells = []
        for center_x in (left_center_x, right_center_x):
            self._marker_cells.append(self._draw_marker_panel(center_x, center_y, marker_side))

        self.canvas.create_text(
            width // 2,
            int(height * 0.48),
            text="CAPTUREBRIDGE LAG TEST",
            fill="white",
            font=("Consolas", max(24, height // 24), "bold"),
            tags=("title",),
        )
        self.canvas.create_text(
            width // 2,
            int(height * 0.62),
            text="000000 ms",
            fill="#ffffff",
            font=("Consolas", max(60, height // 8), "bold"),
            tags=("clock",),
        )
        self.canvas.create_text(
            width // 2,
            int(height * 0.77),
            text="READY",
            fill="#00ff66",
            font=("Consolas", max(42, height // 11), "bold"),
            tags=("phase",),
        )
        self.canvas.create_text(
            width // 2,
            int(height * 0.86),
            text="Keep this target inside the phone video frame",
            fill="#dddddd",
            font=("Segoe UI", max(18, height // 34), "bold"),
            tags=("hint",),
        )
        self._last_marker_id = None

    def _draw_marker_panel(self, center_x: int, center_y: int, marker_side: int):
        if self.canvas is None:
            return []
        half = marker_side // 2
        x0 = int(center_x - half)
        y0 = int(center_y - half)
        x1 = x0 + marker_side
        y1 = y0 + marker_side
        quiet = max(14, marker_side // 9)
        self.canvas.create_rectangle(
            x0 - quiet,
            y0 - quiet,
            x1 + quiet,
            y1 + quiet,
            fill="#ffffff",
            outline="#bdbdbd",
            width=max(2, quiet // 7),
        )
        cells = []
        for row in range(ARUCO_MODULES):
            row_cells = []
            for col in range(ARUCO_MODULES):
                cx0 = x0 + int(col * marker_side / ARUCO_MODULES)
                cy0 = y0 + int(row * marker_side / ARUCO_MODULES)
                cx1 = x0 + int((col + 1) * marker_side / ARUCO_MODULES)
                cy1 = y0 + int((row + 1) * marker_side / ARUCO_MODULES)
                rect = self.canvas.create_rectangle(
                    cx0,
                    cy0,
                    cx1,
                    cy1,
                    fill="#000000",
                    outline="#000000",
                )
                row_cells.append(rect)
            cells.append(row_cells)
        return cells

    def _update_marker_panels(self, marker_id: int):
        if self.canvas is None or marker_id == self._last_marker_id:
            return
        try:
            matrix = marker_matrix(marker_id)
            for panel in self._marker_cells:
                for row_index, row_cells in enumerate(panel):
                    for col_index, rect in enumerate(row_cells):
                        fill = "#ffffff" if matrix[row_index][col_index] else "#000000"
                        self.canvas.itemconfigure(rect, fill=fill, outline=fill)
            self._last_marker_id = marker_id
        except tk.TclError:
            self.close()

    def _tick(self):
        if self.window is None or self.canvas is None:
            return
        try:
            marker_id, elapsed_int = encode_marker_time(self.elapsed_ms())
            self._update_marker_panels(marker_id)

            clock_text = f"{elapsed_int:06d} ms"
            if clock_text != self._last_clock_text:
                self.canvas.itemconfigure("clock", text=clock_text)
                self._last_clock_text = clock_text

            phase_color = {
                "READY": "#00ff66",
                "ARMED": "#ffd54a",
                "START": "#00e5ff",
                "STOP": "#ff5252",
                "TRANSFER": "#b388ff",
                "ANALYZING": "#ffb74d",
                "DONE": "#00ff66",
                "ERROR": "#ff5252",
            }.get(self._phase, "#00ff66")
            if self._phase != self._last_phase:
                self.canvas.itemconfigure("phase", text=self._phase, fill=phase_color)
                self._last_phase = self._phase
            self._after_id = self.window.after(self._next_tick_delay_ms(), self._tick)
        except tk.TclError:
            self.close()

    def _next_tick_delay_ms(self) -> int:
        elapsed = self.elapsed_ms()
        next_frame_ms = (int(elapsed // self.tick_interval_ms) + 1) * self.tick_interval_ms
        return max(1, int(round(next_frame_ms - elapsed)))
