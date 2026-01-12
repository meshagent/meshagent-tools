from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo
from .config import ToolkitConfig
from .tool import Tool
from .toolkit import ToolContext, ToolkitBuilder
from .hosting import RemoteToolkit, Toolkit
from meshagent.api.room_server_client import RoomClient


# ----------------------------
# Helpers
# ----------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _get_tz(tz: Optional[str]) -> timezone:
    """
    Returns a tzinfo. If tz is None -> UTC.
    If tz is provided, uses zoneinfo when available; falls back to UTC if unknown.
    """
    if not tz:
        return timezone.utc
    if ZoneInfo is None:
        # zoneinfo not available; safest fallback
        return timezone.utc
    try:
        return ZoneInfo(tz)  # type: ignore[arg-type]
    except Exception:
        return timezone.utc


def _ensure_aware(dt: datetime, tz: Optional[str] = None) -> datetime:
    """
    If dt is naive, interpret it in tz (or UTC if tz not provided) and make aware.
    If aware, leave as-is.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_get_tz(tz))
    return dt


def _to_tz(dt: datetime, tz: Optional[str]) -> datetime:
    dt = _ensure_aware(dt, tz=None)
    return dt.astimezone(_get_tz(tz))


def _iso(dt: datetime) -> str:
    """
    Canonical DB-friendly ISO with offset.
    """
    dt = _ensure_aware(dt)
    # Keep offset; many DBs accept it; if you prefer 'Z', format_utc tool handles that.
    return dt.isoformat()


def _parse_iso(s: str, assume_tz: Optional[str] = None) -> datetime:
    """
    Parse ISO-8601-ish strings. If it ends with 'Z', treat as UTC.
    If parsed datetime is naive, attach assume_tz (or UTC).
    """
    s2 = s.strip()
    if s2.endswith("Z"):
        s2 = s2[:-1] + "+00:00"
    dt = datetime.fromisoformat(s2)
    return _ensure_aware(dt, assume_tz)


def _start_of_day(dt: datetime) -> datetime:
    dt = _ensure_aware(dt)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    # inclusive end: 23:59:59.999999
    dt = _ensure_aware(dt)
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


def _start_of_month(dt: datetime) -> datetime:
    dt = _ensure_aware(dt)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _end_of_month(dt: datetime) -> datetime:
    dt = _ensure_aware(dt)
    # go to first of next month then subtract a microsecond
    if dt.month == 12:
        nxt = dt.replace(
            year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        nxt = dt.replace(
            month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return nxt - timedelta(microseconds=1)


def _start_of_week(dt: datetime, week_start: int) -> datetime:
    """
    week_start: 0=Mon ... 6=Sun
    """
    dt = _ensure_aware(dt)
    # Python weekday(): Mon=0..Sun=6
    delta_days = (dt.weekday() - week_start) % 7
    return _start_of_day(dt - timedelta(days=delta_days))


def _end_of_week(dt: datetime, week_start: int) -> datetime:
    return (
        _start_of_week(dt, week_start) + timedelta(days=7) - timedelta(microseconds=1)
    )


# ----------------------------
# Tools
# ----------------------------


class NowTool(Tool):
    def __init__(self):
        super().__init__(
            name="now",
            title="now",
            description="Get current time. Returns both UTC and (optional) timezone-local ISO strings.",
            input_schema={
                "type": "object",
                "required": ["tz"],
                "additionalProperties": False,
                "properties": {
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name (e.g. 'America/Los_Angeles'). If omitted, only UTC is returned.",
                    }
                },
            },
        )

    async def execute(self, context: ToolContext, tz: Optional[str] = None):
        utc = _now_utc()
        out: dict[str, Any] = {"utc": _iso(utc)}
        if tz:
            out["local"] = _iso(_to_tz(utc, tz))
            out["tz"] = tz
        return out


class TodayTool(Tool):
    def __init__(self):
        super().__init__(
            name="today_range",
            title="today range",
            description="Get the start/end of 'today' in a given timezone (defaults to UTC). Useful for date filters.",
            input_schema={
                "type": "object",
                "required": ["tz"],
                "additionalProperties": False,
                "properties": {
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name (default UTC).",
                    },
                },
            },
        )

    async def execute(self, context: ToolContext, tz: Optional[str] = None):
        base = _to_tz(_now_utc(), tz)
        start = _start_of_day(base)
        end = _end_of_day(base)
        return {"start": _iso(start), "end": _iso(end), "tz": tz or "UTC"}


class WeekRangeTool(Tool):
    def __init__(self):
        super().__init__(
            name="week_range",
            title="week range",
            description="Get start/end of the week containing a given datetime (or now), in a timezone. Week start configurable.",
            input_schema={
                "type": "object",
                "required": ["dt", "tz", "week_start"],
                "additionalProperties": False,
                "properties": {
                    "dt": {
                        "type": ["string", "null"],
                        "description": "ISO datetime. If omitted, uses now.",
                    },
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name (default UTC).",
                    },
                    "week_start": {
                        "type": "integer",
                        "description": "0=Mon .. 6=Sun (default 0).",
                        "minimum": 0,
                        "maximum": 6,
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        dt: Optional[str] = None,
        tz: Optional[str] = None,
        week_start: int = 0,
    ):
        base = _to_tz(_parse_iso(dt, assume_tz=tz) if dt else _now_utc(), tz)
        start = _start_of_week(base, week_start)
        end = _end_of_week(base, week_start)
        iso_year, iso_week, iso_wday = base.isocalendar()
        return {
            "start": _iso(start),
            "end": _iso(end),
            "tz": tz or "UTC",
            "week_start": week_start,
            "iso": {"year": iso_year, "week": iso_week, "weekday": iso_wday},
        }


class MonthRangeTool(Tool):
    def __init__(self):
        super().__init__(
            name="month_range",
            title="month range",
            description="Get start/end of the month containing a given datetime (or now), in a timezone.",
            input_schema={
                "type": "object",
                "required": ["dt", "tz"],
                "additionalProperties": False,
                "properties": {
                    "dt": {
                        "type": ["string", "null"],
                        "description": "ISO datetime. If omitted, uses now.",
                    },
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name (default UTC).",
                    },
                },
            },
        )

    async def execute(
        self, context: ToolContext, dt: Optional[str] = None, tz: Optional[str] = None
    ):
        base = _to_tz(_parse_iso(dt, assume_tz=tz) if dt else _now_utc(), tz)
        start = _start_of_month(base)
        end = _end_of_month(base)
        return {
            "start": _iso(start),
            "end": _iso(end),
            "tz": tz or "UTC",
            "year": base.year,
            "month": base.month,
        }


class AddDurationTool(Tool):
    def __init__(self):
        super().__init__(
            name="add_duration",
            title="add duration",
            description="Add a duration to an ISO datetime. Supports days/hours/minutes/seconds.",
            input_schema={
                "type": "object",
                "required": ["dt", "tz", "days", "hours", "minutes", "seconds"],
                "additionalProperties": False,
                "properties": {
                    "dt": {"type": "string", "description": "Base ISO datetime."},
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. If dt is naive, interpret it in this timezone (default UTC). Also controls output tz.",
                    },
                    "days": {"type": "integer"},
                    "hours": {"type": "integer"},
                    "minutes": {"type": "integer"},
                    "seconds": {"type": "integer"},
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        dt: str,
        tz: Optional[str] = None,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ):
        base = _parse_iso(dt, assume_tz=tz)
        base = _to_tz(base, tz)
        out = base + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return {"dt": _iso(out), "tz": tz or "UTC"}


class DiffTool(Tool):
    def __init__(self):
        super().__init__(
            name="diff",
            title="diff",
            description="Compute dt2 - dt1. Returns seconds, and a simple breakdown.",
            input_schema={
                "type": "object",
                "required": ["dt1", "dt2", "assume_tz"],
                "additionalProperties": False,
                "properties": {
                    "dt1": {"type": "string", "description": "ISO datetime 1."},
                    "dt2": {"type": "string", "description": "ISO datetime 2."},
                    "assume_tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. If either input is naive, interpret it in this timezone (default UTC).",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        dt1: str,
        dt2: str,
        assume_tz: Optional[str] = None,
    ):
        a = _parse_iso(dt1, assume_tz=assume_tz)
        b = _parse_iso(dt2, assume_tz=assume_tz)
        delta = b - a
        total_seconds = int(delta.total_seconds())
        sign = -1 if total_seconds < 0 else 1
        secs = abs(total_seconds)

        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        return {
            "seconds": total_seconds,
            "breakdown": {
                "sign": sign,
                "days": days,
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
            },
        }


class ParseTool(Tool):
    def __init__(self):
        super().__init__(
            name="parse_iso",
            title="parse iso datetime",
            description="Parse an ISO datetime string and return normalized ISO plus components.",
            input_schema={
                "type": "object",
                "required": ["dt", "assume_tz", "tz"],
                "additionalProperties": False,
                "properties": {
                    "dt": {
                        "type": "string",
                        "description": "ISO datetime (accepts trailing 'Z').",
                    },
                    "assume_tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. If dt is naive, interpret it in this timezone (default UTC).",
                    },
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. Convert output to this timezone (default keep parsed tz).",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        dt: str,
        assume_tz: Optional[str] = None,
        tz: Optional[str] = None,
    ):
        parsed = _parse_iso(dt, assume_tz=assume_tz)
        if tz:
            parsed = _to_tz(parsed, tz)
        iso_year, iso_week, iso_wday = parsed.isocalendar()
        return {
            "iso": _iso(parsed),
            "components": {
                "year": parsed.year,
                "month": parsed.month,
                "day": parsed.day,
                "hour": parsed.hour,
                "minute": parsed.minute,
                "second": parsed.second,
                "microsecond": parsed.microsecond,
            },
            "weekday": parsed.weekday(),  # Mon=0..Sun=6
            "iso_week": {"year": iso_year, "week": iso_week, "weekday": iso_wday},
            "tz": str(parsed.tzinfo),
        }


class FormatTool(Tool):
    def __init__(self):
        super().__init__(
            name="format_dt",
            title="format datetime",
            description="Format an ISO datetime using strftime. (Use for human-readable strings.)",
            input_schema={
                "type": "object",
                "required": ["dt", "fmt", "assume_tz", "tz"],
                "additionalProperties": False,
                "properties": {
                    "dt": {"type": "string", "description": "ISO datetime."},
                    "fmt": {
                        "type": "string",
                        "description": "strftime format string, e.g. '%Y-%m-%d %H:%M:%S'.",
                    },
                    "assume_tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. If dt is naive, interpret it in this timezone (default UTC).",
                    },
                    "tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. Convert before formatting (default: keep).",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        dt: str,
        fmt: str,
        assume_tz: Optional[str] = None,
        tz: Optional[str] = None,
    ):
        parsed = _parse_iso(dt, assume_tz=assume_tz)
        if tz:
            parsed = _to_tz(parsed, tz)
        return {"text": parsed.strftime(fmt)}


class UtcZTool(Tool):
    def __init__(self):
        super().__init__(
            name="to_utc_z",
            title="to utc Z",
            description="Convert an ISO datetime to UTC and return an RFC3339-ish Z string (e.g. 2026-01-11T12:34:56Z).",
            input_schema={
                "type": "object",
                "required": ["dt", "assume_tz", "drop_microseconds"],
                "additionalProperties": False,
                "properties": {
                    "dt": {"type": "string", "description": "ISO datetime."},
                    "assume_tz": {
                        "type": ["string", "null"],
                        "description": "IANA timezone name. If dt is naive, interpret it in this timezone (default UTC).",
                    },
                    "drop_microseconds": {
                        "type": "boolean",
                        "description": "Default true.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        dt: str,
        assume_tz: Optional[str] = None,
        drop_microseconds: bool = True,
    ):
        parsed = _parse_iso(dt, assume_tz=assume_tz)
        u = parsed.astimezone(timezone.utc)
        if drop_microseconds:
            u = u.replace(microsecond=0)
        # emit Z
        s = u.isoformat().replace("+00:00", "Z")
        return {"dt": s}


# ----------------------------
# Toolkit
# ----------------------------


class DatetimeToolkit(RemoteToolkit):
    def __init__(self):
        tools = [
            NowTool(),
            TodayTool(),
            WeekRangeTool(),
            MonthRangeTool(),
            AddDurationTool(),
            DiffTool(),
            ParseTool(),
            FormatTool(),
            UtcZTool(),
        ]
        super().__init__(
            name="datetime",
            title="datetime",
            description="Useful datetime utilities: now/ranges/parse/format/add/diff",
            tools=tools,
        )


class DatetimeToolkitConfig(ToolkitConfig):
    name: Literal["datetime"] = "datetime"


class DatetimeToolkitBuilder(ToolkitBuilder):
    def __init__(self):
        super().__init__(name="datetime", type=DatetimeToolkitConfig)

    async def make(
        self, *, room: RoomClient, model: str, config: DatetimeToolkitConfig
    ) -> Toolkit:
        # no room dependency required; purely local computations
        return DatetimeToolkit()
