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
from typing import Any

from .parser import ParsedHour

# ``recorder.import_statistics`` validates ``stats[].state``/``sum`` as ``float``
# or ``int``; a formatted string is rejected by the schema before the recorder
# ever sees the row, so the rows carry plain numbers.
StatRow = dict[str, Any]

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


#: Channel name -> the ``ParsedHour`` attribute carrying that channel's energy.
CHANNELS = {"import": "ap", "export": "am"}


@dataclass
class Accumulation:
    """The two hourly chains a run produces, plus its totals.

    Each channel is accumulated from *its own* anchor.  A chain is a function of
    one channel only: handing the same rows to both series is what wrote the
    import chain (anchored at 0.0) into the export series on the first live run,
    and a test that inspects a single chain cannot see that.
    """

    import_rows: list[StatRow]
    export_rows: list[StatRow]
    totals: dict[str, float]


def accumulate(
    selection: RunSelection,
    anchor_import_kwh: float,
    anchor_export_kwh: float,
) -> Accumulation:
    """Accumulate both channels from the preceding persisted hour, in integer Wh.

    The rows are shaped for ``recorder.import_statistics`` with ``state == sum``
    (the sensor's own value at the *end* of the hour, which is what makes
    ``change`` for an hour equal to that hour's energy).
    """
    if not selection.complete:
        raise ValueError("no importable hours")

    running_import = int(round(anchor_import_kwh * 1000))
    running_export = int(round(anchor_export_kwh * 1000))

    import_rows: list[StatRow] = []
    export_rows: list[StatRow] = []
    for hour in selection.hours:
        running_import += int(round((hour.ap or 0.0) * 1000))
        running_export += int(round((hour.am or 0.0) * 1000))
        start = hour.start.isoformat()
        import_rows.append(
            {"start": start, "state": _kwh(running_import), "sum": _kwh(running_import)}
        )
        export_rows.append(
            {"start": start, "state": _kwh(running_export), "sum": _kwh(running_export)}
        )

    totals = {
        "import_total": running_import / 1000,
        "export_total": running_export / 1000,
        "import_energy": sum(int(round((h.ap or 0.0) * 1000)) for h in selection.hours) / 1000,
        "export_energy": sum(int(round((h.am or 0.0) * 1000)) for h in selection.hours) / 1000,
    }
    return Accumulation(
        import_rows=import_rows, export_rows=export_rows, totals=totals
    )


def _kwh(wh: int) -> float:
    """Wh as kWh, as a number (not a formatted string).

    The statistics writer requires ``float``/``int``; returning a string here is
    what made the very first live import fail with
    ``expected float or int at 'stats[0].state'``.
    """
    return round(wh / 1000, 3)


def verify_chain(
    rows: list[StatRow],
    selection: RunSelection,
    anchor_kwh: float,
    channel: str = "import",
) -> None:
    """Fail loudly if a chain is not exactly that channel's energies.

    Called once per channel.  Checking only one of them is not enough: the two
    chains differ whenever the channels do, so a wrong (or mis-anchored) chain
    shows up immediately, as does the seam bug this was written for — every
    consecutive pair must differ by that hour's energy of *that* channel, and the
    first row must differ from that channel's anchor by the first hour's energy.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    if len(rows) != len(selection.hours):
        raise ValueError(
            f"{channel} chain has {len(rows)} rows for {len(selection.hours)} hours"
        )
    attribute = CHANNELS[channel]
    previous = int(round(anchor_kwh * 1000))
    for row, hour in zip(rows, selection.hours):
        current = int(round(float(row["sum"]) * 1000))
        expected = previous + int(round((getattr(hour, attribute) or 0.0) * 1000))
        if current != expected:
            raise ValueError(
                f"{channel} chain mismatch at {row['start']}: {current} != {expected} Wh"
            )
        previous = current
