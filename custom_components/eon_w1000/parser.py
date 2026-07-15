"""XLSX parser for E.ON W1000 export files.

Port of the n8n "Calculate hourly sum" Code node logic to Python.
Handles:
- Excel serial date conversion
- 15-min to hourly aggregation
- Meter reading forward/backward reconstruction (1.8.0 / 2.8.0)
- Output format suitable for recorder.import_statistics
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import openpyxl

_LOGGER = logging.getLogger(__name__)

# Excel epoch: December 30, 1899 (not Dec 31 — Excel has the Lotus 1-2-3 bug)
EXCEL_EPOCH = datetime(1899, 12, 30)


def _excel_serial_to_datetime(
    serial: float, tzinfo: timezone | None = None
) -> datetime:
    """Convert Excel serial date number to datetime.

    Adds a microsecond epsilon to handle floating-point rounding.
    """
    corrected = serial + 0.00000001
    dt = EXCEL_EPOCH + timedelta(days=corrected)
    if tzinfo is not None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt


def _to_num(value: Any, default: float = 0.0) -> float:
    """Safely convert a cell value to float. Returns default on failure."""
    if value is None or value == "":
        return default
    try:
        n = float(str(value).replace(",", "."))
        return n if n == n else default  # NaN check
    except (ValueError, TypeError):
        return default


def _round_hour(dt: datetime) -> datetime:
    """Round down to the nearest hour."""
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_eon_xlsx(
    file_path: str, tzinfo: timezone | None = None
) -> list[dict[str, Any]]:
    """Parse E.ON W1000 XLSX export and return calculated hourly rows.

    The E.ON export has a header row followed by data rows:
      Időbélyeg | Érték | Érték | Érték | Érték
      (time)    | +A    | -A    | 1.8.0 | 2.8.0

    Returns list of dicts with keys: start, AP, AM, 1_8_0, 2_8_0
    Each row is an hourly aggregate with cumulative meter readings.
    """
    if tzinfo is None:
        tzinfo = datetime.now().astimezone().tzinfo

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = wb.active  # type: ignore[arg-type]

    rows_iter = sheet.iter_rows(min_row=1, values_only=True)
    header = [str(c).strip() if c else "" for c in next(rows_iter, [])]

    # Find column indices
    time_col: int | None = None
    value_cols: list[int] = []

    for i, name in enumerate(header):
        if name == "Időbélyeg":
            time_col = i
        elif name == "Érték":
            value_cols.append(i)

    if time_col is None:
        raise ValueError("No 'Időbélyeg' column found in XLSX header")
    if len(value_cols) != 4:
        raise ValueError(
            f"Expected 4 'Érték' columns, found {len(value_cols)}"
        )

    # Read raw pieces: one per 15-min row
    pieces: list[dict[str, Any]] = []
    for row in rows_iter:
        if time_col >= len(row) or row[time_col] is None:
            continue

        raw_time = row[time_col]
        try:
            if isinstance(raw_time, (int, float)):
                dt = _excel_serial_to_datetime(float(raw_time), tzinfo)
            elif isinstance(raw_time, datetime):
                dt = raw_time
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tzinfo)
            else:
                dt = datetime.fromisoformat(str(raw_time))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tzinfo)
        except (ValueError, TypeError, OSError):
            continue

        hour = _round_hour(dt)

        ap = _to_num(row[value_cols[0]] if value_cols[0] < len(row) else None)
        am = _to_num(row[value_cols[1]] if value_cols[1] < len(row) else None)
        m180_raw = row[value_cols[2]] if value_cols[2] < len(row) else None
        m280_raw = row[value_cols[3]] if value_cols[3] < len(row) else None

        m180_val = _to_num(m180_raw) if m180_raw is not None and m180_raw != "" else None
        m280_val = _to_num(m280_raw) if m280_raw is not None and m280_raw != "" else None

        pieces.append(
            {
                "start": hour,
                "AP": ap,
                "AM": am,
                "m180": m180_val,
                "m280": m280_val,
            }
        )

    wb.close()

    if not pieces:
        raise ValueError("No valid data rows found in XLSX file")

    # --- Aggregate by hour ---
    grouped: dict[datetime, dict[str, Any]] = {}
    for p in pieces:
        h = p["start"]
        if h not in grouped:
            grouped[h] = {"start": h, "AP": 0.0, "AM": 0.0, "m180": None, "m280": None}
        grouped[h]["AP"] += p["AP"]
        grouped[h]["AM"] += p["AM"]
        if p["m180"] is not None:
            grouped[h]["m180"] = p["m180"]
        if p["m280"] is not None:
            grouped[h]["m280"] = p["m280"]

    hours = sorted(grouped.values(), key=lambda x: x["start"])

    # --- Forward pass: carry meter readings forward ---
    last180: float | None = None
    last280: float | None = None

    for h in hours:
        if h["m180"] is not None:
            last180 = h["m180"]
        if h["m280"] is not None:
            last280 = h["m280"]

        h["start180"] = last180
        h["start280"] = last280

        if last180 is not None:
            last180 += h["AP"]
        if last280 is not None:
            last280 += h["AM"]

        h["end180"] = last180
        h["end280"] = last280

    # --- Backward pass: reconstruct missing start-of-period values ---
    # Handle 1_8_0 and 2_8_0 independently — they may appear in different hours
    for meter_key, field_key in [("start180", "AP"), ("start280", "AM")]:
        first_idx = next(
            (i for i, h in enumerate(hours) if h[meter_key] is not None), -1
        )
        if first_idx > 0:
            base = hours[first_idx][meter_key]  # type: ignore[assignment]
            for i in range(first_idx - 1, -1, -1):
                h = hours[i]
                base -= h[field_key]
                h[meter_key] = base

    # Final fallback: if still None, use 0
    for h in hours:
        if h["start180"] is None:
            h["start180"] = 0.0
        if h["start280"] is None:
            h["start280"] = 0.0

    # --- Build output ---
    result: list[dict[str, Any]] = []
    for h in hours:
        result.append(
            {
                "start": h["start"].isoformat(),
                "AP": f"{h['AP']:.3f}",
                "AM": f"{h['AM']:.3f}",
                "1_8_0": f"{h['start180']:.3f}",
                "2_8_0": f"{h['start280']:.3f}",
            }
        )

    _LOGGER.debug("Parsed %d rows → %d hourly aggregates", len(pieces), len(result))
    return result


def build_statistics_payload(
    calculated: list[dict[str, Any]], meter_key: str
) -> list[dict[str, Any]]:
    """Build recorder.import_statistics stats array from calculated rows.

    meter_key: '1_8_0' for import, '2_8_0' for export
    """
    stats = []
    for row in calculated:
        state = float(row[meter_key])
        stats.append(
            {
                "start": row["start"],
                "state": state,
                "sum": state,
            }
        )
    return stats
