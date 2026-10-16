"""Configuration constants for the reminder system.

This module is intentionally dependency free so the scheduling engine
(:mod:`cogs.reminders.store`) and the UI (:mod:`cogs.reminders.views`) can both
import it without pulling in Discord or MongoDB.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

MONGO_DATABASE = os.getenv("REMINDER_DB_NAME", "discord_bot")
REMINDER_COLLECTION = "reminders"

# ---------------------------------------------------------------------------
# Embed colours
# ---------------------------------------------------------------------------

EMBED_COLOR = 0x5865F2  # blurple
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245
MUTED_COLOR = 0x2F3136

# ---------------------------------------------------------------------------
# Limits and validation
# ---------------------------------------------------------------------------

MAX_REMINDERS_PER_GUILD = 100
MAX_NAME_LENGTH = 100
MAX_MESSAGE_LENGTH = 1500
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 366 * 86400  # ~1 year between runs

# How many reminders may be delivered in a single scheduler tick. Keeps a long
# outage from producing an unbounded burst of sends (and hitting Discord's
# per-channel rate limits) when the bot comes back up.
MAX_REMINDERS_PER_TICK = 25

# Number of consecutive delivery failures before a reminder pauses itself.
MAX_CONSECUTIVE_FAILURES = 5

# Retry delay (seconds) for one-off reminders whose delivery failed for a
# transient reason (Discord 5xx / network). The delay grows linearly with the
# failure count, bounded by RETRY_MAX_DELAY_SECONDS.
RETRY_DELAY_SECONDS = 120
RETRY_MAX_DELAY_SECONDS = 1800

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SCHEDULER_TICK_SECONDS = 30

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

# Component views expire after this long. The dashboard is ephemeral, and a
# stale panel is worse than an expired one, so views disarm themselves.
ITEM_TIMEOUT = 900.0
# Reminders / timezones shown per page in the paginated select menus
# (Discord allows at most 25 options per select).
SELECT_PAGE_SIZE = 24
TIMEZONE_PAGE_SIZE = 24

DEFAULT_TIMEZONE = "UTC"

# ---------------------------------------------------------------------------
# Schedule types
# ---------------------------------------------------------------------------

SCHEDULE_ONCE = "once"
SCHEDULE_INTERVAL = "interval"
SCHEDULE_DAILY = "daily"
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_MONTHLY = "monthly"

SCHEDULE_TYPES = (
    SCHEDULE_ONCE,
    SCHEDULE_INTERVAL,
    SCHEDULE_DAILY,
    SCHEDULE_WEEKLY,
    SCHEDULE_MONTHLY,
)

# value -> (label, description, emoji)
FREQUENCY_CHOICES = (
    (SCHEDULE_ONCE, "One time", "Fire once, then finish", "⏰"),
    (SCHEDULE_INTERVAL, "Repeating interval", "Every X minutes / hours / days", "🔁"),
    (SCHEDULE_DAILY, "Daily", "Every day at a set time", "🌅"),
    (SCHEDULE_WEEKLY, "Weekly", "On the weekdays you pick", "📅"),
    (SCHEDULE_MONTHLY, "Monthly", "On a chosen day of each month", "🗓️"),
)

# Weekday names indexed by ``datetime.weekday()`` (Monday == 0).
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# ---------------------------------------------------------------------------
# Timezones offered in the picker
# ---------------------------------------------------------------------------

# A curated shortlist keeps the menu usable; anything else can be typed in via
# the "Other" button, which accepts any IANA name installed in ``zoneinfo``.
COMMON_TIMEZONES = (
    "UTC",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Karachi",
    "Asia/Dhaka",
    "Asia/Colombo",
    "Asia/Kathmandu",
    "Asia/Singapore",
    "Asia/Kuala_Lumpur",
    "Asia/Jakarta",
    "Asia/Manila",
    "Asia/Bangkok",
    "Asia/Ho_Chi_Minh",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Taipei",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Riyadh",
    "Asia/Tehran",
    "Asia/Jerusalem",
    "Europe/Istanbul",
    "Europe/Moscow",
    "Europe/Athens",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Warsaw",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Lisbon",
    "Africa/Lagos",
    "Africa/Cairo",
    "Africa/Nairobi",
    "Africa/Johannesburg",
    "America/Sao_Paulo",
    "America/Argentina/Buenos_Aires",
    "America/New_York",
    "America/Toronto",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Mexico_City",
    "Australia/Perth",
    "Australia/Adelaide",
    "Australia/Sydney",
    "Australia/Brisbane",
    "Pacific/Auckland",
)

# Friendly shorthands accepted when an admin types a timezone by hand.
TIMEZONE_ALIASES = {
    "utc": "UTC",
    "gmt": "Etc/GMT",
    "z": "UTC",
    "ist": "Asia/Kolkata",
    "pkt": "Asia/Karachi",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "cest": "Europe/Paris",
    "eet": "Europe/Athens",
    "msk": "Europe/Moscow",
    "gst": "Asia/Dubai",
    "sgt": "Asia/Singapore",
    "hkt": "Asia/Hong_Kong",
    "jst": "Asia/Tokyo",
    "kst": "Asia/Seoul",
    "aest": "Australia/Sydney",
    "edt": "America/New_York",
    "est": "America/New_York",
    "cdt": "America/Chicago",
    "cst": "America/Chicago",
    "pdt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
}

# The role fallback used when the bot cannot ping the configured role.
MENTION_WARNING = "⚠️ This reminder's role could not be pinged (the role is not mentionable and I lack **Mention @everyone**)."
