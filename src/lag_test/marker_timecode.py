from __future__ import annotations

from functools import lru_cache


ARUCO_DICT_NAME = "DICT_6X6_1000"
ARUCO_MAX_ID = 999
ARUCO_MARKER_BITS = 6
ARUCO_BORDER_BITS = 1
ARUCO_MODULES = ARUCO_MARKER_BITS + 2 * ARUCO_BORDER_BITS
MARKER_STEP_MS = 5
MAX_MARKER_MS = ARUCO_MAX_ID * MARKER_STEP_MS


def encode_marker_time(elapsed_ms: float) -> tuple[int, int]:
    quantized_ms = int(max(0.0, float(elapsed_ms)) // MARKER_STEP_MS) * MARKER_STEP_MS
    marker_id = max(0, min(ARUCO_MAX_ID, quantized_ms // MARKER_STEP_MS))
    return marker_id, marker_id * MARKER_STEP_MS


def decode_marker_time(marker_id: int) -> int | None:
    marker_id = int(marker_id)
    if marker_id < 0 or marker_id > ARUCO_MAX_ID:
        return None
    return marker_id * MARKER_STEP_MS


def get_aruco_dictionary(cv2_module):
    aruco = getattr(cv2_module, "aruco", None)
    if aruco is None:
        raise RuntimeError("This OpenCV build does not include cv2.aruco")
    dictionary_id = getattr(aruco, ARUCO_DICT_NAME)
    return aruco.getPredefinedDictionary(dictionary_id)


@lru_cache(maxsize=ARUCO_MAX_ID + 1)
def marker_matrix(marker_id: int) -> tuple[tuple[int, ...], ...]:
    import cv2

    marker_id = max(0, min(ARUCO_MAX_ID, int(marker_id)))
    image = cv2.aruco.generateImageMarker(
        get_aruco_dictionary(cv2),
        marker_id,
        ARUCO_MODULES,
        borderBits=ARUCO_BORDER_BITS,
    )
    return tuple(tuple(1 if int(value) > 127 else 0 for value in row) for row in image)


def prewarm_marker_matrices(max_marker_id: int = ARUCO_MAX_ID) -> None:
    """Generate marker matrices before the visible lag clock starts."""
    limit = max(0, min(ARUCO_MAX_ID, int(max_marker_id)))
    for marker_id in range(limit + 1):
        marker_matrix(marker_id)
