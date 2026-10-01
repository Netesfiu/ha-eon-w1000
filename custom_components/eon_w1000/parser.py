"""XLSX parser for E.ON W1000 export files.

Handles the three known E.ON export formats:

1. NEW (2026+): 14-column wide format, embedded variable names, ISO timestamps
   ``Pod | Időbélyeg | Változó | Érték | Mértékegység`` (x4 variable groups)
2. OLD-WIDE (2025): 5-column long format, one variable per row, pivot needed
   ``POD | Változó | Időbélyeg | Mértékegység | Érték``
3. LEGACY (pre-2025): 5-column wide format with Excel serial dates
   ``Időbélyeg | Érték | Érték | Érték | Érték`` (+A, -A, 1.8.0, 2.8.0)

Output contract — deliberately *lossless*:

* A value that is absent in the file stays ``None``.  It is never converted to
  ``0.0``: a missing quarter-hour reading must not become a plausible-looking
  zero-consumption interval.  (The previous importer rejected such hours; this
  parser preserves the distinction so the caller can reject them too.)
* Every hour carries the number of quarter-hour slots it actually contained, so
  the caller can tell a complete hour from a truncated one.
* The raw meter registers (1.8.0 / 2.8.0) are returned for diagnostics and
  cross-checking only.  Cumulative statistics are *not* derived from them — a
  file only ever covers a rolling window, so its registers cannot anchor a
  series that is already persisted in the recorder.

The module imports nothing from Home Assistant so it can be unit-tested
standalone.
"""

from __future__ import annotations

import logging
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo as _tzinfo
from typing import Any, Iterable, Iterator

import openpyxl

_LOGGER = logging.getLogger(__name__)

# Excel epoch: December 30, 1899 (Excel keeps Lotus 1-2-3's leap-year bug).
EXCEL_EPOCH = datetime(1899, 12, 30)

# Variable name mappings (the exports have used both spellings).
_VARIABLE_MAP = {
    "+A": "AP",
    "-A": "AM",
    "DP_1-1:1.8.0*0": "m180",
    "DP_1-1:2.8.0*0": "m280",
}

_MISSING_TOKENS = {"", "none", "null", "nan", "-", "n/a", "na"}


@dataclass
class ParsedHour:
    """One hourly bucket as read from a single file."""

    start: datetime
    ap: float | None = None
    am: float | None = None
    pieces: int = 0
    ap_count: int = 0
    am_count: int = 0
    m180: float | None = None
    m280: float | None = None

    @property
    def is_complete(self) -> bool:
        return self.ap is not None and self.am is not None


@dataclass
class ParseResult:
    """The result of parsing one XLSX attachment."""

    hours: list[ParsedHour] = field(default_factory=list)
    export_format: str = "unknown"
    piece_count: int = 0
    expected_quarters: int = 0

    @property
    def first(self) -> datetime | None:
        return self.hours[0].start if self.hours else None

    @property
    def last(self) -> datetime | None:
        return self.hours[-1].start if self.hours else None

    @property
    def m180(self) -> float | None:
        """Last raw 1.8.0 register seen in the file (diagnostics only)."""
        for hour in reversed(self.hours):
            if hour.m180 is not None:
                return hour.m180
        return None

    @property
    def m280(self) -> float | None:
        """Last raw 2.8.0 register seen in the file (diagnostics only)."""
        for hour in reversed(self.hours):
            if hour.m280 is not None:
                return hour.m280
        return None


# --------------------------------------------------------------------------- #
# Cell conversion
# --------------------------------------------------------------------------- #
def to_float(value: Any) -> float | None:
    """Convert a cell to float, returning ``None`` for anything absent/invalid.

    Handles the comma decimal separator used by the Hungarian exports without
    destroying a thousands separator (``"1,234.5"`` keeps its point).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else None
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if text.lower() in _MISSING_TOKENS:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _round_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def parse_timestamp(raw: Any, tzinfo: timezone) -> datetime | None:
    """Parse a cell into an aware datetime, or ``None`` if it is not a timestamp.

    The wall-clock value in the file is *local* time; ``tzinfo`` must therefore
    be the Home Assistant configured zone for the interval in question.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        moment = raw
        return moment if moment.tzinfo else moment.replace(tzinfo=tzinfo)
    if isinstance(raw, (int, float)):
        try:
            return (EXCEL_EPOCH + timedelta(days=float(raw) + 1e-8)).replace(tzinfo=tzinfo)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tzinfo)
        except ValueError:
            continue
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tzinfo)
    return moment


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #
def detect_format(header: list[str]) -> str:
    """Detect the export format from its header shape."""
    valtozo = sum(1 for cell in header if cell == "Változó")
    ertek = sum(1 for cell in header if cell == "Érték")
    if valtozo >= 2:
        return "new"
    if valtozo == 1 and ertek == 1:
        return "old_wide"
    if ertek >= 4:
        return "legacy"
    raise ValueError(f"Unrecognized E.ON XLSX format. Header: {header}")


# --------------------------------------------------------------------------- #
# Per-format readers -> (hour, AP, AM, m180, m280) pieces
# --------------------------------------------------------------------------- #
def _read_new_format(rows: Iterator[tuple], header: list[str], tzinfo: timezone) -> list[dict[str, Any]]:
    """14-column wide format: four variable groups per row."""
    pieces: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 4:
            continue
        moment = parse_timestamp(row[1], tzinfo)
        if moment is None:
            continue
        piece: dict[str, Any] = {
            "start": _round_hour(moment),
            "AP": None,
            "AM": None,
            "m180": None,
            "m280": None,
        }
        for var_col in (2, 5, 8, 11):
            if var_col + 1 >= len(row):
                continue
            name = str(row[var_col]).strip() if row[var_col] else ""
            target = _VARIABLE_MAP.get(name)
            if target is None:
                continue
            piece[target] = to_float(row[var_col + 1])
        pieces.append(piece)
    return pieces


def _read_old_wide_format(rows: Iterator[tuple], header: list[str], tzinfo: timezone) -> list[dict[str, Any]]:
    """5-column long format: one variable per row, pivoted on the timestamp."""
    pod_col = next((i for i, h in enumerate(header) if h == "POD"), 0)
    var_col = next((i for i, h in enumerate(header) if h == "Változó"), -1)
    time_col = next((i for i, h in enumerate(header) if h == "Időbélyeg"), -1)
    val_col = next((i for i, h in enumerate(header) if h == "Érték"), -1)
    if min(var_col, time_col, val_col) < 0:
        raise ValueError(f"Missing required columns in old-wide format. Header: {header}")
    del pod_col

    pieces: list[dict[str, Any]] = []
    for row in rows:
        if len(row) <= max(var_col, time_col, val_col):
            continue
        name = str(row[var_col]).strip() if row[var_col] else ""
        target = _VARIABLE_MAP.get(name)
        if target is None:
            continue
        moment = parse_timestamp(row[time_col], tzinfo)
        if moment is None:
            continue
        pieces.append(
            {
                "start": _round_hour(moment),
                "AP": to_float(row[val_col]) if target == "AP" else None,
                "AM": to_float(row[val_col]) if target == "AM" else None,
                "m180": to_float(row[val_col]) if target == "m180" else None,
                "m280": to_float(row[val_col]) if target == "m280" else None,
            }
        )
    return pieces


def _read_legacy_format(rows: Iterator[tuple], header: list[str], tzinfo: timezone) -> list[dict[str, Any]]:
    """5-column wide format with Excel serial timestamps."""
    time_col = next((i for i, h in enumerate(header) if h == "Időbélyeg"), None)
    value_cols = [i for i, h in enumerate(header) if h == "Érték"]
    if time_col is None:
        raise ValueError("No 'Időbélyeg' column found in XLSX header")
    if len(value_cols) < 4:
        raise ValueError(f"Expected 4 'Érték' columns, found {len(value_cols)}")

    pieces: list[dict[str, Any]] = []
    for row in rows:
        if time_col >= len(row) or row[time_col] is None:
            continue
        moment = parse_timestamp(row[time_col], tzinfo)
        if moment is None:
            continue

        def cell(index: int) -> float | None:
            column = value_cols[index]
            return to_float(row[column]) if column < len(row) else None

        pieces.append(
            {
                "start": _round_hour(moment),
                "AP": cell(0),
                "AM": cell(1),
                "m180": cell(2),
                "m280": cell(3),
            }
        )
    return pieces


# --------------------------------------------------------------------------- #
# Hourly aggregation
# --------------------------------------------------------------------------- #
def aggregate_hours(pieces: Iterable[dict[str, Any]]) -> list[ParsedHour]:
    """Group 15-minute pieces into hourly buckets without inventing values."""
    buckets: dict[datetime, ParsedHour] = {}
    for piece in pieces:
        start = piece["start"]
        bucket = buckets.get(start)
        if bucket is None:
            bucket = buckets[start] = ParsedHour(start=start)
        bucket.pieces += 1
        for key, attribute in (("AP", "ap"), ("AM", "am"), ("m180", "m180"), ("m280", "m280")):
            value = piece.get(key)
            if value is None:
                continue
            if key in ("AP", "AM"):
                current = getattr(bucket, attribute)
                setattr(bucket, attribute, (current or 0.0) + value)
                setattr(bucket, f"{key.lower()}_count", getattr(bucket, f"{key.lower()}_count") + 1)
            else:
                # A register is a point reading: keep the last one seen.
                setattr(bucket, attribute, value)
    return [buckets[key] for key in sorted(buckets)]


def expected_quarters_per_hour(hours: list[ParsedHour]) -> int:
    """Most frequent number of quarter-hour slots per hour in this file.

    Used to tell "complete" from "truncated"; ties prefer the larger count.
    """
    if not hours:
        return 0
    counts = Counter(hour.pieces for hour in hours)
    return sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse_eon_xlsx(file_path: str, tzinfo: _tzinfo | None = None) -> ParseResult:
    """Parse an E.ON W1000 export into hourly buckets (lossless about gaps)."""
    if tzinfo is None:
        tzinfo = datetime.now().astimezone().tzinfo

    # E.ON's exports carry no default cell style, and openpyxl warns about that
    # on every load.  It is noise in the Home Assistant log, nothing more.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Workbook contains no default style"
        )
        workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(min_row=1, values_only=True)
        header = [str(cell).strip() if cell is not None else "" for cell in next(rows, [])]
        export_format = detect_format(header)
        if export_format == "new":
            pieces = _read_new_format(rows, header, tzinfo)
        elif export_format == "old_wide":
            raise ValueError("old_wide format is not supported for Excel-only history: per-variable rows are not quarter-hour slots")
        else:
            pieces = _read_legacy_format(rows, header, tzinfo)
    finally:
        workbook.close()

    hours = aggregate_hours(pieces)
    if not hours:
        raise ValueError("No valid data rows found in XLSX file")

    result = ParseResult(
        hours=hours,
        export_format=export_format,
        piece_count=len(pieces),
        expected_quarters=expected_quarters_per_hour(hours),
    )
    _LOGGER.debug(
        "Parsed %s: format=%s pieces=%d hours=%d expected_quarters=%d",
        file_path,
        export_format,
        result.piece_count,
        len(hours),
        result.expected_quarters,
    )
    return result
