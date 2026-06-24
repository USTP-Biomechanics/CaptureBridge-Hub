from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Optional, Sequence, Tuple

from .marker_timecode import decode_marker_time, get_aruco_dictionary


@dataclass
class LagTiming:
    label: str
    display_started_perf: float
    start_command_elapsed_ms: float
    stop_command_elapsed_ms: float
    start_ack_elapsed_ms: Optional[float] = None
    stop_ack_elapsed_ms: Optional[float] = None


@dataclass
class LagAnalysisResult:
    video_path: str
    decoded_frame_count: int
    total_frame_count: int
    fps: float
    first_frame_index: Optional[int]
    last_frame_index: Optional[int]
    first_decoded_frame_index: Optional[int]
    last_decoded_frame_index: Optional[int]
    first_frame_elapsed_ms: Optional[int]
    last_frame_elapsed_ms: Optional[int]
    first_frame_confidence: Optional[float]
    last_frame_confidence: Optional[float]
    first_frame_clean: bool
    last_frame_clean: bool
    first_frame_elapsed_predicted: bool
    last_frame_elapsed_predicted: bool
    first_frame_image_path: Optional[str]
    last_frame_image_path: Optional[str]
    start_lag_ms: Optional[float]
    stop_lag_ms: Optional[float]
    start_ack_latency_ms: Optional[float]
    stop_ack_latency_ms: Optional[float]
    confidence: float
    error: Optional[str] = None


SYNC_BITS = (1, 0, 1, 0)
TIME_BITS = 20
TOTAL_BITS = len(SYNC_BITS) + TIME_BITS
CLEAN_CONFIDENCE_THRESHOLD = 0.40
CODE_ROWS = 3
ENDPOINT_SEARCH_MAX_FRAMES = 90


def decode_timecode_from_frame(frame, expected_ms: Optional[float] = None) -> Optional[Tuple[int, float]]:
    candidates = []
    target = _extract_timing_target_crop(frame)
    if target is not None:
        candidates.extend(_decode_aruco_candidates_from_image(target))
    candidates.extend(_decode_aruco_candidates_from_image(frame))
    if candidates:
        return _choose_candidate(candidates, expected_ms)

    if target is not None:
        candidates.extend(_decode_candidates_from_image(target))
    candidates.extend(_decode_candidates_from_image(frame))
    if not candidates:
        return None
    return _choose_candidate(candidates, expected_ms)


def _choose_candidate(
    candidates: Sequence[Tuple[int, float]],
    expected_ms: Optional[float] = None,
) -> Optional[Tuple[int, float]]:
    if not candidates:
        return None
    if expected_ms is not None:
        expected = float(expected_ms)
        plausible = [item for item in candidates if abs(float(item[0]) - expected) <= 10000.0]
        if plausible:
            return min(plausible, key=lambda item: (abs(float(item[0]) - expected), -item[1]))
        return min(candidates, key=lambda item: (abs(float(item[0]) - expected), -item[1]))
    return max(candidates, key=lambda item: item[1])


def analyze_lag_video(video_path: str, timing: LagTiming, sample_stride: int = 1) -> LagAnalysisResult:
    del sample_stride
    try:
        import cv2
    except Exception as exc:
        return _error_result(video_path, timing, f"OpenCV is required for lag analysis: {exc}")

    if not os.path.isfile(video_path):
        return _error_result(video_path, timing, "Video file not found")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return _error_result(video_path, timing, "Could not open video")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    first_frame = _read_frame_at(cap, 0)
    last_index = max(0, total_frames - 1)
    last_frame = _read_frame_at(cap, last_index)

    first_image_path = _write_endpoint_frame(video_path, "first", first_frame, cv2)
    last_image_path = _write_endpoint_frame(video_path, "last", last_frame, cv2)

    first_endpoint = _decode_endpoint_with_prediction(
        cap=cap,
        endpoint="first",
        endpoint_frame=first_frame,
        endpoint_index=0,
        total_frames=total_frames,
        fps=fps,
        expected_endpoint_ms=timing.start_command_elapsed_ms,
    )
    last_endpoint = _decode_endpoint_with_prediction(
        cap=cap,
        endpoint="last",
        endpoint_frame=last_frame,
        endpoint_index=last_index,
        total_frames=total_frames,
        fps=fps,
        expected_endpoint_ms=timing.stop_command_elapsed_ms,
    )
    cap.release()

    first_ms = first_endpoint.elapsed_ms
    last_ms = last_endpoint.elapsed_ms
    first_conf = first_endpoint.confidence
    last_conf = last_endpoint.confidence
    first_clean = first_endpoint.clean
    last_clean = last_endpoint.clean
    decoded_count = int(first_endpoint.decoded) + int(last_endpoint.decoded)
    confidence_values = [value for value in (first_conf, last_conf) if value is not None]
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0

    start_lag_ms = None if first_ms is None else float(first_ms) - float(timing.start_command_elapsed_ms)
    stop_lag_ms = None if last_ms is None else float(last_ms) - float(timing.stop_command_elapsed_ms)

    error_parts = []
    if first_frame is None:
        error_parts.append("Could not read first video frame")
    elif not first_endpoint.decoded:
        error_parts.append("First frame has no readable timecode")
    elif not first_clean:
        error_parts.append(f"First frame timecode confidence is low ({first_conf:.2f})")

    if last_frame is None:
        error_parts.append("Could not read last video frame")
    elif not last_endpoint.decoded:
        error_parts.append("Last frame has no readable timecode")
    elif not last_clean:
        error_parts.append(f"Last frame timecode confidence is low ({last_conf:.2f})")

    if not first_clean:
        start_lag_ms = None
    if not last_clean:
        stop_lag_ms = None

    return LagAnalysisResult(
        video_path=video_path,
        decoded_frame_count=decoded_count,
        total_frame_count=total_frames,
        fps=fps,
        first_frame_index=0 if first_frame is not None else None,
        last_frame_index=last_index if last_frame is not None else None,
        first_decoded_frame_index=first_endpoint.decoded_frame_index,
        last_decoded_frame_index=last_endpoint.decoded_frame_index,
        first_frame_elapsed_ms=first_ms,
        last_frame_elapsed_ms=last_ms,
        first_frame_confidence=first_conf,
        last_frame_confidence=last_conf,
        first_frame_clean=first_clean,
        last_frame_clean=last_clean,
        first_frame_elapsed_predicted=first_endpoint.predicted,
        last_frame_elapsed_predicted=last_endpoint.predicted,
        first_frame_image_path=first_image_path,
        last_frame_image_path=last_image_path,
        start_lag_ms=start_lag_ms,
        stop_lag_ms=stop_lag_ms,
        start_ack_latency_ms=_ack_latency(timing.start_ack_elapsed_ms, timing.start_command_elapsed_ms),
        stop_ack_latency_ms=_ack_latency(timing.stop_ack_elapsed_ms, timing.stop_command_elapsed_ms),
        confidence=confidence,
        error="; ".join(error_parts) if error_parts else None,
    )


@dataclass
class _EndpointDecode:
    decoded: bool
    clean: bool
    predicted: bool
    elapsed_ms: Optional[int]
    confidence: Optional[float]
    decoded_frame_index: Optional[int]


def _decode_endpoint_with_prediction(
    cap,
    endpoint: str,
    endpoint_frame,
    endpoint_index: int,
    total_frames: int,
    fps: float,
    expected_endpoint_ms: float,
) -> _EndpointDecode:
    endpoint_decoded = (
        decode_timecode_from_frame(endpoint_frame, expected_ms=expected_endpoint_ms)
        if endpoint_frame is not None
        else None
    )
    if endpoint_decoded is not None:
        elapsed_ms, confidence = endpoint_decoded
        clean = confidence >= CLEAN_CONFIDENCE_THRESHOLD
        if clean:
            return _EndpointDecode(True, True, False, int(elapsed_ms), confidence, endpoint_index)

    frame_ms = 1000.0 / fps if fps > 0.0 else None
    if frame_ms is not None and total_frames > 1:
        max_scan = min(ENDPOINT_SEARCH_MAX_FRAMES, total_frames - 1)
        for offset in range(1, max_scan + 1):
            if endpoint == "first":
                frame_index = offset
                expected_ms = expected_endpoint_ms + frame_ms * offset
            else:
                frame_index = endpoint_index - offset
                if frame_index < 0:
                    break
                expected_ms = expected_endpoint_ms - frame_ms * offset

            frame = _read_frame_at(cap, frame_index)
            decoded = decode_timecode_from_frame(frame, expected_ms=expected_ms) if frame is not None else None
            if decoded is None:
                continue
            elapsed_ms, confidence = decoded
            if confidence < CLEAN_CONFIDENCE_THRESHOLD:
                continue
            if endpoint == "first":
                endpoint_elapsed = int(round(float(elapsed_ms) - frame_ms * offset))
            else:
                endpoint_elapsed = int(round(float(elapsed_ms) + frame_ms * offset))
            return _EndpointDecode(True, True, True, endpoint_elapsed, confidence, frame_index)

    if endpoint_decoded is not None:
        elapsed_ms, confidence = endpoint_decoded
        return _EndpointDecode(True, False, False, int(elapsed_ms), confidence, endpoint_index)

    return _EndpointDecode(False, False, False, None, None, None)


def _read_frame_at(cap, frame_index: int):
    import cv2

    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
    ok, frame = cap.read()
    return frame if ok else None


def _write_endpoint_frame(video_path: str, name: str, frame, cv2_module) -> Optional[str]:
    if frame is None:
        return None
    path = os.path.splitext(video_path)[0] + f"_lag_{name}_frame.jpg"
    if cv2_module.imwrite(path, frame):
        return path
    return None


def _extract_timing_target_crop(frame):
    import cv2
    import numpy as np

    if frame is None or frame.size == 0 or frame.ndim != 3:
        return None
    height, width = frame.shape[:2]
    if height < 80 or width < 80:
        return None

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([35, 45, 45]), np.array([100, 255, 255]))
    kernel = np.ones((5, 5), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    components = []
    min_area = max(200, int(width * height * 0.00025))
    for index in range(1, count):
        x, y, w, h, area = [int(value) for value in stats[index]]
        if area >= min_area and (w >= width * 0.08 or h >= height * 0.08):
            components.append((x, y, w, h, area))
    if not components:
        return None

    x0 = min(x for x, _y, _w, _h, _area in components)
    y0 = min(y for _x, y, _w, _h, _area in components)
    x1 = max(x + w for x, _y, w, _h, _area in components)
    y1 = max(y + h for _x, y, _w, h, _area in components)
    pad_x = max(4, int((x1 - x0) * 0.015))
    pad_y = max(4, int((y1 - y0) * 0.015))
    x0 = max(0, x0 - pad_x)
    y0 = max(0, y0 - pad_y)
    x1 = min(width, x1 + pad_x)
    y1 = min(height, y1 + pad_y)

    if (x1 - x0) < width * 0.2 or (y1 - y0) < height * 0.2:
        return None
    return frame[y0:y1, x0:x1]


def _decode_aruco_candidates_from_image(frame) -> list[Tuple[int, float]]:
    import cv2
    import numpy as np

    if frame is None or frame.size == 0:
        return []
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        return []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    height, width = gray.shape[:2]
    if height < 80 or width < 80:
        return []

    dictionary = get_aruco_dictionary(cv2)
    parameters = aruco.DetectorParameters()
    if hasattr(aruco, "ArucoDetector"):
        detector = aruco.ArucoDetector(dictionary, parameters)
        corners, ids, _rejected = detector.detectMarkers(gray)
    else:
        corners, ids, _rejected = aruco.detectMarkers(gray, dictionary, parameters=parameters)
    if ids is None or len(ids) == 0:
        return []

    image_area = max(1.0, float(width * height))
    by_value: dict[int, list[float]] = {}
    for raw_id, corner in zip(np.asarray(ids).reshape(-1), corners):
        decoded_ms = decode_marker_time(int(raw_id))
        if decoded_ms is None:
            continue
        points = np.asarray(corner, dtype=np.float32).reshape(4, 2)
        area = abs(float(cv2.contourArea(points)))
        area_score = max(0.0, min(1.0, area / (image_area * 0.04)))
        confidence = max(0.70, min(1.0, 0.74 + area_score * 0.20))
        by_value.setdefault(decoded_ms, []).append(confidence)

    candidates = []
    for value, confidences in by_value.items():
        marker_agreement = min(1.0, len(confidences) / 2.0)
        confidence = min(1.0, (sum(confidences) / len(confidences)) * (0.75 + marker_agreement * 0.25))
        candidates.append((value, confidence))
    return candidates


def _decode_candidates_from_image(frame) -> list[Tuple[int, float]]:
    import cv2

    if frame is None or frame.size == 0:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    height, width = gray.shape[:2]
    if height < 40 or width < TOTAL_BITS * 6:
        return []

    candidates = []
    for band_top_frac, band_bottom_frac in ((0.045, 0.29), (0.04, 0.30), (0.05, 0.31)):
        candidate = _decode_repeated_rows(gray, band_top_frac, band_bottom_frac)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _decode_repeated_rows(
    gray,
    band_top_frac: float,
    band_bottom_frac: float,
) -> Optional[Tuple[int, float]]:
    height, _width = gray.shape[:2]
    band_top = int(height * band_top_frac)
    band_bottom = int(height * band_bottom_frac)
    band_height = band_bottom - band_top
    if band_height < CODE_ROWS * 8:
        return None

    row_gap = max(3, int(band_height * 0.06))
    row_height = max(4, int((band_height - row_gap * (CODE_ROWS + 1)) / CODE_ROWS))
    rows = []
    for row_index in range(CODE_ROWS):
        y0 = band_top + row_gap + row_index * (row_height + row_gap)
        y1 = min(band_bottom, y0 + row_height)
        decoded = _decode_row_with_alignment_search(gray, y0, y1)
        if decoded is not None:
            rows.append(decoded)

    if len(rows) < 2:
        return None

    by_value: dict[int, list[float]] = {}
    for value, confidence in rows:
        by_value.setdefault(int(value), []).append(float(confidence))

    value, confidences = max(
        by_value.items(),
        key=lambda item: (len(item[1]), sum(item[1]) / max(1, len(item[1]))),
    )
    if len(confidences) < 2:
        return None

    row_agreement = len(confidences) / CODE_ROWS
    row_confidence = sum(confidences) / len(confidences)
    confidence = max(0.0, min(1.0, row_confidence * row_agreement))
    return value, confidence


def _decode_row_with_alignment_search(gray, y0: int, y1: int) -> Optional[Tuple[int, float]]:
    candidates = []
    for left_frac in (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08):
        for right_frac in (0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 1.00):
            decoded = _decode_strip(gray, y0, y1, left_frac, right_frac)
            if decoded is not None:
                candidates.append(decoded)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])


def _decode_strip(gray, y0: int, y1: int, left_frac: float, right_frac: float) -> Optional[Tuple[int, float]]:
    import numpy as np

    _height, width = gray.shape[:2]
    x_start = int(width * left_frac)
    x_end = int(width * right_frac)
    strip_width = x_end - x_start
    if strip_width < TOTAL_BITS * 6:
        return None

    means = []
    for index in range(TOTAL_BITS):
        x0 = x_start + int(index * strip_width / TOTAL_BITS)
        x1 = max(x0 + 1, x_start + int((index + 1) * strip_width / TOTAL_BITS))
        pad_x = max(1, int((x1 - x0) * 0.20))
        pad_y = max(1, int((y1 - y0) * 0.20))
        cell = gray[y0 + pad_y : y1 - pad_y, x0 + pad_x : x1 - pad_x]
        means.append(float(np.mean(cell)) if cell.size else 0.0)

    low = min(means)
    high = max(means)
    spread = high - low
    if spread < 45.0:
        return None
    threshold = (low + high) * 0.5
    bits = [1 if value > threshold else 0 for value in means]
    if tuple(bits[: len(SYNC_BITS)]) != SYNC_BITS:
        return None

    value = 0
    for bit_index, bit in enumerate(bits[len(SYNC_BITS) :]):
        if bit:
            value |= 1 << bit_index

    sync_bright = [means[0], means[2]]
    sync_dark = [means[1], means[3]]
    sync_margin = min(sync_bright) - max(sync_dark)
    if sync_margin <= 0:
        return None
    confidence = max(0.0, min(1.0, min(spread, sync_margin) / 180.0))
    return value, confidence


def _ack_latency(ack_elapsed_ms: Optional[float], command_elapsed_ms: float) -> Optional[float]:
    if ack_elapsed_ms is None:
        return None
    return float(ack_elapsed_ms) - float(command_elapsed_ms)


def _error_result(video_path: str, timing: LagTiming, error: str) -> LagAnalysisResult:
    return LagAnalysisResult(
        video_path=video_path,
        decoded_frame_count=0,
        total_frame_count=0,
        fps=0.0,
        first_frame_index=None,
        last_frame_index=None,
        first_decoded_frame_index=None,
        last_decoded_frame_index=None,
        first_frame_elapsed_ms=None,
        last_frame_elapsed_ms=None,
        first_frame_confidence=None,
        last_frame_confidence=None,
        first_frame_clean=False,
        last_frame_clean=False,
        first_frame_elapsed_predicted=False,
        last_frame_elapsed_predicted=False,
        first_frame_image_path=None,
        last_frame_image_path=None,
        start_lag_ms=None,
        stop_lag_ms=None,
        start_ack_latency_ms=_ack_latency(timing.start_ack_elapsed_ms, timing.start_command_elapsed_ms),
        stop_ack_latency_ms=_ack_latency(timing.stop_ack_elapsed_ms, timing.stop_command_elapsed_ms),
        confidence=0.0,
        error=error,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze the first and last frames of a CaptureBridge lag-test video.")
    parser.add_argument("video")
    parser.add_argument("--label", default="lagtest")
    parser.add_argument("--start-ms", type=float, required=True)
    parser.add_argument("--stop-ms", type=float, required=True)
    parser.add_argument("--start-ack-ms", type=float, default=None)
    parser.add_argument("--stop-ack-ms", type=float, default=None)
    args = parser.parse_args(argv)

    timing = LagTiming(
        label=args.label,
        display_started_perf=0.0,
        start_command_elapsed_ms=args.start_ms,
        stop_command_elapsed_ms=args.stop_ms,
        start_ack_elapsed_ms=args.start_ack_ms,
        stop_ack_elapsed_ms=args.stop_ack_ms,
    )
    result = analyze_lag_video(args.video, timing)
    print(json.dumps(asdict(result), indent=2))
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
