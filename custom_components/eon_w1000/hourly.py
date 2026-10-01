"""Hourly selection and accumulation for E.ON W1000 imports.

Two rules, both ported from the importer this integration has to be
interchangeable with (and from its hard-won history of midnight seams):

1. **Only a contiguous run of complete hours is imported.**  A truncated
   quarter-hour tail, a missing hour, a gap, or an hour with a damaged channel
   stops the run — it is never zero-filled and the import never steps over it.
2. **The cumulative series is anchored to the hour the recorder already holds
   immediately before the run.**  It is accumulated in integer Wh from that
   anchor, so importing the same window twice is idempotent and importing an
   overlapping window cannot move the seam to the oldest replaced hour.

The module has no Home Assistant imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .parser import ParsedHour

_LOGGER = logging.getLogger(__name__)

HOUR = timedelta(hours=1)


@dataclass
class RunSelection:
    """The importable part of a file (or of several merged files)."""

    hours: list[ParsedHour] = field(default_factory=list)
    skipped: list[tuple[datetime, str]] = field(default_factory=list)
    missing_export_channel: bool = False
    duplicates: list[datetime] = field(default_factory=list)
    """Hours that carried more quarter-hour slots than the file's normal hour.

    On the autumn DST change the local hour 02:00 exists twice, so a
    wall-clock labelled export has five hours' worth of rows labelled 02:00
    (and only that one hour is ambiguous).  The energy is not lost — every row
    is still counted — but that single bucket holds more than one physical hour,
    so it is reported instead of passing silently.
    """

    @property
    def complete(self) -> bool:
        return bool(self.hours)

    @property
    def first(self) -> datetime | None:
        return self.hours[0].start if self.hours else None

    @property
    def last(self) -> datetime | None:
        return self.hours[-1].start if self.hours else None

    @property
    def anchor_hour(self) -> datetime | None:
        return self.first - HOUR if self.first else None


def select_run(hours: list[ParsedHour], expected_quarters: int) -> RunSelection:
    """Pick the longest contiguous run of complete hours from the front.

    ``hours`` must be sorted and de-duplicated by hour (the coordinator merges
    overlapping attachments newest-wins before calling this).
    """
    if not hours:
        return RunSelection()

    # A portal export without the feed-in channel configured: treat a wholly
    # absent -A channel as 0 rather than refusing to import anything, but say so.
    has_any_export = any(hour.am is not None for hour in hours)
    if not has_any_export:
        _LOGGER.warning(
            "No '-A' (feed-in) values in this export at all; treating export as 0 kWh"
        )
        for hour in hours:
            hour.am = 0.0

    expected = max(expected_quarters, 1)

    selection = RunSelection(missing_export_channel=not has_any_export)
    selection.duplicates = [hour.start for hour in hours if hour.pieces > expected]
    if selection.duplicates:
        _LOGGER.warning(
            "%d hour(s) carry more quarter-hour rows than the file's normal hour "
            "(%d): %s — typically the repeated local hour of a DST change; their "
            "energy is counted, but the bucket holds more than one physical hour",
            len(selection.duplicates),
            expected,
            ", ".join(moment.isoformat() for moment in selection.duplicates[:5]),
        )

    def defect(hour: ParsedHour) -> str | None:
        if hour.ap is None:
            return "no +A (consumption) value"
        if hour.am is None:
            return "no -A (feed-in) value"
        if hour.ap < 0:
            return f"negative consumption ({hour.ap})"
        if hour.am < 0:
            return f"negative feed-in ({hour.am})"
        if hour.pieces < expected:
            return f"truncated hour ({hour.pieces}/{expected} quarter-hour slots)"
        if hour.ap_count < hour.pieces:
            return f"incomplete +A channel ({hour.ap_count}/{hour.pieces} values)"
        if not selection.missing_export_channel and hour.am_count < hour.pieces:
            return f"incomplete -A channel ({hour.am_count}/{hour.pieces} values)"
        return None

    # Find the first importable hour; anything before it is reported, not used.
    start_index = None
    for index, hour in enumerate(hours):
        reason = defect(hour)
        if reason is None:
            start_index = index
            break
        selection.skipped.append((hour.start, reason))

    if start_index is None:
        return selection

    selection.hours.append(hours[start_index])
    for index in range(start_index + 1, len(hours)):
        hour = hours[index]
        previous = hours[index - 1]
        if hour.start - previous.start != HOUR:
            selection.skipped.append(
                (hour.start, f"gap after {previous.start.isoformat()} "
                             f"({(hour.start - previous.start).total_seconds() / 3600:.1f} h)")
            )
            break
        reason = defect(hour)
        if reason is not None:
            selection.skipped.append((hour.start, reason))
            break
        selection.hours.append(hour)

    return selection


def accumulate(
    selection: RunSelection,
    anchor_import_kwh: float,
    anchor_export_kwh: float,
) -> tuple[list[dict[str, str]], dict[str, float]]:
    """Accumulate the run from the preceding persisted hour, in integer Wh.

    Returns ``(stats, totals)`` where ``stats`` rows are shaped for
    ``recorder.import_statistics`` with ``state == sum`` (the sensor's own value
    at the *end* of the hour, which is what makes ``change`` for an hour equal to
    that hour's energy).
    """
    if not selection.complete:
        raise ValueError("no importable hours")

    running_import = int(round(anchor_import_kwh * 1000))
    running_export = int(round(anchor_export_kwh * 1000))

    stats: list[dict[str, str]] = []
    for hour in selection.hours:
        running_import += int(round((hour.ap or 0.0) * 1000))
        running_export += int(round((hour.am or 0.0) * 1000))
        value = _kwh(running_import)
        stats.append(
            {
                "start": hour.start.isoformat(),
                "state": value,
                "sum": value,
            }
        )

    totals = {
        "import_total": running_import / 1000,
        "export_total": running_export / 1000,
        "import_energy": sum(int(round((h.ap or 0.0) * 1000)) for h in selection.hours) / 1000,
        "export_energy": sum(int(round((h.am or 0.0) * 1000)) for h in selection.hours) / 1000,
    }
    return stats, totals


def _kwh(wh: int) -> str:
    return f"{wh / 1000:.3f}"


def verify_chain(stats: list[dict[str, str]], selection: RunSelection, anchor_import: float) -> None:
    """Fail loudly if the produced chain is not exactly the source energies.

    Regression guard for the seam bug: every consecutive pair must differ by the
    hour's +A/-A energy and the first row must differ from the anchor by the
    first hour's energy.
    """
    previous_import = int(round(anchor_import * 1000))
    for row, hour in zip(stats, selection.hours):
        current = int(round(float(row["sum"]) * 1000))
        expected = previous_import + int(round((hour.ap or 0.0) * 1000))
        if current != expected:
            raise ValueError(
                f"chain mismatch at {row['start']}: {current} != {expected} Wh"
            )
        previous_import = current
