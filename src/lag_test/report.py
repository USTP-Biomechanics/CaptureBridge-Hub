from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict
from typing import Any, Dict

from .analyzer import LagAnalysisResult, LagTiming


def write_lag_report(
    report_base_path: str,
    timing: LagTiming,
    analysis: LagAnalysisResult,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, str]:
    os.makedirs(os.path.dirname(os.path.abspath(report_base_path)), exist_ok=True)
    json_path = report_base_path + ".json"
    csv_path = report_base_path + ".csv"
    payload = {
        "timing": asdict(timing),
        "analysis": asdict(analysis),
        "extra": extra or {},
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    row = {**asdict(timing), **asdict(analysis)}
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
    return {"json": json_path, "csv": csv_path}
