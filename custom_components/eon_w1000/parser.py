"""XLSX parser for E.ON W1000 export files.

Handles three E.ON export formats:
1. NEW (2026+): 14-column wide format with embedded variable names and ISO timestamps
   Pod | Időbélyeg | Változó | Érték | Mértékegység | ... (×4 variable groups)
   
2. OLD-WIDE (2025): 5-column long format — one variable per row
   POD | Változó | Időbélyeg | Mértékegység | Érték

3. LEGACY (pre-2025): 5-column wide format with Excel serial dates
   Időbélyeg | Érték | Érték | Érték | Érték
   (time)    | +A    | -A    | 1.8.0 | 2.8.0

Processing:
- 15-min to hourly aggregation of +A/-A values
- Meter reading (1.8.0/2.8.0) forward/backward reconstruction
- Output suitable for HA recorder.import_statistics
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import openpyxl

_LOGGER = logging.getLogger(__name__)

# Excel epoch: December 30, 1899 (Excel's Lotus 1-2-3 compatibility bug)
EXCEL_EPOCH = datetime(1899, 12, 30)

# Variable name mappings (recognize both old and new naming)
_VARIABLE_MAP = {
    "+A": "AP",
    "-A": "AM",
    "DP_1-1:1.8.0*0": "m180",
    "DP_1-1:2.8.0*0": "m280",
}


def _to_num(value: Any, default: float = 0.0) -> float:
    """Safely convert a cell value to float. Returns default on failure."""
    if value is None:
        return default
    s = str(value).strip().replace(",", ".")
    if s == "" or s.lower() == "none":
        return default
    try:
        n = float(s)
        return n if n == n else default  # NaN check
    except (ValueError, TypeError):
        return default


def _to_meter(value: Any) -> float | None:
    """Convert a cell value to meter reading float. Returns None if not a valid meter value."""
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if s == "" or s.lower() == "none":
        return None
    try:
        n = float(s)
        return n if n == n else None
    except (ValueError, TypeError):
        return None


def _parse_iso_timestamp(raw: str, tzinfo: timezone) -> datetime | None:
    """Parse ISO-format timestamp like '2026-07-07 00:00:00' or '2026-07-07T00:00:00'."""
    raw = str(raw).strip()
    if not raw:
        return None
    # Try space-separated format (as in new E.ON exports)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return dt.replace(tzinfo=tzinfo)


def _round_hour(dt: datetime) -> datetime:
    """Round down to the nearest hour."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _detect_format(header: list[str]) -> str:
    """Detect the XLSX format: 'new' (14-col wide), 'old_wide' (5-col long), or 'legacy' (5-col wide)."""
    valtozo_count = sum(1 for h in header if h == "Változó")
    ertek_count = sum(1 for h in header if h == "Érték")
    
    # NEW: 14-column wide with 4 variable groups
    if valtozo_count >= 2:
        return "new"
    
    # OLD-WIDE: 5-column long format — one variable per row
    # Header: ['POD', 'Változó', 'Időbélyeg', 'Mértékegység', 'Érték']
    if valtozo_count == 1 and ertek_count == 1:
        return "old_wide"
    
    # LEGACY: 5-column wide with 4 Érték columns
    if ertek_count >= 4:
        return "legacy"
    
    raise ValueError(
        f"Unrecognized E.ON XLSX format. Header: {header}"
    )


def _parse_new_format(
    rows_iter, header: list[str], tzinfo: timezone
) -> list[dict[str, Any]]:
    """Parse the NEW (14-column) E.ON export format.

    Columns: Pod | Időbélyeg | Változó | Érték | Mértékegység | ... (×4)
    """
    # Build a map: variable name → value column index
    var_cols: dict[str, int] = {}
    for i, h in enumerate(header):
        if h == "Változó" and i + 1 < len(header) and header[i + 1] == "Érték":
            # We'll fill in the mapping from the first data row
            pass

    # Actually, we need to read the first data row to learn the variable names
    # Let's consume from rows_iter, checking each row
    pieces: list[dict[str, Any]] = []

    for row in rows_iter:
        if len(row) < 4:
            continue

        # Column 1 is the timestamp
        raw_time = row[1] if len(row) > 1 else None
        if raw_time is None:
            continue

        dt = _parse_iso_timestamp(str(raw_time), tzinfo)
        if dt is None:
            continue

        hour = _round_hour(dt)

        # Walk through variable groups: columns 2/3, 5/6, 8/9, 11/12
        ap_val: float = 0.0
        am_val: float = 0.0
        m180_val: float | None = None
        m280_val: float | None = None

        for var_col in (2, 5, 8, 11):
            if var_col >= len(row) or var_col + 1 >= len(row):
                continue
            var_name = str(row[var_col]).strip() if row[var_col] else ""
            raw_value = row[var_col + 1] if var_col + 1 < len(row) else None

            mapped = _VARIABLE_MAP.get(var_name)
            if mapped == "AP":
                ap_val = _to_num(raw_value)
            elif mapped == "AM":
                am_val = _to_num(raw_value)
            elif mapped == "m180":
                m180_val = _to_meter(raw_value)
            elif mapped == "m280":
                m280_val = _to_meter(raw_value)

        pieces.append(
            {
                "start": hour,
                "AP": ap_val,
                "AM": am_val,
                "m180": m180_val,
                "m280": m280_val,
            }
        )

    return pieces


def _parse_old_wide_format(
    rows_iter, header: list[str], tzinfo: timezone
) -> list[dict[str, Any]]:
    """Parse the OLD-WIDE (5-column long) E.ON export format.

    One variable per row:
    POD | Változó | Időbélyeg | Mértékegység | Érték

    We need to pivot: group rows by timestamp to combine the 4 variables.
    """
    # Column indices
    pod_col = next((i for i, h in enumerate(header) if h == "POD"), 0)
    var_col = next((i for i, h in enumerate(header) if h == "Változó"), -1)
    time_col = next((i for i, h in enumerate(header) if h == "Időbélyeg"), -1)
    val_col = next((i for i, h in enumerate(header) if h == "Érték"), -1)

    if var_col < 0 or time_col < 0 or val_col < 0:
        raise ValueError(
            f"Missing required columns in old-wide format. Header: {header}"
        )

    # Group rows by timestamp
    by_timestamp: dict[str, dict[str, Any]] = {}
    for row in rows_iter:
        if len(row) <= max(var_col, time_col, val_col):
            continue

        raw_time = row[time_col]
        raw_var = str(row[var_col]).strip() if row[var_col] else ""
        raw_val = row[val_col]

        if raw_time is None or not raw_var:
            continue

        dt = _parse_iso_timestamp(str(raw_time), tzinfo)
        if dt is None:
            continue

        hour_key = _round_hour(dt).isoformat()
        mapped = _VARIABLE_MAP.get(raw_var)

        if hour_key not in by_timestamp:
            by_timestamp[hour_key] = {
                "start": _round_hour(dt),
                "AP": None,
                "AM": None,
                "m180": None,
                "m280": None,
            }

        if mapped == "AP":
            current = by_timestamp[hour_key]["AP"]
            val = _to_num(raw_val)
            by_timestamp[hour_key]["AP"] = (current or 0.0) + val
        elif mapped == "AM":
            current = by_timestamp[hour_key]["AM"]
            val = _to_num(raw_val)
            by_timestamp[hour_key]["AM"] = (current or 0.0) + val
        elif mapped == "m180":
            by_timestamp[hour_key]["m180"] = _to_meter(raw_val)
        elif mapped == "m280":
            by_timestamp[hour_key]["m280"] = _to_meter(raw_val)

    pieces: list[dict[str, Any]] = []
    for ts in sorted(by_timestamp):
        entry = by_timestamp[ts]
        entry["AP"] = entry["AP"] or 0.0
        entry["AM"] = entry["AM"] or 0.0
        pieces.append(entry)

    return pieces


def _parse_legacy_format(
    rows_iter, header: list[str], tzinfo: timezone
) -> list[dict[str, Any]]:
    """Parse the LEGACY (5-column) E.ON export format.

    Columns: Időbélyeg | Érték | Érték | Érték | Érték
              (time)   | +A    | -A    | 1.8.0 | 2.8.0
    """
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

    pieces: list[dict[str, Any]] = []
    for row in rows_iter:
        if time_col >= len(row) or row[time_col] is None:
            continue

        raw_time = row[time_col]
        try:
            if isinstance(raw_time, (int, float)):
                # Excel serial date
                corrected = float(raw_time) + 0.00000001
                dt = EXCEL_EPOCH + timedelta(days=corrected)
                dt = dt.replace(tzinfo=tzinfo)
            elif isinstance(raw_time, datetime):
                dt = raw_time
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tzinfo)
            else:
                dt = _parse_iso_timestamp(str(raw_time), tzinfo)
                if dt is None:
                    continue
        except (ValueError, TypeError, OSError):
            continue

        hour = _round_hour(dt)

        ap = _to_num(row[value_cols[0]] if value_cols[0] < len(row) else None)
        am = _to_num(row[value_cols[1]] if value_cols[1] < len(row) else None)
        m180_raw = row[value_cols[2]] if value_cols[2] < len(row) else None
        m280_raw = row[value_cols[3]] if value_cols[3] < len(row) else None

        pieces.append(
            {
                "start": hour,
                "AP": ap,
                "AM": am,
                "m180": _to_meter(m180_raw),
                "m280": _to_meter(m280_raw),
            }
        )

    return pieces


def _aggregate_and_reconstruct(
    pieces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate 15-min pieces to hourly, then reconstruct meter readings."""
    if not pieces:
        raise ValueError("No valid data rows found in XLSX file")

    # --- Aggregate by hour ---
    grouped: dict[datetime, dict[str, Any]] = {}
    for p in pieces:
        h = p["start"]
        if h not in grouped:
            grouped[h] = {
                "start": h,
                "AP": 0.0,
                "AM": 0.0,
                "m180": None,
                "m280": None,
            }
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

    # --- Backward pass: reconstruct missing start-of-period values ---
    for meter_key, field_key in [("start180", "AP"), ("start280", "AM")]:
        first_idx = next(
            (i for i, h in enumerate(hours) if h[meter_key] is not None), -1
        )
        if first_idx > 0:
            base = hours[first_idx][meter_key]
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

    _LOGGER.debug(
        "Parsed %d 15-min pieces → %d hourly aggregates", len(pieces), len(result)
    )
    return result


# --- Public API ---


def parse_eon_xlsx(
    file_path: str, tzinfo: timezone | None = None
) -> list[dict[str, Any]]:
    """Parse E.ON W1000 XLSX export and return calculated hourly rows.

    Auto-detects the export format (new 14-column or legacy 5-column).

    Returns list of dicts with keys: start, AP, AM, 1_8_0, 2_8_0
    Each row is an hourly aggregate with cumulative meter readings.
    """
    if tzinfo is None:
        tzinfo = datetime.now().astimezone().tzinfo

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = wb.active

    rows_iter = sheet.iter_rows(min_row=1, values_only=True)
    header = [str(c).strip() if c else "" for c in next(rows_iter, [])]

    fmt = _detect_format(header)
    _LOGGER.debug("Detected E.ON XLSX format: %s (header: %s)", fmt, header)

    if fmt == "new":
        pieces = _parse_new_format(rows_iter, header, tzinfo)
    elif fmt == "old_wide":
        pieces = _parse_old_wide_format(rows_iter, header, tzinfo)
    else:
        pieces = _parse_legacy_format(rows_iter, header, tzinfo)

    wb.close()

    return _aggregate_and_reconstruct(pieces)


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
