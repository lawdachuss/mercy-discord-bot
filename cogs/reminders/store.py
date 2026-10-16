"""Scheduling engine and persistence layer for the reminder system.

Everything that is pure logic - parsing durations and wall-clock times,
resolving timezones and computing the next occurrence of a schedule - lives in
the top half of this module so it can be unit tested without a Discord
connection or a MongoDB server. :class:`ReminderStore` in the bottom half is a
thin wrapper around a Motor collection.

Time handling rules used everywhere in this package:

* Every datetime written to MongoDB is timezone aware and in UTC.
* Motor returns timestamps as *naive* UTC datetimes, so anything read back is
  passed through :func:`as_utc` before it is compared or displayed.
* Human input ("18:30", "2h30m", "2026-10-20 18:30") is interpreted in the
  reminder's own timezone and converted to UTC for storage.
"""
from __future__ import annotations

import calendar
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ReturnDocument

from cogs.reminders import config

logger = logging.getLogger(__name__)

UTC = timezone.utc

__all__ = [
    "UTC",
    "as_utc",
    "parse_duration",
    "format_duration",
    "parse_time_of_day",
    "format_time_of_day",
    "parse_absolute_datetime",
    "resolve_timezone",
    "localize",
    "next_occurrence",
    "schedule_type_of",
    "validate_schedule",
    "describe_schedule",
    "format_local",
    "relative_delta",
    "discord_timestamp",
    "new_reminder_id",
    "ReminderDraft",
    "ReminderStore",
]


# ---------------------------------------------------------------------------
# Tiny time helpers
# ---------------------------------------------------------------------------


def as_utc(value: Any) -> Optional[datetime]:
    """Return ``value`` as an aware UTC datetime.

    Motor hands back naive datetimes that are already UTC; treating them as
    local time (or crashing on the comparison) is the classic source of
    off-by-hours reminder bugs, so every read goes through here.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return None


# Longest units first so "months" matches ``months`` and not ``m`` + "onths",
# and a lookahead (not ``\b``) after the unit so compound values such as
# "2h30m" still match: ``\b`` would fail between "h" and "3" because both are
# word characters, silently turning "2h30m" into "30m".
_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>minutes|minute|mins|min|seconds|second|secs|sec|hours|hour|hrs|hr"
    r"|days|day|weeks|week|wks|wk|months|month|mo|s|m|h|d|w)(?![a-z])",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "wks": 604800,
    "week": 604800,
    "weeks": 604800,
    "mo": 2592000,
    "month": 2592000,
    "months": 2592000,
}


def parse_duration(text: str) -> int:
    """Parse ``"2h30m"``, ``"1 day 4 hours"`` or ``"in 90m"`` into seconds.

    Raises :class:`ValueError` with a user facing message when the string is
    not a duration or is not positive.
    """
    if text is None or not str(text).strip():
        raise ValueError("Enter a duration such as `30m`, `2h`, `1d 6h` or `in 90m`.")

    cleaned = str(text).strip().lower()
    for prefix in ("in ", "+"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()

    matches = list(_DURATION_RE.finditer(cleaned))
    if not matches:
        raise ValueError(
            f"Could not read `{text}` as a duration. Try `30m`, `2h`, `1d 6h` or `90 seconds`."
        )

    # Reject anything that is not purely made of duration tokens, so "30m
    # please" or "every 5m" cannot silently mean something else.
    remainder = _DURATION_RE.sub("", cleaned)
    remainder = remainder.replace(",", "").replace("and", "").strip()
    if remainder:
        raise ValueError(
            f"Could not read `{text}` as a duration (unexpected `{remainder}`). "
            "Use forms like `30m`, `2h`, `1d 6h`."
        )

    total = 0.0
    for match in matches:
        total += float(match.group("value")) * _UNIT_SECONDS[match.group("unit").lower()]

    seconds = int(round(total))
    if seconds <= 0:
        raise ValueError("The duration must be greater than zero.")
    return seconds


def format_duration(seconds: Any) -> str:
    """Render a number of seconds as ``"1d 4h 30m"`` (or ``"45s"``)."""
    try:
        remaining = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "unknown"
    if remaining <= 0:
        return "0s"

    parts: List[str] = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if remaining >= size:
            value, remaining = divmod(remaining, size)
            parts.append(f"{value}{label}")
        if len(parts) == 2:
            break
    return " ".join(parts) if parts else f"{remaining}s"


_TIME_RE = re.compile(r"^(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?$", re.IGNORECASE)


def parse_time_of_day(text: str) -> Tuple[int, int]:
    """Parse ``"18:30"``, ``"6pm"`` or ``"7:05 AM"`` into ``(hour, minute)``."""
    if text is None or not str(text).strip():
        raise ValueError("Enter a time such as `18:30`, `9:00` or `6pm`.")

    match = _TIME_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"Could not read `{text}` as a time. Use 24h `HH:MM` or forms like `6pm`.")

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()

    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"`{text}` is not a valid 12-hour time (hours must be 1-12).")
        hour = hour % 12
        if meridiem == "pm":
            hour += 12

    if not 0 <= hour <= 23:
        raise ValueError(f"`{text}` is not a valid time (hours must be 0-23).")
    if not 0 <= minute <= 59:
        raise ValueError(f"`{text}` is not a valid time (minutes must be 0-59).")
    return hour, minute


def format_time_of_day(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


_EXTRA_DATE_FORMATS = (
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%Y/%m/%d %H:%M",
    "%d %b %Y %H:%M",
    "%d %B %Y %H:%M",
    "%b %d %Y %H:%M",
)


def parse_absolute_datetime(text: str, tz: ZoneInfo, now: Optional[datetime] = None) -> datetime:
    """Parse a date/time typed by an admin into an aware datetime in ``tz``.

    Accepts ISO-ish absolute values (``2026-10-20 18:30``, ``2026-10-20T18:30``),
    a few human formats (``20-10-2026 18:30``) and relative offsets
    (``in 2h30m``). A value without an offset is interpreted in ``tz``.
    """
    if text is None or not str(text).strip():
        raise ValueError("Enter a date and time, e.g. `2026-10-20 18:30`, or an offset like `in 2h30m`.")

    cleaned = str(text).strip()
    lowered = cleaned.lower()
    reference = as_utc(now) or datetime.now(UTC)

    if lowered.startswith("in ") or cleaned.startswith("+"):
        seconds = parse_duration(cleaned)
        return (reference + timedelta(seconds=seconds)).astimezone(tz)

    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in _EXTRA_DATE_FORMATS:
            try:
                parsed = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(
            f"Could not read `{text}` as a date and time. Use `YYYY-MM-DD HH:MM` "
            "or a relative offset like `in 2h30m`."
        )

    if parsed.tzinfo is None:
        return localize(parsed, tz)
    return parsed.astimezone(tz)


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve a timezone name (or shorthand such as ``IST``) to a ``ZoneInfo``."""
    if not name or not str(name).strip():
        raise ValueError("A timezone is required.")

    cleaned = str(name).strip()
    candidate = config.TIMEZONE_ALIASES.get(cleaned.lower(), cleaned)
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(
            f"`{cleaned}` is not a known timezone. Use an IANA name such as "
            "`Asia/Kolkata`, `Europe/London` or `America/New_York`."
        ) from None


def localize(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach ``tz`` to a naive wall-clock datetime.

    During a DST spring-forward gap the requested wall-clock time does not
    exist; Python resolves it with the pre-transition offset, which maps to a
    real instant an hour later. That is the behaviour we want (the reminder
    still fires) rather than raising.
    """
    if naive.tzinfo is None:
        return naive.replace(tzinfo=tz)
    return naive.astimezone(tz)


# ---------------------------------------------------------------------------
# Schedule engine
# ---------------------------------------------------------------------------


def _schedule_type(schedule: Dict[str, Any]) -> str:
    value = str(schedule.get("schedule_type") or config.SCHEDULE_ONCE).lower()
    return value if value in config.SCHEDULE_TYPES else config.SCHEDULE_ONCE


def schedule_type_of(reminder: Dict[str, Any]) -> str:
    """Public wrapper around the schedule-type normalisation."""
    return _schedule_type(reminder)


def next_occurrence(
    schedule: Dict[str, Any],
    now: datetime,
    anchor: Optional[datetime] = None,
) -> Optional[datetime]:
    """Compute the next UTC fire time of ``schedule``, strictly after ``now``.

    ``anchor`` is the previously scheduled occurrence. It keeps a repeating
    interval aligned to its original cadence, so a bot that was offline for six
    hours does not silently shift a "every 2h" reminder forever.

    Returns ``None`` when there is nothing left to schedule (a one-off, an
    empty weekday list, or an unparsable schedule).
    """
    now_utc = as_utc(now)
    if now_utc is None:
        return None

    schedule_type = _schedule_type(schedule)

    if schedule_type == config.SCHEDULE_ONCE:
        return None

    try:
        tz = resolve_timezone(schedule.get("timezone") or config.DEFAULT_TIMEZONE)
    except ValueError:
        return None

    if schedule_type == config.SCHEDULE_INTERVAL:
        interval = int(schedule.get("interval_seconds") or 0)
        if interval < 1:
            return None
        base = as_utc(anchor) or now_utc
        if base <= now_utc:
            # Skip whole intervals so exactly one catch-up delivery happens and
            # the cadence stays aligned to the original anchor.
            missed = int((now_utc - base).total_seconds() // interval) + 1
            base = base + timedelta(seconds=interval * missed)
        return base

    hour = int(schedule.get("hour") or 0)
    minute = int(schedule.get("minute") or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    local_now = now_utc.astimezone(tz)
    today: date = local_now.date()

    if schedule_type == config.SCHEDULE_DAILY:
        for offset in range(0, 3):
            day = today + timedelta(days=offset)
            candidate = localize(datetime(day.year, day.month, day.day, hour, minute), tz)
            if candidate > now_utc:
                return candidate.astimezone(UTC)
        return None

    if schedule_type == config.SCHEDULE_WEEKLY:
        weekdays = sorted({int(day) for day in (schedule.get("weekdays") or []) if 0 <= int(day) <= 6})
        if not weekdays:
            return None
        for offset in range(0, 8):
            day = today + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            candidate = localize(datetime(day.year, day.month, day.day, hour, minute), tz)
            if candidate > now_utc:
                return candidate.astimezone(UTC)
        return None

    if schedule_type == config.SCHEDULE_MONTHLY:
        day_of_month = int(schedule.get("day_of_month") or 1)
        if not 1 <= day_of_month <= 31:
            return None
        year, month = local_now.year, local_now.month
        for _ in range(0, 15):
            last_day = calendar.monthrange(year, month)[1]
            # Clamp so "the 31st" still fires in a 30 day month (and February).
            effective_day = min(day_of_month, last_day)
            candidate = localize(datetime(year, month, effective_day, hour, minute), tz)
            if candidate > now_utc:
                return candidate.astimezone(UTC)
            month += 1
            if month > 12:
                month = 1
                year += 1
        return None

    return None


def validate_schedule(schedule: Dict[str, Any], now: datetime) -> Optional[str]:
    """Validate a schedule dict, returning a friendly error or ``None``."""
    schedule_type = _schedule_type(schedule)
    now_utc = as_utc(now) or datetime.now(UTC)

    if schedule_type == config.SCHEDULE_ONCE:
        once_at = as_utc(schedule.get("once_at"))
        if once_at is None:
            return "❌ This reminder has no date and time yet."
        if once_at <= now_utc + timedelta(seconds=30):
            return "❌ That time has already passed — pick a moment in the future."
        return None

    try:
        resolve_timezone(schedule.get("timezone") or config.DEFAULT_TIMEZONE)
    except ValueError as exc:
        return f"❌ {exc}"

    if schedule_type == config.SCHEDULE_INTERVAL:
        interval = int(schedule.get("interval_seconds") or 0)
        if interval < config.MIN_INTERVAL_SECONDS:
            return (
                f"❌ The interval must be at least **{format_duration(config.MIN_INTERVAL_SECONDS)}**. "
                "Shorter reminders are treated as spam by Discord."
            )
        if interval > config.MAX_INTERVAL_SECONDS:
            return f"❌ The interval cannot be longer than **{format_duration(config.MAX_INTERVAL_SECONDS)}**."
        return None

    hour = schedule.get("hour")
    minute = schedule.get("minute")
    if hour is None or minute is None:
        return "❌ Pick a time of day for this reminder."
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        return "❌ That time of day is not valid."

    if schedule_type == config.SCHEDULE_WEEKLY and not (schedule.get("weekdays") or []):
        return "❌ Pick at least one weekday."

    if schedule_type == config.SCHEDULE_MONTHLY:
        day_of_month = int(schedule.get("day_of_month") or 0)
        if not 1 <= day_of_month <= 31:
            return "❌ Pick a day of the month between 1 and 31."

    if next_occurrence(schedule, now_utc) is None:
        return "❌ I could not work out when this reminder should next run."

    return None


def describe_schedule(reminder: Dict[str, Any]) -> str:
    """One line, human description of when a reminder runs."""
    schedule_type = _schedule_type(reminder)
    tz = reminder.get("timezone") or config.DEFAULT_TIMEZONE

    if schedule_type == config.SCHEDULE_ONCE:
        once_at = as_utc(reminder.get("once_at"))
        if once_at is None:
            return "Once (no time set)"
        return f"Once on {format_local(once_at, tz)} ({tz})"

    if schedule_type == config.SCHEDULE_INTERVAL:
        return f"Every {format_duration(reminder.get('interval_seconds'))}"

    at = format_time_of_day(reminder.get("hour") or 0, reminder.get("minute") or 0)

    if schedule_type == config.SCHEDULE_DAILY:
        return f"Daily at {at} ({tz})"

    if schedule_type == config.SCHEDULE_WEEKLY:
        weekdays = sorted({int(day) for day in (reminder.get("weekdays") or [])})
        names = ", ".join(config.WEEKDAY_SHORT[day] for day in weekdays if 0 <= day <= 6) or "no days set"
        return f"Weekly on {names} at {at} ({tz})"

    if schedule_type == config.SCHEDULE_MONTHLY:
        day_of_month = int(reminder.get("day_of_month") or 1)
        suffix = "th" if 10 <= day_of_month % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day_of_month % 10, "th")
        return f"Monthly on the {day_of_month}{suffix} at {at} ({tz})"

    return "Unknown schedule"


def format_local(value: datetime, timezone_name: str = config.DEFAULT_TIMEZONE) -> str:
    """Render a datetime in a reminder's timezone, e.g. ``Tue 20 Oct 2026, 18:30``."""
    moment = as_utc(value)
    if moment is None:
        return "unknown"
    try:
        tz = resolve_timezone(timezone_name)
    except ValueError:
        tz = ZoneInfo(config.DEFAULT_TIMEZONE)
    return moment.astimezone(tz).strftime("%a %d %b %Y, %H:%M")


def relative_delta(value: datetime, now: Optional[datetime] = None) -> str:
    """``"in 3h 12m"`` / ``"3h ago"`` for a datetime."""
    moment = as_utc(value)
    if moment is None:
        return "unknown"
    reference = as_utc(now) or datetime.now(UTC)
    seconds = (moment - reference).total_seconds()
    if abs(seconds) < 45:
        return "in a moment" if seconds >= 0 else "just now"
    if seconds > 0:
        return f"in {format_duration(seconds)}"
    return f"{format_duration(-seconds)} ago"


def discord_timestamp(value: datetime, style: str = "f") -> str:
    """A Discord ``<t:...>`` timestamp so every member sees their own local time."""
    moment = as_utc(value)
    if moment is None:
        return "`not set`"
    return f"<t:{int(moment.timestamp())}:{style}>"


def new_reminder_id() -> str:
    """Short, human friendly identifier (also used in autocomplete)."""
    return uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Draft used by the interactive setup wizard
# ---------------------------------------------------------------------------


class ReminderDraft:
    """Mutable state for one pass through the interactive wizard."""

    __slots__ = (
        "flow",
        "reminder_id",
        "guild_id",
        "author_id",
        "name",
        "message",
        "schedule_type",
        "hour",
        "minute",
        "weekdays",
        "day_of_month",
        "once_at",
        "interval_seconds",
        "timezone",
        "channel_id",
        "role_id",
        "page",
        "note",
    )

    def __init__(
        self,
        *,
        guild_id: int,
        author_id: int,
        flow: str = "create",
        reminder_id: Optional[str] = None,
    ) -> None:
        self.flow = flow
        self.reminder_id = reminder_id
        self.guild_id = guild_id
        self.author_id = author_id
        self.name: Optional[str] = None
        self.message: Optional[str] = None
        self.schedule_type: str = config.SCHEDULE_ONCE
        self.hour: Optional[int] = None
        self.minute: Optional[int] = None
        self.weekdays: List[int] = []
        self.day_of_month: Optional[int] = None
        self.once_at: Optional[datetime] = None
        self.interval_seconds: Optional[int] = None
        self.timezone: str = config.DEFAULT_TIMEZONE
        self.channel_id: Optional[int] = None
        self.role_id: Optional[int] = None
        self.page: int = 0
        self.note: str = ""

    @classmethod
    def from_reminder(cls, reminder: Dict[str, Any], *, flow: str = "edit_time") -> "ReminderDraft":
        draft = cls(
            guild_id=int(reminder.get("guild_id") or 0),
            author_id=int(reminder.get("created_by") or 0),
            flow=flow,
            reminder_id=reminder.get("reminder_id"),
        )
        draft.name = reminder.get("name")
        draft.message = reminder.get("message")
        draft.schedule_type = _schedule_type(reminder)
        draft.hour = reminder.get("hour")
        draft.minute = reminder.get("minute")
        draft.weekdays = sorted({int(day) for day in (reminder.get("weekdays") or [])})
        draft.day_of_month = reminder.get("day_of_month")
        draft.once_at = as_utc(reminder.get("once_at"))
        draft.interval_seconds = reminder.get("interval_seconds")
        draft.timezone = reminder.get("timezone") or config.DEFAULT_TIMEZONE
        draft.channel_id = reminder.get("channel_id")
        draft.role_id = reminder.get("role_id")
        return draft

    def schedule(self) -> Dict[str, Any]:
        """The schedule portion of the document, ready for validation/storage."""
        return {
            "schedule_type": self.schedule_type,
            "hour": self.hour,
            "minute": self.minute,
            "weekdays": list(self.weekdays),
            "day_of_month": self.day_of_month,
            "once_at": self.once_at,
            "interval_seconds": self.interval_seconds,
            "timezone": self.timezone,
        }

    def needs_timezone(self) -> bool:
        """An interval reminder is a pure duration, so a timezone is optional."""
        return self.schedule_type != config.SCHEDULE_INTERVAL


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ReminderStore:
    """CRUD + atomic claiming for the ``reminders`` collection."""

    def __init__(self, collection: Any) -> None:
        self.collection = collection

    # -- indexes ----------------------------------------------------------

    async def ensure_indexes(self) -> None:
        """Create the indexes the scheduler relies on (idempotent)."""
        await self.collection.create_index(
            [("guild_id", 1), ("reminder_id", 1)], unique=True, name="guild_reminder"
        )
        await self.collection.create_index(
            [("status", 1), ("next_run_at", 1)], name="due_reminders"
        )
        await self.collection.create_index(
            [("guild_id", 1), ("created_at", -1)], name="guild_recent"
        )

    # -- reads ------------------------------------------------------------

    async def get(self, guild_id: int, reminder_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({"guild_id": int(guild_id), "reminder_id": str(reminder_id)})

    async def list_for_guild(self, guild_id: int, limit: int = config.MAX_REMINDERS_PER_GUILD) -> List[Dict[str, Any]]:
        cursor = (
            self.collection.find({"guild_id": int(guild_id)})
            .sort([("status", 1), ("next_run_at", 1)])
            .limit(int(limit))
        )
        return await cursor.to_list(length=int(limit))

    async def search(self, guild_id: int, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Cheap lookup used by slash-command autocomplete."""
        filter_doc: Dict[str, Any] = {"guild_id": int(guild_id)}
        if query:
            escaped = re.escape(str(query)[:40])
            filter_doc["$or"] = [
                {"name": {"$regex": escaped, "$options": "i"}},
                {"reminder_id": {"$regex": escaped, "$options": "i"}},
            ]
        cursor = self.collection.find(filter_doc).sort([("next_run_at", 1), ("name", 1)]).limit(int(limit))
        return await cursor.to_list(length=int(limit))

    async def count_for_guild(self, guild_id: int) -> int:
        return int(await self.collection.count_documents({"guild_id": int(guild_id)}))

    async def stats_for_guild(self, guild_id: int) -> Dict[str, int]:
        stats = {"total": 0, "active": 0, "paused": 0, "failed": 0, "completed": 0}
        cursor = self.collection.aggregate(
            [
                {"$match": {"guild_id": int(guild_id)}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        async for row in cursor:
            status = str(row.get("_id") or "unknown")
            count = int(row.get("count") or 0)
            stats["total"] += count
            if status in stats:
                stats[status] = count
        return stats

    # -- writes -----------------------------------------------------------

    async def insert(self, document: Dict[str, Any]) -> None:
        await self.collection.insert_one(document)

    async def update(self, guild_id: int, reminder_id: str, patch: Dict[str, Any]) -> bool:
        patch = dict(patch)
        patch["updated_at"] = datetime.now(UTC)
        result = await self.collection.update_one(
            {"guild_id": int(guild_id), "reminder_id": str(reminder_id)},
            {"$set": patch},
        )
        return bool(result.matched_count)

    async def delete(self, guild_id: int, reminder_id: str) -> bool:
        result = await self.collection.delete_one(
            {"guild_id": int(guild_id), "reminder_id": str(reminder_id)}
        )
        return bool(result.deleted_count)

    async def set_status(self, guild_id: int, reminder_id: str, status: str, **extra: Any) -> bool:
        patch: Dict[str, Any] = {"status": status}
        patch.update(extra)
        return await self.update(guild_id, reminder_id, patch)

    async def set_status_for_all(self, guild_id: int, status: str, *, only_statuses: Sequence[str] = ("active",)) -> int:
        """Bulk pause/resume. Keeps scheduling fields untouched on resume."""
        patch: Dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
        filter_doc: Dict[str, Any] = {"guild_id": int(guild_id)}
        if only_statuses:
            filter_doc["status"] = {"$in": list(only_statuses)}
        result = await self.collection.update_many(filter_doc, {"$set": patch})
        return int(result.modified_count)

    # -- scheduler --------------------------------------------------------

    def _claim_update(self, reminder: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """Build the update that claims one occurrence of ``reminder``."""
        occurrence = as_utc(reminder.get("next_run_at"))
        update: Dict[str, Any] = {
            "$set": {
                "claimed_at": now,
                "last_claimed_for": occurrence,
                "last_run_key": occurrence.isoformat() if occurrence else None,
            }
        }

        if _schedule_type(reminder) == config.SCHEDULE_ONCE:
            # A one-off is claimed by marking it completed. It can therefore
            # never be delivered twice, even across restarts.
            update["$set"]["status"] = "completed"
            return update

        following = next_occurrence(reminder, now, anchor=occurrence)
        if following is None:
            update["$set"]["status"] = "failed"
            update["$set"]["last_error"] = "Could not work out the next run time; the schedule is invalid."
            return update

        update["$set"]["next_run_at"] = following
        return update

    async def claim_due(self, now: datetime, limit: int = config.MAX_REMINDERS_PER_TICK) -> List[Dict[str, Any]]:
        """Atomically claim everything that is due, newest schedule first.

        A claim rewrites ``next_run_at`` in the *same* update that matches the
        previous value, so two scheduler passes - or two bot processes sharing
        the database - can never deliver the same occurrence twice. That
        compare-and-set is what makes duplicate notifications impossible
        instead of merely unlikely.
        """
        now_utc = as_utc(now) or datetime.now(UTC)
        cursor = (
            self.collection.find({"status": "active", "next_run_at": {"$lte": now_utc}})
            .sort([("next_run_at", 1)])
            .limit(int(limit))
        )
        due = await cursor.to_list(length=int(limit))

        claimed: List[Dict[str, Any]] = []
        for reminder in due:
            update = self._claim_update(reminder, now_utc)
            claimed_reminder = await self.collection.find_one_and_update(
                {
                    "_id": reminder["_id"],
                    "status": reminder.get("status", "active"),
                    "next_run_at": reminder.get("next_run_at"),
                },
                update,
                return_document=ReturnDocument.AFTER,
            )
            if claimed_reminder is not None:
                claimed.append(claimed_reminder)
        return claimed

    async def record_success(self, reminder: Dict[str, Any]) -> None:
        await self.collection.update_one(
            {"_id": reminder["_id"]},
            {
                "$set": {
                    "failure_count": 0,
                    "last_error": None,
                    "last_delivered_at": datetime.now(UTC),
                },
                "$inc": {"delivered_count": 1},
            },
        )

    async def record_failure(
        self,
        reminder: Dict[str, Any],
        error: str,
        *,
        pause: bool = False,
        retry_at: Optional[datetime] = None,
    ) -> None:
        """Record a failed delivery.

        ``retry_at`` puts a one-off reminder back on the schedule for another
        attempt; ``pause`` stops the reminder entirely (permissions will not fix
        themselves, so retrying forever would just spam the log).
        """
        patch: Dict[str, Any] = {
            "last_error": str(error)[:400],
            "last_failed_at": datetime.now(UTC),
        }
        if pause:
            patch["status"] = "failed"
        elif retry_at is not None:
            patch["status"] = "active"
            patch["next_run_at"] = as_utc(retry_at)
            patch["claimed_at"] = None

        await self.collection.update_one(
            {"_id": reminder["_id"]},
            {"$set": patch, "$inc": {"failure_count": 1}},
        )
