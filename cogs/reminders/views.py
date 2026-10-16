"""Interactive Discord UI for the reminder system.

Every panel here is *ephemeral* and reusable: a step never sends a new message
when it can edit the message it grew out of, so the whole wizard stays in one
private message the admin can walk back and forth through.

Layout and input parsing live here. Navigation and database work are delegated
to :class:`cogs.reminders.reminders.ReminderCog` (``self.cog.step_*`` /
``self.cog.finish_draft``), which keeps the Mongo code in one place and lets
this module stay free of I/O.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Sequence

import discord

from cogs.interaction_utils import deregister_modal, safe_edit, safe_reply, send_modal
from cogs.reminders import config
from cogs.reminders.store import (
    ReminderDraft,
    as_utc,
    describe_schedule,
    discord_timestamp,
    format_duration,
    format_local,
    format_time_of_day,
    relative_delta,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def has_manage_permission(interaction: discord.Interaction) -> bool:
    """True when the actor may configure reminders (Admins, or Manage Server)."""
    if interaction.guild is None:
        return False
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    permissions = member.guild_permissions
    return bool(permissions.administrator or permissions.manage_guild)


def truncate(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def make_button(
    label: str,
    action: Callable[[discord.Interaction], Any],
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: Optional[str] = None,
    row: Optional[int] = None,
    disabled: bool = False,
) -> discord.ui.Button:
    """Build a button bound to a coroutine.

    Binding the callback after construction (the pattern the sticky-message cog
    already uses) keeps a dozen one-line actions from needing a dozen classes.
    """
    button = discord.ui.Button(label=label, style=style, emoji=emoji, row=row, disabled=disabled)
    button.callback = action
    return button


async def render(interaction: discord.Interaction, *, content: Optional[str] = None,
                 embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None) -> bool:
    """Show a panel, editing the current ephemeral message when there is one.

    Three cases matter: a component click (edit the message it came from), a
    modal submitted over that message (same), and a modal opened straight from
    a slash command with no message yet (send a new private message).
    """
    if interaction.response.is_done():
        return await safe_edit(interaction, content=content, embed=embed, view=view)

    if interaction.message is not None:
        try:
            await interaction.response.edit_message(content=content, embed=embed, view=view)
            return True
        except discord.HTTPException as exc:
            logger.debug("Could not edit reminder panel, sending a new one: %s", exc)

    try:
        await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=True)
        return True
    except discord.HTTPException as exc:
        logger.warning("Could not render reminder panel: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------


def build_dashboard_embed(stats: Dict[str, int], reminders: Sequence[Dict[str, Any]],
                          now: Any) -> discord.Embed:
    embed = discord.Embed(
        title="⏰ Reminder dashboard",
        color=config.EMBED_COLOR,
        description=(
            "Everything on this panel is persistent — reminders are stored in the "
            "database and keep running across restarts."
        ),
    )
    embed.add_field(
        name="Overview",
        value=(
            f"**{stats.get('total', 0)}** total • "
            f"**{stats.get('active', 0)}** active • "
            f"**{stats.get('paused', 0)}** paused"
            + (f" • **{stats.get('failed', 0)}** failed" if stats.get("failed") else "")
        ),
        inline=False,
    )

    upcoming = [item for item in reminders if item.get("status") == "active" and item.get("next_run_at")]
    upcoming.sort(key=lambda item: as_utc(item["next_run_at"]) or now)
    if upcoming:
        lines = []
        for item in upcoming[:5]:
            when = as_utc(item["next_run_at"])
            lines.append(
                f"• **{truncate(item.get('name') or 'Untitled', 60)}** — {discord_timestamp(when, 'R')} "
                f"(<#{item.get('channel_id')}>)"
            )
        if len(upcoming) > 5:
            lines.append(f"…and **{len(upcoming) - 5}** more")
        embed.add_field(name="Next up", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Next up", value="Nothing scheduled yet.", inline=False)

    embed.set_footer(text="Use the buttons below • Only the person who opened this panel can use it")
    return embed


def build_reminder_embed(reminder: Dict[str, Any], *, now: Any = None,
                         note: Optional[str] = None) -> discord.Embed:
    status = str(reminder.get("status") or "active")
    colour = {
        "active": config.EMBED_COLOR,
        "paused": config.WARNING_COLOR,
        "failed": config.ERROR_COLOR,
        "completed": config.MUTED_COLOR,
    }.get(status, config.EMBED_COLOR)

    embed = discord.Embed(
        title=f"⏰ {truncate(reminder.get('name') or 'Untitled reminder', 250)}",
        description=truncate(reminder.get("message") or "*no message*", 4000),
        color=colour,
    )

    timezone_name = reminder.get("timezone") or config.DEFAULT_TIMEZONE
    next_run = as_utc(reminder.get("next_run_at"))
    if status == "active" and next_run is not None:
        next_text = f"{discord_timestamp(next_run, 'F')}\n{discord_timestamp(next_run, 'R')}"
    elif status == "completed":
        next_text = "Finished (one-off reminder already delivered)"
    elif status == "failed":
        next_text = "Stopped — see the error below"
    else:
        next_text = "Paused"

    role_id = reminder.get("role_id")
    embed.add_field(name="Schedule", value=describe_schedule(reminder), inline=True)
    embed.add_field(name="Next run", value=next_text, inline=True)
    embed.add_field(name="Status", value=status.title(), inline=True)
    embed.add_field(name="Channel", value=f"<#{reminder.get('channel_id')}>" if reminder.get("channel_id") else "`not set`", inline=True)
    embed.add_field(name="Role ping", value=f"<@&{role_id}>" if role_id else "🔕 none", inline=True)
    embed.add_field(name="Timezone", value=truncate(timezone_name, 60), inline=True)

    stats_line = f"Delivered **{int(reminder.get('delivered_count') or 0)}** time(s)"
    failures = int(reminder.get("failure_count") or 0)
    if failures:
        stats_line += f" • **{failures}** consecutive failure(s)"
    embed.add_field(name="Delivery", value=stats_line, inline=False)

    last_delivered = as_utc(reminder.get("last_delivered_at"))
    if last_delivered is not None:
        embed.add_field(name="Last delivered", value=discord_timestamp(last_delivered, 'R'), inline=True)
    created_at = as_utc(reminder.get("created_at"))
    if created_at is not None:
        embed.add_field(name="Created", value=discord_timestamp(created_at, 'R'), inline=True)

    if reminder.get("last_error"):
        embed.add_field(name="⚠️ Last error", value=truncate(reminder["last_error"], 1000), inline=False)

    if note:
        embed.add_field(name="ℹ️ Note", value=truncate(note, 1000), inline=False)

    embed.set_footer(text=f"Reminder ID: {reminder.get('reminder_id')}")
    return embed


def build_draft_embed(draft: ReminderDraft, *, title: str, step_hint: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=step_hint, color=config.EMBED_COLOR)
    embed.add_field(name="Name", value=truncate(draft.name, 100) or "`not set`", inline=True)

    if draft.schedule_type == config.SCHEDULE_ONCE:
        if draft.once_at is not None:
            schedule = f"Once on {format_local(draft.once_at, draft.timezone)}"
        else:
            schedule = "Once — time not set"
    elif draft.schedule_type == config.SCHEDULE_INTERVAL:
        schedule = (
            f"Every {format_duration(draft.interval_seconds)}"
            if draft.interval_seconds
            else "Interval not set"
        )
    elif draft.schedule_type == config.SCHEDULE_DAILY:
        schedule = (
            f"Daily at {format_time_of_day(draft.hour, draft.minute)}"
            if draft.hour is not None
            else "Daily — time not set"
        )
    elif draft.schedule_type == config.SCHEDULE_WEEKLY:
        names = ", ".join(config.WEEKDAY_SHORT[day] for day in sorted(draft.weekdays)) or "no days chosen"
        clock = f" at {format_time_of_day(draft.hour, draft.minute)}" if draft.hour is not None else ""
        schedule = f"Weekly on {names}{clock}"
    else:
        clock = f" at {format_time_of_day(draft.hour, draft.minute)}" if draft.hour is not None else ""
        schedule = f"Monthly on day {draft.day_of_month or '?'}{clock}"

    embed.add_field(name="Schedule", value=truncate(schedule, 100), inline=True)
    embed.add_field(name="Timezone", value=truncate(draft.timezone, 60), inline=True)
    embed.add_field(
        name="Channel",
        value=f"<#{draft.channel_id}>" if draft.channel_id else "`not set`",
        inline=True,
    )
    embed.add_field(
        name="Role ping",
        value=f"<@&{draft.role_id}>" if draft.role_id else "🔕 none",
        inline=True,
    )
    embed.add_field(name="Message", value=truncate(draft.message, 900) or "`not set`", inline=False)

    if draft.note:
        embed.add_field(name="ℹ️ Note", value=truncate(draft.note, 500), inline=False)

    return embed


# ---------------------------------------------------------------------------
# Base view
# ---------------------------------------------------------------------------


class ReminderView(discord.ui.View):
    """Base panel: only the invoker may press its buttons, and only with perms.

    ``interaction_check`` answers with a private message and returns ``False``,
    which stops discord.py from running the item's callback. Checking here
    (rather than only on the slash command) is what stops a member who lost
    their permissions from using a panel opened earlier.
    """

    def __init__(self, *, author_id: int, timeout: float = config.ITEM_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await safe_reply(
                interaction,
                content="❌ This panel belongs to whoever opened it. Run the command yourself to open your own.",
                ephemeral=True,
            )
            return False
        if not has_manage_permission(interaction):
            await safe_reply(
                interaction,
                content="❌ You need the **Manage Server** permission to manage reminders.",
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Reminder panel error in %s: %s", type(item).__name__, error)
        await safe_reply(
            interaction,
            content="❌ Something went wrong handling that. Please try again or check the bot logs.",
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        # A stale panel that is still clickable is worse than an expired one.
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                logger.debug("Could not disable expired reminder panel", exc_info=True)


class WizardView(ReminderView):
    """Base for a step of the setup / edit wizard."""

    def __init__(self, cog: Any, draft: ReminderDraft, *, timeout: float = config.ITEM_TIMEOUT) -> None:
        super().__init__(author_id=draft.author_id, timeout=timeout)
        self.cog = cog
        self.draft = draft


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------


class FrequencyStep(WizardView):
    """Pick the schedule type."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        options = [
            discord.SelectOption(label=label, value=value, description=description, emoji=emoji)
            for value, label, description, emoji in config.FREQUENCY_CHOICES
        ]
        select = discord.ui.Select(placeholder="Choose how often this reminder should run…", options=options)
        select.callback = self._on_pick
        self.add_item(select)
        self.add_item(make_button("✏️ Name & message", self._edit_details))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        self.draft.schedule_type = select.values[0]
        # A different cadence invalidates the details of the previous one.
        self.draft.hour = None
        self.draft.minute = None
        self.draft.weekdays = []
        self.draft.day_of_month = None
        self.draft.once_at = None
        self.draft.interval_seconds = None
        await self.cog.step_schedule(interaction, self.draft)

    async def _edit_details(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, DetailsModal(self.cog, self.draft))


class ScheduleStep(WizardView):
    """A single "open the right modal" step, labelled per schedule type."""

    PROMPTS = {
        config.SCHEDULE_ONCE: ("🗓️ Set date & time", "Set the date and time this reminder should fire."),
        config.SCHEDULE_INTERVAL: ("⏱️ Set the interval", "Set how often the reminder should repeat."),
        config.SCHEDULE_DAILY: ("🕒 Set the time of day", "Set the time it should fire every day."),
        config.SCHEDULE_WEEKLY: ("🕒 Set the time of day", "Set the time it should fire on the chosen weekdays."),
        config.SCHEDULE_MONTHLY: ("🕒 Set the time of day", "Set the time it should fire on the chosen day."),
    }

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        label, _hint = self.PROMPTS.get(draft.schedule_type, self.PROMPTS[config.SCHEDULE_ONCE])
        self.add_item(make_button(label, self._open, style=discord.ButtonStyle.primary))
        self.add_item(make_button("🔁 Change frequency", self._change_frequency))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger))

    def _modal(self) -> discord.ui.Modal:
        if self.draft.schedule_type == config.SCHEDULE_ONCE:
            return OnceModal(self.cog, self.draft)
        if self.draft.schedule_type == config.SCHEDULE_INTERVAL:
            return IntervalModal(self.cog, self.draft)
        return TimeOfDayModal(self.cog, self.draft)

    async def _open(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, self._modal())

    async def _change_frequency(self, interaction: discord.Interaction) -> None:
        await self.cog.step_frequency(interaction, self.draft)


class WeekdayStep(WizardView):
    """Multi-select for the weekdays of a weekly reminder."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        options = [
            discord.SelectOption(label=config.WEEKDAY_NAMES[index], value=str(index))
            for index in range(7)
        ]
        select = discord.ui.Select(
            placeholder="Pick one or more weekdays…",
            options=options,
            min_values=1,
            max_values=7,
        )
        select.callback = self._on_pick
        self.add_item(select)
        self.add_item(make_button("↩️ Back", self._back))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        self.draft.weekdays = sorted({int(value) for value in select.values})
        await self.cog.step_schedule(interaction, self.draft)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog.step_frequency(interaction, self.draft)


class DayOfMonthStep(WizardView):
    """Day-of-month picker, split across two selects (Discord caps at 25 options)."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        first = discord.ui.Select(
            placeholder="Day of the month (1 - 25)…",
            options=[discord.SelectOption(label=str(day), value=str(day)) for day in range(1, 26)],
        )
        second = discord.ui.Select(
            placeholder="Day of the month (26 - 31)…",
            options=[discord.SelectOption(label=str(day), value=str(day)) for day in range(26, 32)],
        )
        # Binding each select to a closure (rather than inspecting
        # ``interaction.data``) keeps the picked value unambiguous even though
        # both menus write to the same draft field.
        first.callback = self._make_pick(first)
        second.callback = self._make_pick(second)
        self.add_item(first)
        self.add_item(second)
        self.add_item(make_button("📌 Last day of the month", self._last_day))
        self.add_item(make_button("↩️ Back", self._back))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger))

    def _make_pick(self, select: discord.ui.Select) -> Callable[[discord.Interaction], Any]:
        async def callback(interaction: discord.Interaction) -> None:
            values = getattr(select, "values", None) or []
            if values:
                self.draft.day_of_month = int(values[0])
            await self.cog.step_schedule(interaction, self.draft)

        return callback

    async def _last_day(self, interaction: discord.Interaction) -> None:
        # 31 is clamped to each month's real length when the next run is computed.
        self.draft.day_of_month = 31
        await self.cog.step_schedule(interaction, self.draft)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog.step_frequency(interaction, self.draft)


class TimezoneStep(WizardView):
    """Paginated timezone picker with a free-text escape hatch."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        zones = list(config.COMMON_TIMEZONES)
        pages = max(1, (len(zones) + config.TIMEZONE_PAGE_SIZE - 1) // config.TIMEZONE_PAGE_SIZE)
        page = max(0, min(draft.page, pages - 1))
        draft.page = page
        start = page * config.TIMEZONE_PAGE_SIZE
        chunk = zones[start:start + config.TIMEZONE_PAGE_SIZE]

        select = discord.ui.Select(
            placeholder=f"Pick a timezone ({page + 1}/{pages})…",
            options=[
                discord.SelectOption(
                    label=zone,
                    value=zone,
                    default=(zone == draft.timezone),
                    description=f"UTC{_utc_offset_label(zone)}",
                )
                for zone in chunk
            ],
        )
        select.callback = self._on_pick
        self.add_item(select)
        self.add_item(make_button("🌐 Other (type it)", self._other, row=1))
        self.add_item(
            make_button("◀️", self._previous, row=1, disabled=page <= 0),
        )
        self.add_item(
            make_button("▶️", self._next, row=1, disabled=page >= pages - 1),
        )
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger, row=1))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        self.draft.timezone = select.values[0]
        await self.cog.step_channel(interaction, self.draft)

    async def _other(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, TimezoneModal(self.cog, self.draft))

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.draft.page = max(0, self.draft.page - 1)
        await self.cog.step_timezone(interaction, self.draft)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.draft.page = self.draft.page + 1
        await self.cog.step_timezone(interaction, self.draft)


def _utc_offset_label(zone_name: str) -> str:
    from datetime import datetime, timezone as _timezone

    from cogs.reminders.store import resolve_timezone

    try:
        zone = resolve_timezone(zone_name)
    except ValueError:
        return ""
    offset = datetime.now(_timezone.utc).astimezone(zone).utcoffset()
    if offset is None:
        return ""
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


class ChannelStep(WizardView):
    """Native channel picker plus an ID escape hatch for unusual channels."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        select = discord.ui.ChannelSelect(
            placeholder="Pick the channel to post reminders in…",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_pick
        self.add_item(select)
        self.add_item(make_button("⌨️ Enter a channel ID", self._manual, row=1))
        self.add_item(make_button("↩️ Back", self._back, row=1))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger, row=1))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        value = select.values[0]
        self.draft.channel_id = int(value.id)
        await self.cog.after_channel(interaction, self.draft)

    async def _manual(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, ChannelIdModal(self.cog, self.draft))

    async def _back(self, interaction: discord.Interaction) -> None:
        if self.draft.needs_timezone():
            await self.cog.step_timezone(interaction, self.draft)
        else:
            await self.cog.step_schedule(interaction, self.draft)


class RoleStep(WizardView):
    """Native role picker with an explicit "no ping" option."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        select = discord.ui.RoleSelect(
            placeholder="Pick the role to ping (or press “No role”)…",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_pick
        self.add_item(select)
        self.add_item(make_button("🔕 No role (no ping)", self._no_role, row=1))
        self.add_item(make_button("↩️ Back", self._back, row=1))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger, row=1))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        role = select.values[0]
        guild = interaction.guild
        if guild is not None and role.id == guild.default_role.id:
            # @everyone is not a ping target here: it would need Mention
            # Everyone, and allowed_mentions deliberately blocks that.
            self.draft.role_id = None
            self.draft.note = "`@everyone` is not used as a ping target — reminders never ping the whole server."
        else:
            self.draft.role_id = int(role.id)
            self.draft.note = ""
            if guild is not None:
                resolved = guild.get_role(role.id)
                if resolved is not None and not _role_pingable(resolved, guild):
                    self.draft.note = (
                        f"⚠️ I cannot ping <@&{role.id}> yet: it is not mentionable and I lack "
                        "**Mention @everyone**. I will post its name instead unless that changes."
                    )
        await self.cog.after_role(interaction, self.draft)

    async def _no_role(self, interaction: discord.Interaction) -> None:
        self.draft.role_id = None
        self.draft.note = ""
        await self.cog.after_role(interaction, self.draft)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog.step_channel(interaction, self.draft)


def _role_pingable(role: discord.Role, guild: discord.Guild) -> bool:
    """A role can be pinged when it is mentionable or the bot may mention @everyone."""
    if role.mentionable:
        return True
    me = guild.me
    return bool(me is not None and me.guild_permissions.mention_everyone)


class ConfirmStep(WizardView):
    """Final review before the reminder is written to the database."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(cog, draft)
        label = "💾 Apply changes" if draft.reminder_id else "✅ Create reminder"
        self.add_item(make_button(label, self._save, style=discord.ButtonStyle.success))
        self.add_item(make_button("✏️ Name & message", self._edit_details))
        self.add_item(make_button("📢 Channel", self._channel))
        self.add_item(make_button("🔔 Role", self._role))
        self.add_item(make_button("❌ Cancel", self.cog.cancel_wizard, style=discord.ButtonStyle.danger))

    async def _save(self, interaction: discord.Interaction) -> None:
        await self.cog.finish_draft(interaction, self.draft)

    async def _edit_details(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, DetailsModal(self.cog, self.draft))

    async def _channel(self, interaction: discord.Interaction) -> None:
        await self.cog.step_channel(interaction, self.draft)

    async def _role(self, interaction: discord.Interaction) -> None:
        await self.cog.step_role(interaction, self.draft)


# ---------------------------------------------------------------------------
# Dashboard / management views
# ---------------------------------------------------------------------------


class DashboardView(ReminderView):
    """The landing panel opened by ``/reminder dashboard``."""

    def __init__(self, cog: Any, author_id: int) -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        self.add_item(make_button("➕ Create reminder", self._create, style=discord.ButtonStyle.success))
        self.add_item(make_button("📋 Manage reminders", self._manage))
        self.add_item(make_button("🔄 Refresh", self._refresh))
        self.add_item(make_button("⏸️ Pause all", self._pause_all))
        self.add_item(make_button("▶️ Resume all", self._resume_all))
        self.add_item(make_button("❓ Help", self._help))

    async def _create(self, interaction: discord.Interaction) -> None:
        await self.cog.start_create(interaction)

    async def _manage(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, 0)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dashboard(interaction)

    async def _pause_all(self, interaction: discord.Interaction) -> None:
        await self.cog.bulk_status(interaction, "paused")

    async def _resume_all(self, interaction: discord.Interaction) -> None:
        await self.cog.bulk_status(interaction, "active")

    async def _help(self, interaction: discord.Interaction) -> None:
        await self.cog.show_help(interaction)


class ManageView(ReminderView):
    """Paginated list where each entry opens the reminder's own panel."""

    def __init__(self, cog: Any, author_id: int, reminders: Sequence[Dict[str, Any]],
                 page: int, pages: int) -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        self.reminders = list(reminders)
        self.page = page
        self.pages = pages

        options = []
        for reminder in self.reminders:
            label = truncate(reminder.get("name") or reminder.get("reminder_id"), 100)
            status = str(reminder.get("status") or "active")
            marker = {"active": "🟢", "paused": "⏸️", "failed": "🔴", "completed": "✅"}.get(status, "⚪")
            description = f"{marker} {describe_schedule(reminder)}"
            next_run = as_utc(reminder.get("next_run_at"))
            if status == "active" and next_run is not None:
                description += f" • next {relative_delta(next_run)}"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(reminder.get("reminder_id")),
                    description=truncate(description, 100),
                )
            )

        select = discord.ui.Select(
            placeholder="Open a reminder to edit, pause or delete it…",
            options=options,
            disabled=not options,
        )
        select.callback = self._open
        self.add_item(select)
        self.add_item(make_button("➕ Create", self._create, style=discord.ButtonStyle.success, row=1))
        self.add_item(make_button("◀️", self._previous, row=1, disabled=page <= 0))
        self.add_item(make_button("▶️", self._next, row=1, disabled=page >= pages - 1))
        self.add_item(make_button("🔄 Refresh", self._refresh, row=1))
        self.add_item(make_button("🏠 Dashboard", self._home, row=1))

    async def _open(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        await self.cog.show_detail(interaction, select.values[0])

    async def _create(self, interaction: discord.Interaction) -> None:
        await self.cog.start_create(interaction)

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, max(0, self.page - 1))

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, self.page + 1)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, self.page)

    async def _home(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dashboard(interaction)


class ReminderDetailView(ReminderView):
    """Everything you can do to one reminder."""

    def __init__(self, cog: Any, author_id: int, reminder: Dict[str, Any]) -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        self.reminder = reminder
        status = str(reminder.get("status") or "active")

        self.add_item(make_button("✏️ Name & message", self._edit_details))
        self.add_item(make_button("🕒 Reschedule", self._reschedule))
        self.add_item(make_button("🔁 Frequency", self._frequency))
        self.add_item(make_button("🌐 Timezone", self._timezone))
        self.add_item(make_button("📢 Channel", self._channel))
        self.add_item(make_button("🔔 Role", self._role))

        if status == "paused":
            self.add_item(make_button("▶️ Resume", self._resume, style=discord.ButtonStyle.success))
        else:
            self.add_item(make_button("⏸️ Pause", self._pause))

        self.add_item(make_button("🗑️ Delete", self._delete, style=discord.ButtonStyle.danger))
        self.add_item(make_button("🔍 Preview", self._preview))
        self.add_item(make_button("🧪 Send test", self._send_test))
        self.add_item(make_button("↩️ Back", self._back))

    def _new_draft(self, interaction: discord.Interaction, flow: str) -> ReminderDraft:
        draft = ReminderDraft.from_reminder(self.reminder, flow=flow)
        # Ownership follows whoever is editing right now, not whoever created the
        # reminder: every admin must be able to edit any reminder, and the
        # wizard's interaction check keys off this id.
        draft.author_id = interaction.user.id
        return draft

    async def _edit_details(self, interaction: discord.Interaction) -> None:
        await send_modal(interaction, DetailsModal(self.cog, self._new_draft(interaction, "edit_details")))

    async def _reschedule(self, interaction: discord.Interaction) -> None:
        await self.cog.step_schedule(interaction, self._new_draft(interaction, "edit_time"))

    async def _frequency(self, interaction: discord.Interaction) -> None:
        await self.cog.step_frequency(interaction, self._new_draft(interaction, "edit_frequency"))

    async def _timezone(self, interaction: discord.Interaction) -> None:
        await self.cog.step_timezone(interaction, self._new_draft(interaction, "edit_timezone"))

    async def _channel(self, interaction: discord.Interaction) -> None:
        await self.cog.step_channel(interaction, self._new_draft(interaction, "edit_channel"))

    async def _role(self, interaction: discord.Interaction) -> None:
        await self.cog.step_role(interaction, self._new_draft(interaction, "edit_role"))

    async def _pause(self, interaction: discord.Interaction) -> None:
        await self.cog.toggle_pause(interaction, self.reminder, pause=True)

    async def _resume(self, interaction: discord.Interaction) -> None:
        await self.cog.toggle_pause(interaction, self.reminder, pause=False)

    async def _delete(self, interaction: discord.Interaction) -> None:
        await self.cog.show_delete_confirm(interaction, self.reminder)

    async def _preview(self, interaction: discord.Interaction) -> None:
        await self.cog.preview_reminder(interaction, self.reminder)

    async def _send_test(self, interaction: discord.Interaction) -> None:
        await self.cog.show_test_confirm(interaction, self.reminder)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, 0)


class ConfirmActionView(ReminderView):
    """Two-button confirmation used for destructive or public actions."""

    def __init__(self, cog: Any, author_id: int, *, confirm_label: str, confirm_style: discord.ButtonStyle,
                 on_confirm: Callable[[discord.Interaction], Any], on_cancel: Callable[[discord.Interaction], Any]) -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        self.add_item(make_button(confirm_label, on_confirm, style=confirm_style))
        self.add_item(make_button("↩️ Cancel", on_cancel))


class ListPagerView(ReminderView):
    """``/reminder list`` output: compact pages of every reminder."""

    def __init__(self, cog: Any, author_id: int, page: int, pages: int) -> None:
        super().__init__(author_id=author_id)
        self.cog = cog
        self.page = page
        self.pages = pages
        self.add_item(make_button("◀️", self._previous, disabled=page <= 0))
        self.add_item(make_button("▶️", self._next, disabled=page >= pages - 1))
        self.add_item(make_button("🔄 Refresh", self._refresh))
        self.add_item(make_button("📋 Manage", self._manage))
        self.add_item(make_button("🏠 Dashboard", self._home))

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self.cog.show_list(interaction, max(0, self.page - 1))

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.cog.show_list(interaction, self.page + 1)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.show_list(interaction, self.page)

    async def _manage(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage(interaction, 0)

    async def _home(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dashboard(interaction)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


class DetailsModal(discord.ui.Modal):
    """Name + message. The first step of the wizard, and the inline editor."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="Reminder details" if not draft.reminder_id else "Edit reminder details")
        self.cog = cog
        self.draft = draft
        self.name = discord.ui.TextInput(
            label="Name",
            placeholder="e.g. Weekly staff meeting",
            default=truncate(draft.name, config.MAX_NAME_LENGTH - 1) if draft.name else None,
            max_length=config.MAX_NAME_LENGTH,
            required=True,
        )
        self.message = discord.ui.TextInput(
            label="Message",
            placeholder="What should the reminder say?",
            style=discord.TextStyle.paragraph,
            default=truncate(draft.message, config.MAX_MESSAGE_LENGTH - 1) if draft.message else None,
            max_length=config.MAX_MESSAGE_LENGTH,
            required=True,
        )
        self.add_item(self.name)
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        self.draft.name = str(self.name.value).strip()
        self.draft.message = str(self.message.value).strip()

        if not self.draft.name:
            await safe_reply(interaction, content="❌ The reminder needs a name.", ephemeral=True)
            return
        if not self.draft.message:
            await safe_reply(interaction, content="❌ The reminder needs a message.", ephemeral=True)
            return

        if self.draft.flow == "edit_details":
            await self.cog.finish_draft(interaction, self.draft)
        else:
            await self.cog.step_frequency(interaction, self.draft)


class OnceModal(discord.ui.Modal):
    """Absolute date/time or a relative offset, e.g. ``in 2h30m``."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="When should it fire?")
        self.cog = cog
        self.draft = draft
        self.when = discord.ui.TextInput(
            label="Date & time (or “in 2h30m”)",
            placeholder="2026-10-20 18:30",
            default=_once_default(draft),
            max_length=60,
            required=True,
        )
        self.add_item(self.when)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        from cogs.reminders.store import parse_absolute_datetime, resolve_timezone

        try:
            zone = resolve_timezone(self.draft.timezone)
            moment = parse_absolute_datetime(str(self.when.value), zone)
        except ValueError as exc:
            await safe_reply(interaction, content=f"❌ {exc}", ephemeral=True)
            return

        self.draft.once_at = as_utc(moment)
        await self.cog.after_schedule(interaction, self.draft)


class IntervalModal(discord.ui.Modal):
    """How often a repeating reminder should run."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="How often should it repeat?")
        self.cog = cog
        self.draft = draft
        self.every = discord.ui.TextInput(
            label="Repeat every",
            placeholder="30m, 2h, 1d 6h",
            default=(
                format_duration(draft.interval_seconds)
                if draft.interval_seconds
                else None
            ),
            max_length=60,
            required=True,
        )
        self.add_item(self.every)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        from cogs.reminders.store import parse_duration

        try:
            seconds = parse_duration(str(self.every.value))
        except ValueError as exc:
            await safe_reply(interaction, content=f"❌ {exc}", ephemeral=True)
            return

        self.draft.interval_seconds = seconds
        await self.cog.after_schedule(interaction, self.draft)


class TimeOfDayModal(discord.ui.Modal):
    """Wall-clock time for daily / weekly / monthly reminders."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="What time of day?")
        self.cog = cog
        self.draft = draft
        self.at = discord.ui.TextInput(
            label="Time of day",
            placeholder="18:30 or 6:30pm",
            default=(
                format_time_of_day(draft.hour, draft.minute)
                if draft.hour is not None and draft.minute is not None
                else None
            ),
            max_length=10,
            required=True,
        )
        self.add_item(self.at)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        from cogs.reminders.store import parse_time_of_day

        try:
            hour, minute = parse_time_of_day(str(self.at.value))
        except ValueError as exc:
            await safe_reply(interaction, content=f"❌ {exc}", ephemeral=True)
            return

        self.draft.hour = hour
        self.draft.minute = minute
        await self.cog.after_schedule(interaction, self.draft)


class TimezoneModal(discord.ui.Modal):
    """Free-text IANA timezone."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="Type a timezone")
        self.cog = cog
        self.draft = draft
        self.zone = discord.ui.TextInput(
            label="IANA timezone",
            placeholder="Asia/Kolkata",
            default=draft.timezone,
            max_length=60,
            required=True,
        )
        self.add_item(self.zone)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        from cogs.reminders.store import resolve_timezone

        try:
            resolve_timezone(str(self.zone.value))
        except ValueError as exc:
            await safe_reply(interaction, content=f"❌ {exc}", ephemeral=True)
            return

        self.draft.timezone = str(self.zone.value).strip()
        await self.cog.step_channel(interaction, self.draft)


class ChannelIdModal(discord.ui.Modal):
    """Fallback for channels the native picker cannot show."""

    def __init__(self, cog: Any, draft: ReminderDraft) -> None:
        super().__init__(title="Channel ID")
        self.cog = cog
        self.draft = draft
        self.channel = discord.ui.TextInput(
            label="Channel ID",
            placeholder="123456789012345678",
            max_length=25,
            required=True,
        )
        self.add_item(self.channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        raw = str(self.channel.value).strip().strip("<#>")
        if not raw.isdigit():
            await safe_reply(interaction, content="❌ That does not look like a channel ID.", ephemeral=True)
            return

        guild = interaction.guild
        channel = guild.get_channel(int(raw)) if guild is not None else None
        if channel is None and guild is not None:
            try:
                channel = await self.cog.fetch_guild_channel(guild, int(raw))
            except discord.HTTPException:
                channel = None
        if channel is None:
            await safe_reply(interaction, content="❌ I could not find that channel in this server.", ephemeral=True)
            return

        self.draft.channel_id = int(raw)
        await self.cog.after_channel(interaction, self.draft)


def _once_default(draft: ReminderDraft) -> Optional[str]:
    if draft.once_at is None:
        return None
    from cogs.reminders.store import resolve_timezone

    try:
        zone = resolve_timezone(draft.timezone)
    except ValueError:
        return None
    return as_utc(draft.once_at).astimezone(zone).strftime("%Y-%m-%d %H:%M")
