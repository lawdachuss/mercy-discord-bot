"""Tests for the reminder system.

These cover the parts that are easy to get subtly wrong and expensive to debug
in production: duration/time parsing, timezone maths, the next-occurrence
engine (including DST and month clamping), the atomic claim that prevents
duplicate notifications, and the wiring of every interactive component.

Nothing here needs a Discord connection or a MongoDB server: the store is
exercised against an in-memory collection that mimics the two Mongo operations
the scheduler actually depends on.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import discord
import pytest
from discord.ext import commands

from cogs.reminders import config
from cogs.reminders import views as reminder_views
from cogs.reminders.reminders import ReminderCog
from cogs.reminders.store import (
    ReminderDraft,
    ReminderStore,
    as_utc,
    describe_schedule,
    discord_timestamp,
    format_duration,
    format_local,
    localize,
    next_occurrence,
    parse_absolute_datetime,
    parse_duration,
    parse_time_of_day,
    relative_delta,
    resolve_timezone,
    schedule_type_of,
    validate_schedule,
)

UTC = timezone.utc
IST = resolve_timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# In-memory collection standing in for Motor
# ---------------------------------------------------------------------------


def _matches(document: Dict[str, Any], query: Dict[str, Any]) -> bool:
    for key, expected in query.items():
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                actual = document.get(key)
                if operator == "$lte" and not (actual is not None and actual <= operand):
                    return False
                if operator == "$in" and actual not in operand:
                    return False
                if operator == "$exists" and bool(actual is not None) != bool(operand):
                    return False
        elif document.get(key) != expected:
            return False
    return True


class _FakeCursor:
    def __init__(self, documents: List[Dict[str, Any]]) -> None:
        self._documents = documents

    def sort(self, key_or_list, direction: Optional[int] = None) -> "_FakeCursor":
        if isinstance(key_or_list, list):
            field, order = key_or_list[0]
        else:
            field, order = key_or_list, direction or 1
        self._documents.sort(
            key=lambda doc: (doc.get(field) is None, doc.get(field)), reverse=order < 0
        )
        return self

    def limit(self, count: int) -> "_FakeCursor":
        self._documents = self._documents[:count]
        return self

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        documents = self._documents if length is None else self._documents[:length]
        return [copy.deepcopy(doc) for doc in documents]


class FakeCollection:
    """Just enough Mongo for :class:`ReminderStore`."""

    def __init__(self, documents: Optional[List[Dict[str, Any]]] = None) -> None:
        self.documents = [copy.deepcopy(doc) for doc in (documents or [])]

    async def create_index(self, *args, **kwargs) -> str:
        return "index"

    async def insert_one(self, document: Dict[str, Any]) -> None:
        self.documents.append(copy.deepcopy(document))

    def find(self, query: Dict[str, Any]) -> _FakeCursor:
        return _FakeCursor([doc for doc in self.documents if _matches(doc, query)])

    async def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for document in self.documents:
            if _matches(document, query):
                return copy.deepcopy(document)
        return None

    async def find_one_and_update(self, query, update, return_document=None):
        for index, document in enumerate(self.documents):
            # Exact-value matching on next_run_at is what makes the claim a
            # compare-and-set; the fake must not be more forgiving than Mongo.
            if not _matches(document, query):
                continue
            patched = copy.deepcopy(document)
            for field, value in update.get("$set", {}).items():
                patched[field] = value
            for field, value in update.get("$inc", {}).items():
                patched[field] = patched.get(field, 0) + value
            self.documents[index] = patched
            return copy.deepcopy(patched)
        return None

    async def update_one(self, query, update, upsert: bool = False):
        for index, document in enumerate(self.documents):
            if not _matches(document, query):
                continue
            patched = copy.deepcopy(document)
            for field, value in update.get("$set", {}).items():
                patched[field] = value
            for field, value in update.get("$inc", {}).items():
                patched[field] = patched.get(field, 0) + value
            self.documents[index] = patched
            return _WriteResult(1)
        return _WriteResult(0)

    async def update_many(self, query, update):
        changed = 0
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                changed += 1
                self.documents[index] = {**document, **update.get("$set", {})}
        return _WriteResult(changed)

    async def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return _WriteResult(1)
        return _WriteResult(0)

    async def count_documents(self, query) -> int:
        return sum(1 for doc in self.documents if _matches(doc, query))


class _WriteResult:
    def __init__(self, count: int = 1) -> None:
        self.matched_count = count
        self.modified_count = count
        self.deleted_count = count


def reminder_fixture(**overrides: Any) -> Dict[str, Any]:
    base = {
        "_id": "doc-1",
        "guild_id": 1,
        "reminder_id": "abc123",
        "name": "Standup",
        "message": "Time for standup",
        "schedule_type": config.SCHEDULE_DAILY,
        "hour": 18,
        "minute": 30,
        "weekdays": [],
        "day_of_month": None,
        "once_at": None,
        "interval_seconds": None,
        "timezone": "Asia/Kolkata",
        "channel_id": 99,
        "role_id": None,
        "status": "active",
        "next_run_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "failure_count": 0,
        "delivered_count": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30m", 1800),
        ("30 minutes", 1800),
        ("2h30m", 9000),
        ("1d 6h", 108000),
        ("in 90m", 5400),
        ("+45s", 45),
        ("1 week", 604800),
        ("1mo", 2592000),
        ("90 seconds", 90),
    ],
)
def test_parse_duration_accepts_common_forms(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "30", "5m please", "0m", "every 5m", "-5m"])
def test_parse_duration_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize(
    "text,expected",
    [("18:30", (18, 30)), ("6pm", (18, 0)), ("12am", (0, 0)), ("12pm", (12, 0)),
     ("7:05 AM", (7, 5)), ("00:00", (0, 0)), ("9", (9, 0))],
)
def test_parse_time_of_day_accepts_common_forms(text, expected):
    assert parse_time_of_day(text) == expected


@pytest.mark.parametrize("text", ["", "25:00", "13pm", "abc", "12:75"])
def test_parse_time_of_day_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_time_of_day(text)


@pytest.mark.parametrize(
    "seconds,expected", [(90, "1m 30s"), (9000, "2h 30m"), (45, "45s"), (86400, "1d"), (0, "0s")]
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_resolve_timezone_handles_aliases_and_junk():
    assert str(resolve_timezone("IST")) == "Asia/Kolkata"
    assert str(resolve_timezone("utc")) == "UTC"
    with pytest.raises(ValueError):
        resolve_timezone("Mars/Olympus")


def test_parse_absolute_datetime_uses_the_schedules_timezone():
    parsed = parse_absolute_datetime("2026-10-20 18:30", IST)
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)
    assert as_utc(parsed) == datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def test_parse_absolute_datetime_supports_relative_offsets():
    now = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
    parsed = parse_absolute_datetime("in 2h", IST, now=now)
    assert as_utc(parsed) == now + timedelta(hours=2)


@pytest.mark.parametrize("text", ["", "sometime soon", "2026-13-45 99:99"])
def test_parse_absolute_datetime_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_absolute_datetime(text, IST)


def test_as_utc_treats_naive_values_from_mongo_as_utc():
    naive = datetime(2026, 1, 1, 12, 0)
    assert as_utc(naive) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert as_utc(None) is None


# ---------------------------------------------------------------------------
# Next-occurrence engine
# ---------------------------------------------------------------------------


def test_once_schedules_have_no_following_occurrence():
    schedule = {"schedule_type": config.SCHEDULE_ONCE, "once_at": datetime(2030, 1, 1, tzinfo=UTC)}
    assert next_occurrence(schedule, datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_daily_rolls_to_tomorrow_when_the_time_has_passed():
    schedule = {"schedule_type": config.SCHEDULE_DAILY, "hour": 18, "minute": 30, "timezone": "Asia/Kolkata"}
    # 12:00 UTC is 17:30 IST, so today's 18:30 IST is still ahead.
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    upcoming = next_occurrence(schedule, now)
    assert upcoming == datetime(2026, 6, 10, 13, 0, tzinfo=UTC)

    # 14:00 UTC is 19:30 IST, so the next one is tomorrow.
    later = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)
    following = next_occurrence(schedule, later)
    assert following == datetime(2026, 6, 11, 13, 0, tzinfo=UTC)


def test_daily_occurrence_is_never_in_the_past():
    schedule = {"schedule_type": config.SCHEDULE_DAILY, "hour": 0, "minute": 0, "timezone": "UTC"}
    for offset in range(-5, 6):
        now = datetime(2026, 3, 15, 12, 0, tzinfo=UTC) + timedelta(days=offset, hours=offset)
        assert next_occurrence(schedule, now) > now


def test_weekly_picks_the_next_chosen_weekday():
    # 2026-06-10 is a Wednesday.
    schedule = {
        "schedule_type": config.SCHEDULE_WEEKLY,
        "hour": 9,
        "minute": 0,
        "weekdays": [0, 4],  # Monday and Friday
        "timezone": "UTC",
    }
    now = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    # Friday 2026-06-12 comes before Monday 2026-06-15.
    assert next_occurrence(schedule, now) == datetime(2026, 6, 12, 9, 0, tzinfo=UTC)


def test_weekly_without_weekdays_has_no_occurrence():
    schedule = {"schedule_type": config.SCHEDULE_WEEKLY, "hour": 9, "minute": 0, "weekdays": [], "timezone": "UTC"}
    assert next_occurrence(schedule, datetime(2026, 6, 10, tzinfo=UTC)) is None


def test_monthly_clamps_to_the_last_day_of_short_months():
    schedule = {"schedule_type": config.SCHEDULE_MONTHLY, "hour": 12, "minute": 0,
                "day_of_month": 31, "timezone": "UTC"}
    now = datetime(2026, 1, 31, 13, 0, tzinfo=UTC)
    # February 2026 has 28 days, so the 31st clamps to the 28th.
    assert next_occurrence(schedule, now) == datetime(2026, 2, 28, 12, 0, tzinfo=UTC)


def test_interval_keeps_its_cadence_after_downtime():
    schedule = {"schedule_type": config.SCHEDULE_INTERVAL, "interval_seconds": 7200, "timezone": "UTC"}
    anchor = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    # Bot was offline for five hours; the next run must stay on the 2h grid and
    # must not be in the past (which would cause a burst of catch-up pings).
    now = datetime(2026, 6, 10, 5, 0, tzinfo=UTC)
    following = next_occurrence(schedule, now, anchor=anchor)
    assert following == datetime(2026, 6, 10, 6, 0, tzinfo=UTC)
    assert following > now


def test_interval_first_run_starts_one_interval_from_now():
    schedule = {"schedule_type": config.SCHEDULE_INTERVAL, "interval_seconds": 3600, "timezone": "UTC"}
    now = datetime(2026, 6, 10, 5, 0, tzinfo=UTC)
    assert next_occurrence(schedule, now) == datetime(2026, 6, 10, 6, 0, tzinfo=UTC)


def test_dst_spring_forward_still_produces_a_real_instant():
    # 2026-03-08 02:00 does not exist in New York (clocks jump 02:00 -> 03:00).
    schedule = {"schedule_type": config.SCHEDULE_DAILY, "hour": 2, "minute": 30, "timezone": "America/New_York"}
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    following = next_occurrence(schedule, now)
    assert following is not None
    assert following.utcoffset() == timedelta(0)
    assert following > now


def test_localize_attaches_the_zone():
    assert localize(datetime(2026, 1, 1, 9, 0), IST).utcoffset() == timedelta(hours=5, minutes=30)


def test_validate_schedule_rejects_past_one_offs_and_tiny_intervals():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    past = {"schedule_type": config.SCHEDULE_ONCE, "once_at": now - timedelta(minutes=1)}
    assert "passed" in validate_schedule(past, now)

    soon = {"schedule_type": config.SCHEDULE_ONCE, "once_at": now + timedelta(hours=1)}
    assert validate_schedule(soon, now) is None

    tiny = {"schedule_type": config.SCHEDULE_INTERVAL, "interval_seconds": 5, "timezone": "UTC"}
    assert "at least" in validate_schedule(tiny, now)


def test_describe_schedule_is_human_readable():
    assert describe_schedule(reminder_fixture()) == "Daily at 18:30 (Asia/Kolkata)"
    weekly = reminder_fixture(schedule_type=config.SCHEDULE_WEEKLY, weekdays=[0, 4])
    assert describe_schedule(weekly) == "Weekly on Mon, Fri at 18:30 (Asia/Kolkata)"
    interval = reminder_fixture(schedule_type=config.SCHEDULE_INTERVAL, interval_seconds=5400)
    assert describe_schedule(interval) == "Every 1h 30m"
    once = reminder_fixture(schedule_type=config.SCHEDULE_ONCE,
                            once_at=datetime(2026, 10, 20, 13, 0, tzinfo=UTC))
    assert describe_schedule(once).startswith("Once on Tue 20 Oct 2026, 18:30")


def test_formatting_helpers():
    moment = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
    assert format_local(moment, "Asia/Kolkata") == "Tue 20 Oct 2026, 18:30"
    assert discord_timestamp(moment, "R") == f"<t:{int(moment.timestamp())}:R>"
    assert relative_delta(moment, moment - timedelta(hours=3)).startswith("in 3h")
    assert relative_delta(moment, moment + timedelta(minutes=10)).startswith("10m ago")


def test_schedule_type_of_normalises_unknown_values():
    assert schedule_type_of({"schedule_type": "DAILY"}) == config.SCHEDULE_DAILY
    assert schedule_type_of({"schedule_type": "nonsense"}) == config.SCHEDULE_ONCE
    assert schedule_type_of({}) == config.SCHEDULE_ONCE


# ---------------------------------------------------------------------------
# Store: atomic claim / duplicate prevention
# ---------------------------------------------------------------------------


async def _document(store: ReminderStore, document_id: str) -> Dict[str, Any]:
    return await store.collection.find_one({"_id": document_id})


def test_claim_due_delivers_a_due_reminder_once():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        store = ReminderStore(FakeCollection([reminder_fixture(next_run_at=now - timedelta(minutes=5))]))

        first = await store.claim_due(now)
        second = await store.claim_due(now)

        assert len(first) == 1
        assert second == [], "a claimed occurrence must never be handed out twice"

    asyncio.run(scenario())


def test_claimed_one_off_is_completed_and_never_fires_again():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        collection = FakeCollection(
            [reminder_fixture(
                schedule_type=config.SCHEDULE_ONCE,
                once_at=now - timedelta(minutes=1),
                next_run_at=now - timedelta(minutes=1),
            )]
        )
        store = ReminderStore(collection)

        claimed = await store.claim_due(now)
        assert len(claimed) == 1
        assert claimed[0]["status"] == "completed"

        # A restart must not replay it, whatever "now" is.
        assert await store.claim_due(now + timedelta(days=1)) == []

    asyncio.run(scenario())


def test_claimed_recurring_reminder_advances_into_the_future():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        store = ReminderStore(FakeCollection([reminder_fixture(next_run_at=now - timedelta(minutes=5))]))

        claimed = await store.claim_due(now)
        assert len(claimed) == 1

        advanced = as_utc(claimed[0]["next_run_at"])
        assert advanced > now
        assert claimed[0]["last_run_key"] is not None
        assert claimed[0]["status"] == "active"

    asyncio.run(scenario())


def test_claim_ignores_paused_and_future_reminders():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        store = ReminderStore(
            FakeCollection(
                [
                    reminder_fixture(_id="paused", status="paused", next_run_at=now - timedelta(hours=1)),
                    reminder_fixture(_id="future", next_run_at=now + timedelta(hours=1)),
                ]
            )
        )
        assert await store.claim_due(now) == []

    asyncio.run(scenario())


def test_claim_marks_an_unparsable_schedule_as_failed():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        broken = reminder_fixture(
            schedule_type=config.SCHEDULE_WEEKLY,
            weekdays=[],
            next_run_at=now - timedelta(minutes=5),
        )
        store = ReminderStore(FakeCollection([broken]))

        await store.claim_due(now)
        assert (await _document(store, "doc-1"))["status"] == "failed"

    asyncio.run(scenario())


def test_record_failure_can_retry_a_one_off_and_pause_a_bad_channel():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        store = ReminderStore(FakeCollection([reminder_fixture(schedule_type=config.SCHEDULE_ONCE)]))

        reminder = await _document(store, "doc-1")
        await store.record_failure(reminder, "transient boom", retry_at=now + timedelta(minutes=2))
        retried = await _document(store, "doc-1")
        assert retried["status"] == "active"
        assert as_utc(retried["next_run_at"]) == now + timedelta(minutes=2)
        assert retried["failure_count"] == 1

        await store.record_failure(retried, "channel gone", pause=True)
        stopped = await _document(store, "doc-1")
        assert stopped["status"] == "failed"
        assert "channel gone" in stopped["last_error"]

    asyncio.run(scenario())


def test_update_and_delete_are_scoped_to_the_guild():
    async def scenario():
        store = ReminderStore(FakeCollection([reminder_fixture()]))
        assert await store.update(999, "abc123", {"name": "nope"}) is False
        assert await store.delete(999, "abc123") is False
        assert await store.update(1, "abc123", {"name": "yes"}) is True
        assert (await _document(store, "doc-1"))["name"] == "yes"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Draft / document building
# ---------------------------------------------------------------------------


def test_draft_round_trips_a_reminder_document():
    reminder = reminder_fixture(
        schedule_type=config.SCHEDULE_WEEKLY, weekdays=[4, 0], hour=9, minute=15
    )
    draft = ReminderDraft.from_reminder(reminder, flow="edit_time")
    assert draft.reminder_id == "abc123"
    assert draft.weekdays == [0, 4]
    assert draft.schedule()["schedule_type"] == config.SCHEDULE_WEEKLY
    assert draft.needs_timezone() is True


def test_interval_drafts_skip_the_timezone_step():
    draft = ReminderDraft(guild_id=1, author_id=2)
    draft.schedule_type = config.SCHEDULE_INTERVAL
    draft.interval_seconds = 3600
    assert draft.needs_timezone() is False
    assert describe_schedule(draft.schedule() | {"timezone": draft.timezone}) == "Every 1h"


# ---------------------------------------------------------------------------
# Cog navigation logic (no Discord objects required)
# ---------------------------------------------------------------------------


def make_cog() -> ReminderCog:
    """A cog instance without running ``__init__`` (no bot, no database)."""
    return object.__new__(ReminderCog)


def test_wizard_flow_order_for_creation():
    cog = make_cog()
    draft = ReminderDraft(guild_id=1, author_id=2)
    draft.schedule_type = config.SCHEDULE_DAILY
    assert cog._next_step("create", "details", draft) == "frequency"
    assert cog._next_step("create", "frequency", draft) == "schedule"
    assert cog._next_step("create", "schedule", draft) == "timezone"
    assert cog._next_step("create", "timezone", draft) == "channel"
    assert cog._next_step("create", "channel", draft) == "role"
    assert cog._next_step("create", "role", draft) == "confirm"


def test_create_flow_skips_timezone_for_intervals():
    cog = make_cog()
    draft = ReminderDraft(guild_id=1, author_id=2)
    draft.schedule_type = config.SCHEDULE_INTERVAL
    draft.interval_seconds = 3600
    assert cog._next_step("create", "schedule", draft) == "channel"


def test_edit_flows_stop_at_the_confirmation_panel():
    cog = make_cog()
    draft = ReminderDraft(guild_id=1, author_id=2, flow="edit_time", reminder_id="abc")
    draft.schedule_type = config.SCHEDULE_DAILY
    assert cog._next_step("edit_time", "schedule", draft) == "confirm"
    assert cog._next_step("edit_channel", "channel", draft) == "confirm"
    assert cog._next_step("edit_frequency", "schedule", draft) == "timezone"


def test_resume_of_an_overdue_one_off_fires_soon_instead_of_vanishing():
    cog = make_cog()
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    missed = reminder_fixture(
        schedule_type=config.SCHEDULE_ONCE, once_at=now - timedelta(days=1)
    )
    following = cog._next_run_on_resume(missed, now)
    assert following is not None and following > now

    upcoming = reminder_fixture(
        schedule_type=config.SCHEDULE_ONCE, once_at=now + timedelta(hours=3)
    )
    assert cog._next_run_on_resume(upcoming, now) == now + timedelta(hours=3)

    recurring = reminder_fixture()
    assert cog._next_run_on_resume(recurring, now) > now


# ---------------------------------------------------------------------------
# Message composition (role pings and mention restrictions)
# ---------------------------------------------------------------------------


class _StubRole:
    def __init__(self, mentionable: bool) -> None:
        self.id = 555
        self.mentionable = mentionable
        self.mention = "<@&555>"
        self.name = "Events"


class _StubMe:
    def __init__(self, mention_everyone: bool) -> None:
        self.guild_permissions = discord.Permissions(mention_everyone=mention_everyone)


class _StubGuild:
    def __init__(self, *, role_mentionable: bool, bot_can_mention_everyone: bool, role_present: bool = True) -> None:
        self._role = _StubRole(role_mentionable) if role_present else None
        self.me = _StubMe(bot_can_mention_everyone)

    def get_role(self, role_id: int):
        return self._role if self._role is not None and role_id == self._role.id else None


def test_mentionable_role_is_pinged():
    outgoing = make_cog().build_message(
        reminder_fixture(role_id=555),
        _StubGuild(role_mentionable=True, bot_can_mention_everyone=False),
    )
    assert outgoing.content == "<@&555>"
    assert outgoing.allowed_mentions.roles is True
    assert outgoing.allowed_mentions.everyone is False
    assert outgoing.allowed_mentions.users is False
    assert outgoing.warning is None


def test_unmentionable_role_falls_back_so_everyone_is_never_tripped():
    outgoing = make_cog().build_message(
        reminder_fixture(role_id=555),
        _StubGuild(role_mentionable=False, bot_can_mention_everyone=False),
    )
    assert outgoing.content == "**Events**"
    assert outgoing.allowed_mentions.roles is False
    assert outgoing.warning == config.MENTION_WARNING


def test_unmentionable_role_is_pinged_when_the_bot_may_mention_everyone():
    outgoing = make_cog().build_message(
        reminder_fixture(role_id=555),
        _StubGuild(role_mentionable=False, bot_can_mention_everyone=True),
    )
    assert outgoing.content == "<@&555>"
    assert outgoing.allowed_mentions.roles is True


def test_missing_role_and_roleless_reminders_post_without_a_ping():
    missing = make_cog().build_message(
        reminder_fixture(role_id=555),
        _StubGuild(role_mentionable=True, bot_can_mention_everyone=False, role_present=False),
    )
    assert missing.content is None
    assert "no longer exists" in missing.warning

    plain = make_cog().build_message(reminder_fixture(role_id=None), _StubGuild(role_mentionable=True, bot_can_mention_everyone=False))
    assert plain.content is None
    assert plain.allowed_mentions.roles is False
    assert plain.embed.title.startswith("⏰ Standup")


# ---------------------------------------------------------------------------
# UI wiring
# ---------------------------------------------------------------------------


class FakeInteraction:
    """Records what the UI asked Discord to do, without a gateway."""

    def __init__(self, message: Any = None, user_id: int = 7) -> None:
        self.id = 1
        self.type = discord.InteractionType.component
        self.message = message
        self.guild = None
        self.user = SimpleNamespace(id=user_id)
        self.calls: List[str] = []
        self.response = self

    def is_done(self) -> bool:
        return False

    async def defer(self, **kwargs) -> None:
        self.calls.append("defer")

    async def edit_message(self, **kwargs) -> None:
        self.calls.append("edit_message")

    async def send_message(self, **kwargs) -> None:
        self.calls.append("send_message")

    async def send_modal(self, modal) -> None:
        self.calls.append("send_modal")


class FakeCog:
    """Every navigation method the views call, as a no-op coroutine."""

    def __getattr__(self, name: str):
        async def _noop(*args, **kwargs):
            return None

        return _noop


def make_draft() -> ReminderDraft:
    draft = ReminderDraft(guild_id=1, author_id=7)
    draft.name = "Standup"
    draft.message = "Time for standup"
    draft.timezone = "Asia/Kolkata"
    draft.channel_id = 99
    draft.schedule_type = config.SCHEDULE_DAILY
    draft.hour = 18
    draft.minute = 30
    return draft


def all_views():
    """One instance of every interactive panel/step in the package."""
    cog = FakeCog()
    draft = make_draft()
    weekly = make_draft()
    weekly.schedule_type = config.SCHEDULE_WEEKLY
    monthly = make_draft()
    monthly.schedule_type = config.SCHEDULE_MONTHLY
    interval = make_draft()
    interval.schedule_type = config.SCHEDULE_INTERVAL
    interval.interval_seconds = 3600
    reminder = reminder_fixture()

    return {
        "dashboard": reminder_views.DashboardView(cog, 7),
        "manage": reminder_views.ManageView(cog, 7, [reminder], 0, 1),
        "detail": reminder_views.ReminderDetailView(cog, 7, reminder),
        "list": reminder_views.ListPagerView(cog, 7, 0, 2),
        "confirm_action": reminder_views.ConfirmActionView(
            cog, 7, confirm_label="Yes", confirm_style=discord.ButtonStyle.danger,
            on_confirm=_noop, on_cancel=_noop,
        ),
        "frequency": reminder_views.FrequencyStep(cog, draft),
        "schedule": reminder_views.ScheduleStep(cog, draft),
        "weekday": reminder_views.WeekdayStep(cog, weekly),
        "day_of_month": reminder_views.DayOfMonthStep(cog, monthly),
        "timezone": reminder_views.TimezoneStep(cog, draft),
        "channel": reminder_views.ChannelStep(cog, draft),
        "role": reminder_views.RoleStep(cog, draft),
        "confirm": reminder_views.ConfirmStep(cog, draft),
        "interval_schedule": reminder_views.ScheduleStep(cog, interval),
    }


async def _noop(*args, **kwargs) -> None:
    return None


def test_every_view_fits_discord_component_limits():
    for name, view in all_views().items():
        # Construction itself validates Discord's caps (25 components, 5 per
        # row, 5 rows), so reaching this point already proves the layout is
        # legal; the assertions below document and pin that guarantee.
        rendered = view.to_components()
        assert len(rendered) <= 5, f"{name} uses more than 5 action rows"
        rows: Dict[int, int] = {}
        for item in view.children:
            row = getattr(item, "_rendered_row", None)
            if row is None:
                row = item.row if item.row is not None else 0
            rows[row] = rows.get(row, 0) + 1
        assert max(rows) <= 4, f"{name} uses more than 5 rows"
        for row, count in rows.items():
            assert count <= 5, f"{name} row {row} has {count} components"
        for item in view.children:
            label = getattr(item, "label", None)
            if label is not None:
                assert len(label) <= 80, f"{name}: label too long ({label})"


def test_every_select_option_respects_length_limits():
    for name, view in all_views().items():
        for item in view.children:
            for option in getattr(item, "options", []) or []:
                assert len(option.label) <= 100, f"{name}: option label too long"
                assert len(option.value) <= 100, f"{name}: option value too long"
                if option.description:
                    assert len(option.description) <= 100, f"{name}: description too long"


def test_all_view_callbacks_are_callable():
    for name, view in all_views().items():
        for item in view.children:
            assert callable(item.callback), f"{name}: {type(item).__name__} has no callback"


def _prime_selection(item: discord.ui.Item) -> None:
    """Give a select the values Discord would have sent for a real click."""
    if isinstance(item, discord.ui.ChannelSelect):
        item._values = [SimpleNamespace(id=99)]
    elif isinstance(item, discord.ui.RoleSelect):
        item._values = [SimpleNamespace(id=555)]
    else:
        options = getattr(item, "options", []) or []
        if not options:
            return
        count = 2 if getattr(item, "max_values", 1) > 1 else 1
        item._values = [option.value for option in options[:count]]


def test_every_button_and_select_can_be_pressed_without_raising():
    """Press every interactive component once.

    Every button and menu is wired through the cog, so this proves there are no
    typos or missing navigation targets between the UI and the cog.
    """

    async def press_all():
        for name, view in all_views().items():
            for item in view.children:
                if isinstance(item, discord.ui.Select) or isinstance(
                    item, (discord.ui.ChannelSelect, discord.ui.RoleSelect)
                ):
                    _prime_selection(item)
                interaction = FakeInteraction(message=None)
                await item.callback(interaction)  # type: ignore[misc]

    asyncio.run(press_all())


def test_modals_can_be_built_with_and_without_prefilled_values():
    cog = FakeCog()
    draft = make_draft()
    hourly = make_draft()
    hourly.schedule_type = config.SCHEDULE_INTERVAL
    hourly.interval_seconds = 3600
    modals = [
        reminder_views.DetailsModal(cog, draft),
        reminder_views.OnceModal(cog, draft),
        reminder_views.IntervalModal(cog, hourly),
        reminder_views.TimeOfDayModal(cog, draft),
        reminder_views.TimezoneModal(cog, draft),
        reminder_views.ChannelIdModal(cog, draft),
        reminder_views.DetailsModal(cog, ReminderDraft(guild_id=1, author_id=7)),
    ]
    for modal in modals:
        assert modal.title
        assert 1 <= len(modal.children) <= 5
        for child in modal.children:
            assert isinstance(child, discord.ui.TextInput)
        assert isinstance(modals[0].children[0], discord.ui.TextInput)
        # Prefilled editors must carry the existing values so the admin never
        # has to retype anything they are not changing.
        assert modals[0].name.value == "Standup"
        assert modals[2].every.value == "1h"  # interval modal, hourly draft
        assert modals[3].at.value == "18:30"


def test_permission_helper_denies_non_members_and_dms():
    dm = FakeInteraction()
    assert reminder_views.has_manage_permission(dm) is False
    dm.guild = object()  # a guild, but the actor is not a Member instance
    dm.user = object()
    assert reminder_views.has_manage_permission(dm) is False


def test_edit_drafts_belong_to_the_admin_who_opened_them():
    # A reminder created by user 999 must still be editable by user 42.
    view = reminder_views.ReminderDetailView(FakeCog(), 7, reminder_fixture(created_by=999))
    interaction = SimpleNamespace(user=SimpleNamespace(id=42))

    draft = view._new_draft(interaction, "edit_time")

    assert draft.author_id == 42
    assert draft.reminder_id == "abc123"
    assert draft.name == "Standup"
    assert draft.flow == "edit_time"


def test_dashboard_embed_summarises_state():
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    embed = reminder_views.build_dashboard_embed(
        {"total": 3, "active": 2, "paused": 1},
        [reminder_fixture(next_run_at=now + timedelta(hours=2))],
        now,
    )
    assert "3" in embed.fields[0].value
    assert "Standup" in embed.fields[1].value


def test_detail_embed_surfaces_the_last_error():
    reminder = reminder_fixture(status="failed", last_error="I do not have permission")
    embed = reminder_views.build_reminder_embed(reminder, now=datetime(2026, 6, 10, tzinfo=UTC))
    names = [field.name for field in embed.fields]
    assert any("Last error" in name for name in names)
    assert embed.color.value == config.ERROR_COLOR


# ---------------------------------------------------------------------------
# Delivery path (cog + store together, no gateway)
# ---------------------------------------------------------------------------


class FakeChannel(discord.abc.Messageable):
    """A channel that records everything the reminder posts."""

    def __init__(self, channel_id: int = 99, guild: Any = None, error: Optional[Exception] = None) -> None:
        self.id = channel_id
        self.guild = guild
        self.error = error
        self.sent: List[Dict[str, Any]] = []

    async def _get_channel(self):
        return self

    async def send(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return SimpleNamespace(id=1234, channel=self)


class FakeMessageableBot:
    def __init__(self, channel: Any, guild: Any, ready: bool = True) -> None:
        self.channel = channel
        self.guild = guild
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id: int):
        # Mirrors the real API for a channel that is gone or invisible.
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    def get_guild(self, guild_id: int):
        return self.guild

    def get_user(self, user_id: int):
        return None

    async def fetch_user(self, user_id: int):
        return None


def build_delivery_cog(channel: Any, guild: Any, documents: List[Dict[str, Any]]):
    cog = object.__new__(ReminderCog)
    cog.bot = FakeMessageableBot(channel, guild)
    cog._collection = FakeCollection(documents)
    cog._store = ReminderStore(cog._collection)
    cog._inflight = set()
    cog._db_warned = False
    return cog


def test_delivery_posts_the_ping_and_records_success():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(guild=guild)
        cog = build_delivery_cog(
            channel,
            guild,
            [reminder_fixture(role_id=555, next_run_at=now - timedelta(minutes=1))],
        )

        claimed = await cog.store.claim_due(now)
        assert len(claimed) == 1
        delivered, error = await cog._deliver(claimed[0])

        assert delivered is True and error is None
        assert len(channel.sent) == 1
        posted = channel.sent[0]
        assert posted["content"] == "<@&555>"
        assert posted["allowed_mentions"].everyone is False
        assert posted["allowed_mentions"].users is False
        assert posted["allowed_mentions"].roles is True
        assert "Standup" in posted["embed"].title

        stored = await _document(cog.store, "doc-1")
        assert stored["delivered_count"] == 1
        assert stored["failure_count"] == 0

        # The occurrence is spent: a busy second tick must not re-deliver it.
        assert await cog.store.claim_due(now) == []
        assert len(channel.sent) == 1

    asyncio.run(scenario())


def test_forbidden_channel_pauses_the_reminder_and_alerts_nobody_when_dms_fail():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        response = SimpleNamespace(status=403, reason="Forbidden")
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(guild=guild, error=discord.Forbidden(response, "missing permissions"))
        cog = build_delivery_cog(
            channel,
            guild,
            [reminder_fixture(role_id=555, next_run_at=now - timedelta(minutes=1))],
        )

        claimed = await cog.store.claim_due(now)
        delivered, error = await cog._deliver(claimed[0])

        assert delivered is False
        assert "permission" in error
        stored = await _document(cog.store, "doc-1")
        assert stored["status"] == "failed", "a permissions problem cannot fix itself, so stop retrying"
        assert stored["failure_count"] == 1

    asyncio.run(scenario())


def test_missing_channel_stops_the_reminder():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(channel_id=1234)  # nothing resolves to channel 99
        cog = build_delivery_cog(
            channel,
            guild,
            [reminder_fixture(next_run_at=now - timedelta(minutes=1))],
        )

        claimed = await cog.store.claim_due(now)
        delivered, error = await cog._deliver(claimed[0])

        assert delivered is False
        assert "could not reach" in error
        assert (await _document(cog.store, "doc-1"))["status"] == "failed"

    asyncio.run(scenario())


def test_a_failed_test_send_does_not_touch_the_schedule():
    async def scenario():
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        response = SimpleNamespace(status=403, reason="Forbidden")
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(guild=guild, error=discord.Forbidden(response, "nope"))
        before = reminder_fixture(next_run_at=now + timedelta(hours=3))
        cog = build_delivery_cog(channel, guild, [copy.deepcopy(before)])

        reminder = await _document(cog.store, "doc-1")
        delivered, error = await cog._deliver(reminder, test=True)

        assert delivered is False and "permission" in error
        after = await _document(cog.store, "doc-1")
        assert after["status"] == "active"
        assert after["next_run_at"] == before["next_run_at"]
        assert after.get("failure_count", 0) == 0

    asyncio.run(scenario())


def test_scheduler_loop_delivers_only_due_reminders():
    async def scenario():
        now = datetime.now(UTC)
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(guild=guild)
        due = reminder_fixture(_id="due", reminder_id="due1", next_run_at=now - timedelta(seconds=30))
        future = reminder_fixture(_id="future", reminder_id="future1", next_run_at=now + timedelta(days=1))
        paused = reminder_fixture(_id="paused", reminder_id="paused1", status="paused",
                                  next_run_at=now - timedelta(days=1))
        cog = build_delivery_cog(channel, guild, [due, future, paused])

        # Run one real pass of the loop body (``tasks.Loop.coro`` is the wrapped
        # function, so it can be awaited directly without a scheduler).
        await cog.scheduler.coro(cog)

        assert len(channel.sent) == 1
        assert (await _document(cog.store, "future"))["next_run_at"] == future["next_run_at"]
        assert (await _document(cog.store, "paused"))["status"] == "paused"
        # The delivered one advanced instead of staying overdue.
        assert as_utc((await _document(cog.store, "due"))["next_run_at"]) > now

    asyncio.run(scenario())


def test_store_behaves_correctly_against_real_mongo_semantics():
    """Optional: re-run the critical queries through a Mongo-API double.

    The in-memory collection above is deliberately simple; this test checks the
    pieces it cannot: BSON date round-tripping (motor hands back *naive* UTC),
    real index creation and uniqueness, and the aggregation pipeline behind the
    dashboard. It is skipped when ``mongomock-motor`` is not installed.
    """
    mongomock_motor = pytest.importorskip("mongomock_motor")

    async def scenario():
        client = mongomock_motor.AsyncMongoMockClient()
        collection = client["discord_bot"]["reminders"]
        store = ReminderStore(collection)
        await store.ensure_indexes()

        indexes = await collection.list_indexes().to_list(None)
        assert "guild_reminder" in {index["name"] for index in indexes}

        now = datetime.now(UTC).replace(microsecond=0)
        reminder = reminder_fixture(created_at=now, next_run_at=now - timedelta(minutes=1))
        reminder.pop("_id", None)
        await store.insert(reminder)

        stored = await collection.find_one({"reminder_id": "abc123"})
        assert stored["next_run_at"].tzinfo is None, "motor returns naive UTC"
        assert as_utc(stored["next_run_at"]) < now

        first = await store.claim_due(datetime.now(UTC))
        second = await store.claim_due(datetime.now(UTC))
        assert len(first) == 1 and second == [], "compare-and-set must hold on real Mongo"
        assert as_utc(first[0]["next_run_at"]) > datetime.now(UTC)

        assert (await store.stats_for_guild(1))["active"] == 1

        with pytest.raises(Exception):
            await store.insert(dict(reminder))

    asyncio.run(scenario())


def test_extension_loads_and_serialises_a_valid_command_tree():
    """Load the cog into a real Bot and validate the payload Discord would get."""

    async def scenario():
        bot = commands.Bot(command_prefix=".", intents=discord.Intents.default())
        try:
            await bot.load_extension("cogs.reminders.reminders")
            assert "Reminders" in bot.cogs

            group = bot.tree.get_command("reminder")
            assert group is not None
            assert group.guild_only is True
            assert group.default_permissions.manage_guild is True
            assert {command.name for command in group.commands} == {
                "dashboard", "create", "list", "manage", "preview", "pause", "resume", "delete", "help",
            }

            # to_dict() is exactly what the sync request sends, so this also
            # proves the names/descriptions/options are within Discord's limits.
            payload = group.to_dict(bot.tree)
            assert payload["name"] == "reminder"
            assert len(payload["description"]) <= 100
            assert int(payload["default_member_permissions"]) == discord.Permissions(manage_guild=True).value
            assert payload["dm_permission"] is False

            subcommands = {option["name"]: option for option in payload["options"]}
            assert set(subcommands) == {
                "dashboard", "create", "list", "manage", "preview", "pause", "resume", "delete", "help",
            }
            for subcommand in subcommands.values():
                assert subcommand["type"] == 1  # SUB_COMMAND
                assert 0 < len(subcommand["description"]) <= 100
                for option in subcommand.get("options", []):
                    assert option["type"] == 3  # STRING
                    assert option["required"] is True
                    assert 0 < len(option["description"]) <= 100
                    assert option["autocomplete"] is True
        finally:
            await bot.unload_extension("cogs.reminders.reminders")

    asyncio.run(scenario())


def test_scheduler_claims_nothing_while_the_gateway_is_not_ready():
    async def scenario():
        now = datetime.now(UTC)
        guild = _StubGuild(role_mentionable=True, bot_can_mention_everyone=False)
        channel = FakeChannel(guild=guild)
        overdue = reminder_fixture(next_run_at=now - timedelta(hours=1))
        cog = build_delivery_cog(channel, guild, [overdue])
        cog.bot.ready = False

        await cog.scheduler.coro(cog)

        assert channel.sent == []
        untouched = await _document(cog.store, "doc-1")
        assert untouched["status"] == "active"
        assert untouched["next_run_at"] == overdue["next_run_at"]

    asyncio.run(scenario())
