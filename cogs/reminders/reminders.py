"""Reminder cog: slash commands, interactive dashboard and the delivery loop.

The cog is the only component that talks to Discord *and* MongoDB. The UI
(:mod:`cogs.reminders.views`) owns layout and calls the ``step_*`` /
``finish_draft`` methods here; the storage layer
(:mod:`cogs.reminders.store`) owns the schedule maths and the atomic claim that
makes duplicate notifications impossible.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.reminders import config
from cogs.reminders.store import (
    ReminderDraft,
    ReminderStore,
    as_utc,
    describe_schedule,
    discord_timestamp,
    new_reminder_id,
    next_occurrence,
    resolve_timezone,
    schedule_type_of,
    validate_schedule,
)
from cogs.reminders.views import (
    ChannelStep,
    ConfirmActionView,
    ConfirmStep,
    DashboardView,
    DetailsModal,
    DayOfMonthStep,
    FrequencyStep,
    ListPagerView,
    ManageView,
    ReminderDetailView,
    RoleStep,
    ScheduleStep,
    TimezoneStep,
    WeekdayStep,
    build_dashboard_embed,
    build_draft_embed,
    build_reminder_embed,
    has_manage_permission,
    render,
    truncate,
)
from cogs.interaction_utils import safe_reply, send_modal

logger = logging.getLogger(__name__)

UTC = timezone.utc

# Ordered steps for each wizard flow. ``create`` walks the whole thing; the
# ``edit_*`` flows jump straight to the one thing the admin asked to change and
# then land on the confirmation panel, so every change is reviewed before it is
# written to the database.
FLOW_STEPS: Dict[str, Tuple[str, ...]] = {
    "create": ("details", "frequency", "schedule", "timezone", "channel", "role", "confirm"),
    "edit_details": ("confirm",),
    "edit_frequency": ("frequency", "schedule", "timezone", "confirm"),
    "edit_time": ("schedule", "confirm"),
    "edit_timezone": ("timezone", "confirm"),
    "edit_channel": ("channel", "confirm"),
    "edit_role": ("role", "confirm"),
}

STATUS_ORDER = {"active": 0, "paused": 1, "failed": 2, "completed": 3}
STATUS_ICON = {"active": "🟢", "paused": "⏸️", "failed": "🔴", "completed": "✅"}

UNAUTHORIZED_MESSAGE = "❌ You need the **Manage Server** permission to manage reminders."
NO_DATABASE_MESSAGE = "❌ The reminder database is unavailable right now. Please try again in a moment."


class OutgoingMessage(NamedTuple):
    """Exactly what will be posted when a reminder fires."""

    content: Optional[str]
    embed: discord.Embed
    allowed_mentions: discord.AllowedMentions
    warning: Optional[str]


def order_reminders(reminders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Active first, then by next run - the order an admin expects to see."""

    def sort_key(reminder: Dict[str, Any]) -> Tuple[int, datetime, str]:
        status = str(reminder.get("status") or "active")
        next_run = as_utc(reminder.get("next_run_at"))
        return (
            STATUS_ORDER.get(status, 4),
            next_run or datetime.max.replace(tzinfo=UTC),
            str(reminder.get("name") or "").lower(),
        )

    return sorted(reminders, key=sort_key)


class ReminderCog(commands.Cog, name="Reminders"):
    """Scheduled reminders with an interactive admin dashboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._collection = None
        self._store: Optional[ReminderStore] = None
        self._db_warned = False
        # Occurrence keys currently being delivered in this process. The
        # database claim already guarantees at-most-once, this is the local
        # belt-and-braces guard.
        self._inflight: set[str] = set()
        self._db_configure()

    # ------------------------------------------------------------------
    # Storage plumbing
    # ------------------------------------------------------------------

    def _db_configure(self) -> None:
        client = getattr(self.bot, "mongo_client", None)
        if client is None:
            return
        try:
            self._collection = client[config.MONGO_DATABASE][config.REMINDER_COLLECTION]
            self._store = ReminderStore(self._collection)
        except Exception:
            logger.exception("Could not open the reminder collection")
            self._collection = None
            self._store = None

    @property
    def store(self) -> Optional[ReminderStore]:
        """The reminder store, re-resolved if the bot reconnected MongoDB."""
        if self._store is None:
            self._db_configure()
        elif getattr(self._store.collection, "database", None) is not None:
            # After a MongoDB reconnect the shared client is replaced; make sure
            # we are not holding the old, closed collection handle.
            client = getattr(self.bot, "mongo_client", None)
            if client is not None and self._store.collection.database.client is not client:
                self._db_configure()
        return self._store

    async def cog_load(self) -> None:
        store = self.store
        if store is not None:
            try:
                await store.ensure_indexes()
            except Exception:
                logger.warning("Could not create reminder indexes (will retry on demand)", exc_info=True)
        self.scheduler.start()
        logger.info("Reminder cog loaded (tick every %ss)", config.SCHEDULER_TICK_SECONDS)

    def cog_unload(self) -> None:
        self.scheduler.cancel()

    # ------------------------------------------------------------------
    # Permission / DB guards used by every entry point
    # ------------------------------------------------------------------

    async def _ensure_authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await safe_reply(interaction, content="❌ Reminders are server-only — run this in your server.", ephemeral=True)
            return False
        if not has_manage_permission(interaction):
            await safe_reply(interaction, content=UNAUTHORIZED_MESSAGE, ephemeral=True)
            return False
        if self.store is None and not self._db_warned:
            self._db_warned = True
            logger.error("Reminder command used while the MongoDB client is unavailable")
        return True

    async def _defer_for_edit(self, interaction: discord.Interaction) -> bool:
        """Claim the 3-second window while keeping the panel on one message.

        A deferred *message update* (used when the panel message exists) makes
        the following ``safe_edit`` rewrite that same ephemeral panel instead of
        spawning a second private message on every button press.
        """
        if interaction.response.is_done():
            return True
        try:
            if interaction.message is not None:
                await interaction.response.defer()
            elif interaction.type is discord.InteractionType.modal_submit:
                # A modal opened straight from a slash command has no message to
                # update, so create a private one for the edit to land on.
                await interaction.response.defer(ephemeral=True, thinking=True)
            else:
                await interaction.response.defer(ephemeral=True)
            return True
        except discord.HTTPException as exc:
            logger.debug("Could not defer reminder interaction: %s", exc)
            return False

    async def _load_reminders(self, guild_id: int) -> List[Dict[str, Any]]:
        store = self.store
        if store is None:
            return []
        return order_reminders(await store.list_for_guild(guild_id))

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    @tasks.loop(seconds=config.SCHEDULER_TICK_SECONDS)
    async def scheduler(self) -> None:
        """Deliver everything that is due, at most once per occurrence."""
        store = self.store
        if store is None:
            return
        if not self.bot.is_ready():
            # Tick without claiming anything: before READY (or right after a
            # lost gateway connection) channels and roles are not resolvable,
            # and claiming here would mark perfectly good reminders as failed.
            return

        now = datetime.now(UTC)
        try:
            due = await store.claim_due(now, config.MAX_REMINDERS_PER_TICK)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:
            logger.error("Reminder scheduler could not claim due reminders", exc_info=True)
            return

        for reminder in due:
            try:
                await self._deliver(reminder)
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception:
                logger.exception(
                    "Reminder delivery crashed for %s", reminder.get("reminder_id")
                )
            # Space out sends so a batch of reminders does not trip Discord's
            # per-channel rate limits.
            await asyncio.sleep(0.5)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # The client was closed (or never logged in). Stop the loop instead
            # of letting it tick against an unpopulated cache.
            logger.debug("Reminder scheduler stopped before the bot became ready")
            self.scheduler.cancel()
            return
        # Align to a tick boundary so a restart picks up overdue reminders
        # within one tick instead of a random fraction of one.
        now = time.time()
        delay = config.SCHEDULER_TICK_SECONDS - (now % config.SCHEDULER_TICK_SECONDS)
        if delay > 0:
            await asyncio.sleep(delay)

    def _role_pingable(self, role: discord.Role, guild: discord.Guild) -> bool:
        """A role can be pinged when it is mentionable, or via Mention @everyone."""
        if role.mentionable:
            return True
        me = guild.me
        return bool(me is not None and me.guild_permissions.mention_everyone)

    def build_message(self, reminder: Dict[str, Any], guild: Optional[discord.Guild]) -> OutgoingMessage:
        """Compose the exact message a reminder posts (used to send and to preview)."""
        name = reminder.get("name") or "Reminder"
        body = reminder.get("message") or ""
        role_id = reminder.get("role_id")
        warning: Optional[str] = None
        ping = ""

        if role_id and guild is not None:
            role = guild.get_role(int(role_id))
            if role is None:
                warning = f"The role <@&{role_id}> no longer exists, so this reminder was posted without a ping."
            elif self._role_pingable(role, guild):
                ping = role.mention
            else:
                # Never sneak an @everyone in: post the role's name instead of a
                # mention that the bot cannot legitimately make.
                ping = f"**{discord.utils.escape_markdown(role.name)}**"
                warning = config.MENTION_WARNING
        elif role_id:
            warning = "No role ping: the server is unavailable to the bot right now."

        embed = discord.Embed(
            title=f"⏰ {truncate(name, 240)}",
            description=truncate(body, 4000) or "\u200b",
            color=config.EMBED_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Reminder • {truncate(describe_schedule(reminder), 200)} • ID {reminder.get('reminder_id')}")

        mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=ping.startswith("<@&"),
        )
        return OutgoingMessage(content=ping or None, embed=embed, allowed_mentions=mentions, warning=warning)

    async def _resolve_channel(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None

    async def _deliver(self, reminder: Dict[str, Any], *, test: bool = False) -> Tuple[bool, Optional[str]]:
        """Post one reminder. Returns ``(delivered, error)``.

        ``test=True`` posts the same message without touching the schedule or
        the delivery counters.
        """
        store = self.store
        key = f"{reminder.get('reminder_id')}:{reminder.get('last_run_key')}"
        if not test:
            if key in self._inflight:
                logger.debug("Skipping reminder %s: already being delivered", reminder.get("reminder_id"))
                return False, "already being delivered"
            self._inflight.add(key)

        try:
            channel = await self._resolve_channel(int(reminder.get("channel_id") or 0))
            if channel is None:
                return await self._handle_failure(
                    reminder,
                    f"I could not reach the target channel (<#{reminder.get('channel_id')}>). "
                    "It may have been deleted, or I lost access to it.",
                    pause=True,
                    test=test,
                )

            guild = self.bot.get_guild(int(reminder.get("guild_id") or 0))
            if guild is None:
                guild = getattr(channel, "guild", None)

            # ``abc.Messageable`` is the honest question here (“can I post
            # here?”) and covers text/voice/stage channels and threads without
            # needing to enumerate every channel type Discord adds.
            if not isinstance(channel, discord.abc.Messageable):
                return await self._handle_failure(
                    reminder,
                    f"<#{reminder.get('channel_id')}> is not a channel I can post messages in.",
                    pause=True,
                    test=test,
                )

            outgoing = self.build_message(reminder, guild)
            if outgoing.warning:
                logger.info("Reminder %s: %s", reminder.get("reminder_id"), outgoing.warning)

            try:
                await channel.send(
                    content=outgoing.content,
                    embed=outgoing.embed,
                    allowed_mentions=outgoing.allowed_mentions,
                )
            except discord.Forbidden:
                return await self._handle_failure(
                    reminder,
                    f"I do not have permission to post in <#{reminder.get('channel_id')}> "
                    "(I need **View Channel** and **Send Messages**).",
                    pause=True,
                    test=test,
                )
            except discord.NotFound:
                return await self._handle_failure(
                    reminder,
                    f"<#{reminder.get('channel_id')}> no longer exists.",
                    pause=True,
                    test=test,
                )
            except discord.HTTPException as exc:
                return await self._handle_failure(
                    reminder,
                    f"Discord rejected the message ({exc.status}: {exc.text}).",
                    pause=False,
                    test=test,
                )

            if not test and store is not None:
                try:
                    await store.record_success(reminder)
                except Exception:
                    logger.warning(
                        "Could not record successful delivery of %s", reminder.get("reminder_id"), exc_info=True
                    )
            return True, None
        finally:
            if not test:
                self._inflight.discard(key)

    async def _handle_failure(
        self,
        reminder: Dict[str, Any],
        message: str,
        *,
        pause: bool,
        test: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """Record a failed delivery, pausing or retrying as appropriate."""
        if test:
            return False, message

        store = self.store
        attempts = int(reminder.get("failure_count") or 0) + 1
        if not pause and attempts >= config.MAX_CONSECUTIVE_FAILURES:
            pause = True
            message = f"{message} (given up after {attempts} consecutive failures)"

        retry_at: Optional[datetime] = None
        if not pause and schedule_type_of(reminder) == config.SCHEDULE_ONCE:
            # A one-off never recurs, so a transient failure is worth retrying.
            # Recurring reminders already have their next occurrence scheduled,
            # and retrying would deliver twice.
            delay = min(config.RETRY_DELAY_SECONDS * attempts, config.RETRY_MAX_DELAY_SECONDS)
            retry_at = datetime.now(UTC) + timedelta(seconds=delay)

        if store is not None:
            try:
                await store.record_failure(reminder, message, pause=pause, retry_at=retry_at)
            except Exception:
                logger.warning("Could not record reminder failure", exc_info=True)

        logger.warning("Reminder %s failed: %s", reminder.get("reminder_id"), message)

        if pause:
            note = f"⏰ Reminder **{truncate(reminder.get('name') or 'Reminder', 80)}** was stopped: {message}"
            await self._notify_creator(reminder, note)
        return False, message

    async def _notify_creator(self, reminder: Dict[str, Any], note: str) -> None:
        """Best-effort DM to whoever created the reminder."""
        creator_id = int(reminder.get("created_by") or 0)
        if not creator_id:
            return
        try:
            user = self.bot.get_user(creator_id) or await self.bot.fetch_user(creator_id)
            if user is not None and not user.bot:
                await user.send(note)
        except discord.Forbidden:
            logger.debug("Reminder creator %s has DMs closed", creator_id)
        except Exception:
            logger.debug("Could not notify reminder creator %s", creator_id, exc_info=True)

    # ------------------------------------------------------------------
    # Wizard navigation
    # ------------------------------------------------------------------

    def _next_step(self, flow: str, current: str, draft: ReminderDraft) -> str:
        steps = FLOW_STEPS.get(flow, FLOW_STEPS["create"])
        try:
            index = steps.index(current)
        except ValueError:
            index = 0
        for step in steps[index + 1:]:
            if step == "timezone" and not draft.needs_timezone():
                continue
            return step
        return "confirm"

    async def _advance(self, interaction: discord.Interaction, draft: ReminderDraft, current: str) -> None:
        step = self._next_step(draft.flow, current, draft)
        if step == "frequency":
            await self.step_frequency(interaction, draft)
        elif step == "schedule":
            await self.step_schedule(interaction, draft)
        elif step == "timezone":
            await self.step_timezone(interaction, draft)
        elif step == "channel":
            await self.step_channel(interaction, draft)
        elif step == "role":
            await self.step_role(interaction, draft)
        else:
            await self.step_confirm(interaction, draft)

    async def start_create(self, interaction: discord.Interaction) -> None:
        """Open the creation wizard (starts with the details modal)."""
        if not await self._ensure_authorized(interaction):
            return
        draft = ReminderDraft(guild_id=interaction.guild.id, author_id=interaction.user.id, flow="create")
        draft.channel_id = interaction.channel_id
        logger.info("Reminder wizard opened by %s in guild %s", interaction.user, interaction.guild.id)
        await send_modal(interaction, DetailsModal(self, draft))

    async def step_frequency(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return
        draft.page = 0
        embed = build_draft_embed(
            draft,
            title="🔁 Step 2 of 6 • Frequency",
            step_hint="Choose how often this reminder should run.",
        )
        await render(interaction, embed=embed, view=FrequencyStep(self, draft))

    async def step_schedule(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return

        if draft.schedule_type == config.SCHEDULE_WEEKLY and not draft.weekdays:
            await self._render_weekdays(interaction, draft)
            return
        if draft.schedule_type == config.SCHEDULE_MONTHLY and not draft.day_of_month:
            await self._render_days(interaction, draft)
            return

        embed = build_draft_embed(
            draft,
            title="🕒 Step 3 of 6 • When",
            step_hint="Set the timing for this reminder.",
        )
        await render(interaction, embed=embed, view=ScheduleStep(self, draft))

    async def _render_weekdays(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        embed = build_draft_embed(
            draft,
            title="📅 Step 3 of 6 • Weekdays",
            step_hint="Pick the weekdays this reminder should fire on.",
        )
        await render(interaction, embed=embed, view=WeekdayStep(self, draft))

    async def _render_days(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        embed = build_draft_embed(
            draft,
            title="🗓️ Step 3 of 6 • Day of month",
            step_hint=(
                "Pick the day of the month. If a month is shorter than the day you pick, "
                "the reminder fires on its last day instead."
            ),
        )
        await render(interaction, embed=embed, view=DayOfMonthStep(self, draft))

    async def step_timezone(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return
        embed = build_draft_embed(
            draft,
            title="🌐 Timezone",
            step_hint="Pick the timezone the times above are written in.",
        )
        await render(interaction, embed=embed, view=TimezoneStep(self, draft))

    async def step_channel(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return
        embed = build_draft_embed(
            draft,
            title="📢 Destination channel",
            step_hint="Pick the channel that should receive this reminder.",
        )
        await render(interaction, embed=embed, view=ChannelStep(self, draft))

    async def step_role(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return
        embed = build_draft_embed(
            draft,
            title="🔔 Notification role",
            step_hint=(
                "Pick a role to ping when this reminder fires, or press **No role** to post without a ping. "
                "Reminders never ping `@everyone` or `@here`."
            ),
        )
        await render(interaction, embed=embed, view=RoleStep(self, draft))

    async def step_confirm(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        if not await self._ensure_authorized(interaction):
            return
        embed = build_draft_embed(
            draft,
            title="✅ Review reminder",
            step_hint="Check everything below, then save.",
        )
        error = self.validate_draft(draft, interaction=interaction)
        if error:
            embed.add_field(name="⚠️ Fix this first", value=error, inline=False)
        await render(interaction, embed=embed, view=ConfirmStep(self, draft))

    async def after_schedule(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        await self._advance(interaction, draft, "schedule")

    async def after_channel(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        await self._advance(interaction, draft, "channel")

    async def after_role(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        await self._advance(interaction, draft, "role")

    async def cancel_wizard(self, interaction: discord.Interaction) -> None:
        """Drop the in-progress wizard without writing anything."""
        if not await self._ensure_authorized(interaction):
            return
        embed = discord.Embed(
            title="❌ Cancelled",
            description="Nothing was saved. Open the dashboard whenever you want to try again.",
            color=config.MUTED_COLOR,
        )
        await render(interaction, embed=embed, view=DashboardView(self, interaction.user.id))

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def validate_draft(self, draft: ReminderDraft, *, interaction: discord.Interaction) -> Optional[str]:
        """Validate everything the admin configured, returning a friendly error."""
        if not draft.name or not draft.name.strip():
            return "❌ The reminder needs a name."
        if len(draft.name) > config.MAX_NAME_LENGTH:
            return f"❌ The name cannot be longer than **{config.MAX_NAME_LENGTH}** characters."
        if not draft.message or not draft.message.strip():
            return "❌ The reminder needs a message."
        if len(draft.message) > config.MAX_MESSAGE_LENGTH:
            return f"❌ The message cannot be longer than **{config.MAX_MESSAGE_LENGTH}** characters."
        if not draft.channel_id:
            return "❌ Pick a channel for the reminder."
        try:
            resolve_timezone(draft.timezone)
        except ValueError as exc:
            return f"❌ {exc}"

        error = validate_schedule(draft.schedule(), datetime.now(UTC))
        if error:
            return error

        guild = interaction.guild
        if guild is None:
            return "❌ This has to be done inside the server."
        channel = guild.get_channel(int(draft.channel_id))
        if channel is None:
            return "❌ I can no longer see that channel — pick another one."
        me = guild.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not permissions.view_channel or not permissions.send_messages:
                return (
                    f"❌ I need **View Channel** and **Send Messages** in <#{draft.channel_id}> "
                    "before I can post reminders there."
                )
        return None

    def compute_next_run(self, draft: ReminderDraft, now: datetime, anchor: Optional[datetime] = None) -> Optional[datetime]:
        if draft.schedule_type == config.SCHEDULE_ONCE:
            return as_utc(draft.once_at)
        return next_occurrence(draft.schedule(), now, anchor=anchor)

    def draft_document(self, draft: ReminderDraft, now: datetime,
                       existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Turn a draft into the reminder document (or the update patch)."""
        anchor = as_utc(existing.get("next_run_at")) if existing else None
        document: Dict[str, Any] = {
            "name": draft.name.strip(),
            "message": draft.message.strip(),
            "schedule_type": draft.schedule_type,
            "hour": draft.hour,
            "minute": draft.minute,
            "weekdays": list(draft.weekdays),
            "day_of_month": draft.day_of_month,
            "once_at": as_utc(draft.once_at),
            "interval_seconds": draft.interval_seconds,
            "timezone": draft.timezone,
            "channel_id": int(draft.channel_id),
            "role_id": int(draft.role_id) if draft.role_id else None,
            "next_run_at": self.compute_next_run(draft, now, anchor),
            "updated_at": now,
        }
        if existing is None:
            document.update(
                {
                    "guild_id": int(draft.guild_id),
                    "reminder_id": None,  # filled in by the caller
                    "created_by": int(draft.author_id),
                    "created_at": now,
                    "status": "active",
                    "delivered_count": 0,
                    "failure_count": 0,
                    "last_error": None,
                    "paused_at": None,
                    "last_run_key": None,
                    "claimed_at": None,
                }
            )
        else:
            status = str(existing.get("status") or "active")
            if status in ("failed", "completed"):
                # An edit is an explicit fix, so put it back on the schedule.
                status = "active"
            document.update(
                {
                    "status": status,
                    "failure_count": 0,
                    "last_error": None,
                    "paused_at": None if status == "active" else existing.get("paused_at"),
                    # A new occurrence key means the edited reminder can fire
                    # even if it inherited a "already claimed" marker.
                    "last_run_key": None,
                    "claimed_at": None,
                }
            )
        return document

    async def finish_draft(self, interaction: discord.Interaction, draft: ReminderDraft) -> None:
        """Write the draft to MongoDB and show the result."""
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)

        creating = not draft.reminder_id
        error = self.validate_draft(draft, interaction=interaction)
        if error:
            embed = build_draft_embed(draft, title="⚠️ Not saved yet", step_hint=error)
            await render(interaction, embed=embed, view=ConfirmStep(self, draft))
            return

        now = datetime.now(UTC)
        try:
            if creating:
                count = await store.count_for_guild(draft.guild_id)
                if count >= config.MAX_REMINDERS_PER_GUILD:
                    await render(
                        interaction,
                        embed=discord.Embed(
                            title="⚠️ Reminder limit reached",
                            description=(
                                f"This server already has **{count}** reminders "
                                f"(limit **{config.MAX_REMINDERS_PER_GUILD}**). "
                                "Delete one before creating another."
                            ),
                            color=config.WARNING_COLOR,
                        ),
                        view=DashboardView(self, interaction.user.id),
                    )
                    return

                document = self.draft_document(draft, now)
                document["reminder_id"] = new_reminder_id()
                await store.insert(document)
                reminder = document
                note = "Reminder created. It will keep running across restarts."
                logger.info(
                    "Reminder %s created by %s in guild %s (%s)",
                    document["reminder_id"], interaction.user, draft.guild_id, describe_schedule(document),
                )
            else:
                existing = await store.get(draft.guild_id, draft.reminder_id)
                if existing is None:
                    await render(
                        interaction,
                        embed=discord.Embed(
                            title="❌ Reminder not found",
                            description="It may have been deleted in the meantime.",
                            color=config.ERROR_COLOR,
                        ),
                        view=DashboardView(self, interaction.user.id),
                    )
                    return
                patch = self.draft_document(draft, now, existing=existing)
                await store.update(draft.guild_id, draft.reminder_id, patch)
                reminder = await store.get(draft.guild_id, draft.reminder_id) or patch
                note = "Changes saved."
                logger.info("Reminder %s updated by %s", draft.reminder_id, interaction.user)
        except Exception:
            logger.exception("Could not save reminder for guild %s", draft.guild_id)
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Could not save",
                    description="The database rejected that change. Please try again in a moment.",
                    color=config.ERROR_COLOR,
                ),
                view=ConfirmStep(self, draft),
            )
            return

        await self._render_detail(interaction, reminder, note=note)

    # ------------------------------------------------------------------
    # Panels
    # ------------------------------------------------------------------

    async def open_dashboard(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        guild_id = interaction.guild.id
        try:
            stats = await store.stats_for_guild(guild_id)
            reminders = await self._load_reminders(guild_id)
        except Exception:
            logger.exception("Could not load reminder dashboard for guild %s", guild_id)
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Dashboard unavailable",
                    description="I could not read the reminder database. Please try again in a moment.",
                    color=config.ERROR_COLOR,
                ),
            )
            return

        embed = build_dashboard_embed(stats, reminders, datetime.now(UTC))
        await render(interaction, embed=embed, view=DashboardView(self, interaction.user.id))

    async def show_manage(self, interaction: discord.Interaction, page: int = 0,
                          note: Optional[str] = None) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        guild_id = interaction.guild.id
        try:
            reminders = await self._load_reminders(guild_id)
        except Exception:
            logger.exception("Could not list reminders for guild %s", guild_id)
            reminders = []

        if not reminders:
            embed = discord.Embed(
                title="📋 Manage reminders",
                description="There are no reminders yet. Create the first one!",
                color=config.EMBED_COLOR,
            )
            await render(interaction, embed=embed, view=DashboardView(self, interaction.user.id))
            return

        pages = max(1, math.ceil(len(reminders) / config.SELECT_PAGE_SIZE))
        page = max(0, min(int(page), pages - 1))
        chunk = reminders[page * config.SELECT_PAGE_SIZE:(page + 1) * config.SELECT_PAGE_SIZE]

        embed = discord.Embed(
            title="📋 Manage reminders",
            description=(
                f"Showing **{len(chunk)}** of **{len(reminders)}** reminders"
                + (f" • page **{page + 1}/{pages}**" if pages > 1 else "")
                + (f"\n\n{note}" if note else "")
                + "\n\nSelect one below to edit, pause, reschedule or delete it."
            ),
            color=config.EMBED_COLOR,
        )
        for reminder in chunk[:8]:
            status = str(reminder.get("status") or "active")
            next_run = as_utc(reminder.get("next_run_at"))
            when = discord_timestamp(next_run, "R") if status == "active" and next_run else status.title()
            embed.add_field(
                name=f"{STATUS_ICON.get(status, '⚪')} {truncate(reminder.get('name') or 'Untitled', 200)}",
                value=(
                    f"{describe_schedule(reminder)}\n"
                    f"<#{reminder.get('channel_id')}> • {when} • `{reminder.get('reminder_id')}`"
                ),
                inline=False,
            )

        await render(interaction, embed=embed, view=ManageView(self, interaction.user.id, chunk, page, pages))

    async def show_list(self, interaction: discord.Interaction, page: int = 0) -> None:
        """Compact, paginated listing for ``/reminder list``."""
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        guild_id = interaction.guild.id
        try:
            reminders = await self._load_reminders(guild_id)
        except Exception:
            logger.exception("Could not list reminders for guild %s", guild_id)
            reminders = []

        per_page = 8
        pages = max(1, math.ceil(len(reminders) / per_page))
        page = max(0, min(int(page), pages - 1))
        chunk = reminders[page * per_page:(page + 1) * per_page]

        embed = discord.Embed(
            title="⏰ All reminders",
            color=config.EMBED_COLOR,
            description=f"**{len(reminders)}** reminder(s) stored for this server." if reminders else "No reminders yet.",
        )
        for reminder in chunk:
            status = str(reminder.get("status") or "active")
            next_run = as_utc(reminder.get("next_run_at"))
            lines = [describe_schedule(reminder)]
            if status == "active" and next_run is not None:
                lines.append(f"Next {discord_timestamp(next_run, 'f')} ({discord_timestamp(next_run, 'R')})")
            else:
                lines.append(f"Status: **{status.title()}**")
            lines.append(f"Channel <#{reminder.get('channel_id')}> • ID `{reminder.get('reminder_id')}`")
            embed.add_field(
                name=f"{STATUS_ICON.get(status, '⚪')} {truncate(reminder.get('name') or 'Untitled', 200)}",
                value="\n".join(lines),
                inline=False,
            )
        if pages > 1:
            embed.set_footer(text=f"Page {page + 1} of {pages}")

        await render(interaction, embed=embed, view=ListPagerView(self, interaction.user.id, page, pages))

    async def show_detail(self, interaction: discord.Interaction, reminder_id: str,
                          note: Optional[str] = None) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        try:
            reminder = await store.get(interaction.guild.id, reminder_id)
        except Exception:
            logger.exception("Could not load reminder %s", reminder_id)
            reminder = None

        if reminder is None:
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Reminder not found",
                    description=f"I have no reminder with the ID `{reminder_id}`.",
                    color=config.ERROR_COLOR,
                ),
                view=DashboardView(self, interaction.user.id),
            )
            return

        await self._render_detail(interaction, reminder, note=note)

    async def _render_detail(self, interaction: discord.Interaction, reminder: Dict[str, Any],
                             note: Optional[str] = None) -> None:
        embed = build_reminder_embed(reminder, now=datetime.now(UTC), note=note)
        await render(interaction, embed=embed, view=ReminderDetailView(self, interaction.user.id, reminder))

    async def show_help(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_authorized(interaction):
            return
        embed = discord.Embed(title="❓ Reminder help", color=config.EMBED_COLOR)
        embed.add_field(
            name="Creating",
            value=(
                "Press **➕ Create reminder** and follow the six steps: name & message, frequency, "
                "timing, timezone, channel, then the role to ping."
            ),
            inline=False,
        )
        embed.add_field(
            name="Types of schedule",
            value=(
                "**One time** — fires once at an exact date and time.\n"
                "**Repeating interval** — every `30m`, `2h`, `1d`…\n"
                "**Daily** — every day at a set time.\n"
                "**Weekly** — on the weekdays you pick.\n"
                "**Monthly** — on a day of the month (short months use their last day)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Managing",
            value=(
                "Open **📋 Manage reminders** to edit, reschedule, change channel/role, pause, resume, "
                "preview, send a test or delete. Changes apply instantly and survive restarts."
            ),
            inline=False,
        )
        embed.add_field(
            name="Pings",
            value=(
                "Reminders ping the role you choose. A role can only be pinged if it is mentionable, or if "
                "the bot has **Mention @everyone**; otherwise the role's name is posted instead. Reminders "
                "never ping `@everyone` or `@here`."
            ),
            inline=False,
        )
        embed.set_footer(text="Only members with Manage Server (or Administrators) can open these panels.")
        await render(interaction, embed=embed, view=DashboardView(self, interaction.user.id))

    # ------------------------------------------------------------------
    # Per-reminder actions
    # ------------------------------------------------------------------

    async def toggle_pause(self, interaction: discord.Interaction, reminder: Dict[str, Any], *,
                           pause: bool) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        reminder_id = str(reminder.get("reminder_id"))
        now = datetime.now(UTC)

        if pause:
            patch: Dict[str, Any] = {"status": "paused", "paused_at": now}
            note = "⏸️ Paused — this reminder will not fire until you resume it."
        else:
            next_run = self._next_run_on_resume(reminder, now)
            if next_run is None:
                await self._render_detail(
                    interaction,
                    reminder,
                    note="❌ I could not work out the next run time. Reconfigure the schedule first.",
                )
                return
            patch = {
                "status": "active",
                "paused_at": None,
                "next_run_at": next_run,
                "failure_count": 0,
                "last_error": None,
                "claimed_at": None,
                "last_run_key": None,
            }
            note = f"▶️ Resumed — next run {discord_timestamp(next_run, 'R')}."

        try:
            await store.update(interaction.guild.id, reminder_id, patch)
            updated = await store.get(interaction.guild.id, reminder_id)
        except Exception:
            logger.exception("Could not change reminder status for %s", reminder_id)
            await self._render_detail(interaction, reminder, note="❌ Could not save that change. Try again.")
            return

        await self._render_detail(interaction, updated or reminder, note=note)

    def _next_run_on_resume(self, reminder: Dict[str, Any], now: datetime) -> Optional[datetime]:
        if schedule_type_of(reminder) == config.SCHEDULE_ONCE:
            once_at = as_utc(reminder.get("once_at"))
            if once_at is not None and once_at > now:
                return once_at
            # The scheduled moment passed while it was paused: fire shortly
            # instead of silently dropping the reminder.
            return now + timedelta(seconds=60)
        return next_occurrence(reminder, now)

    async def bulk_status(self, interaction: discord.Interaction, status: str) -> None:
        """Pause or resume every reminder in the guild."""
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        guild_id = interaction.guild.id
        now = datetime.now(UTC)
        changed = 0

        try:
            reminders = await store.list_for_guild(guild_id)
            if status == "active":
                for reminder in reminders:
                    if str(reminder.get("status")) != "paused":
                        continue
                    next_run = self._next_run_on_resume(reminder, now)
                    if next_run is None:
                        continue
                    await store.update(
                        guild_id,
                        reminder.get("reminder_id"),
                        {
                            "status": "active",
                            "paused_at": None,
                            "next_run_at": next_run,
                            "failure_count": 0,
                            "last_error": None,
                            "claimed_at": None,
                            "last_run_key": None,
                        },
                    )
                    changed += 1
            else:
                changed = await store.set_status_for_all(guild_id, "paused", only_statuses=("active",))
        except Exception:
            logger.exception("Bulk %s failed for guild %s", status, guild_id)
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Could not update reminders",
                    description="The database rejected that change.",
                    color=config.ERROR_COLOR,
                ),
                view=DashboardView(self, interaction.user.id),
            )
            return

        verb = "resumed" if status == "active" else "paused"
        await self.open_dashboard(interaction)
        await safe_reply(
            interaction,
            content=f"✅ {changed} reminder(s) {verb}.",
            ephemeral=True,
        )

    async def show_delete_confirm(self, interaction: discord.Interaction, reminder: Dict[str, Any]) -> None:
        if not await self._ensure_authorized(interaction):
            return
        reminder_id = str(reminder.get("reminder_id"))
        embed = discord.Embed(
            title="🗑️ Delete this reminder?",
            description=(
                f"**{truncate(reminder.get('name') or 'Untitled', 200)}**\n"
                f"{describe_schedule(reminder)}\n\n"
                "This cannot be undone."
            ),
            color=config.ERROR_COLOR,
        )
        view = ConfirmActionView(
            self,
            interaction.user.id,
            confirm_label="🗑️ Delete forever",
            confirm_style=discord.ButtonStyle.danger,
            on_confirm=lambda target: self.delete_reminder(target, reminder_id),
            on_cancel=lambda target: self.show_detail(target, reminder_id, note="Nothing was deleted."),
        )
        await render(interaction, embed=embed, view=view)

    async def delete_reminder(self, interaction: discord.Interaction, reminder_id: str) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return

        await self._defer_for_edit(interaction)
        try:
            deleted = await store.delete(interaction.guild.id, reminder_id)
        except Exception:
            logger.exception("Could not delete reminder %s", reminder_id)
            deleted = False

        if not deleted:
            await self.show_manage(interaction, 0, note=f"❌ I could not delete `{reminder_id}`.")
            return

        logger.info("Reminder %s deleted by %s", reminder_id, interaction.user)
        await self.show_manage(interaction, 0, note=f"🗑️ Deleted reminder `{reminder_id}`.")

    async def preview_reminder(self, interaction: discord.Interaction, reminder: Dict[str, Any]) -> None:
        """Show the exact message that will be posted, without posting it."""
        if not await self._ensure_authorized(interaction):
            return
        outgoing = self.build_message(reminder, interaction.guild)
        preview_embed = outgoing.embed.copy()
        preview_embed.add_field(
            name="🔍 Preview only",
            value=(
                "This is exactly what will be posted when the reminder fires — "
                "nothing has been sent to the channel yet."
            ),
            inline=False,
        )
        if outgoing.warning:
            preview_embed.add_field(name="⚠️ Ping warning", value=outgoing.warning, inline=False)
        ping_line = outgoing.content or "_no role ping_"
        await render(
            interaction,
            content=f"**Role ping:** {ping_line}",
            embed=preview_embed,
            view=ConfirmActionView(
                self,
                interaction.user.id,
                confirm_label="🧪 Send test now",
                confirm_style=discord.ButtonStyle.primary,
                on_confirm=lambda target: self.send_test(target, reminder),
                on_cancel=lambda target: self.show_detail(target, str(reminder.get("reminder_id"))),
            ),
        )

    async def show_test_confirm(self, interaction: discord.Interaction, reminder: Dict[str, Any]) -> None:
        if not await self._ensure_authorized(interaction):
            return
        role_id = reminder.get("role_id")
        embed = discord.Embed(
            title="🧪 Send a test now?",
            description=(
                f"I will post this reminder to <#{reminder.get('channel_id')}> right now.\n"
                + (f"That will ping <@&{role_id}>." if role_id else "No role will be pinged.")
                + "\n\nThe schedule and delivery counters are not affected."
            ),
            color=config.WARNING_COLOR,
        )
        view = ConfirmActionView(
            self,
            interaction.user.id,
            confirm_label="🧪 Send test now",
            confirm_style=discord.ButtonStyle.primary,
            on_confirm=lambda target: self.send_test(target, reminder),
            on_cancel=lambda target: self.show_detail(target, str(reminder.get("reminder_id"))),
        )
        await render(interaction, embed=embed, view=view)

    async def send_test(self, interaction: discord.Interaction, reminder: Dict[str, Any]) -> None:
        if not await self._ensure_authorized(interaction):
            return
        await self._defer_for_edit(interaction)
        delivered, error = await self._deliver(reminder, test=True)
        if delivered:
            note = f"✅ Test posted to <#{reminder.get('channel_id')}>."
        else:
            note = f"❌ Test failed: {error}"
        refreshed = reminder
        store = self.store
        if store is not None:
            try:
                refreshed = await store.get(interaction.guild.id, str(reminder.get("reminder_id"))) or reminder
            except Exception:
                refreshed = reminder
        await self._render_detail(interaction, refreshed, note=note)

    async def fetch_guild_channel(self, guild: discord.Guild, channel_id: int):
        """Resolve a channel that is not in the cache (used by the ID modal)."""
        return await self._resolve_channel(channel_id)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    reminder_group = app_commands.Group(
        name="reminder",
        description="Create and manage scheduled reminders for this server",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    async def reminder_autocomplete(self, interaction: discord.Interaction,
                                    current: str) -> List[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        store = self.store
        if store is None:
            return []
        try:
            reminders = await store.search(interaction.guild.id, current, limit=25)
        except Exception:
            logger.debug("Reminder autocomplete failed", exc_info=True)
            return []
        return [
            app_commands.Choice(
                name=truncate(f"{reminder.get('name') or 'Untitled'} • {reminder.get('status') or 'active'}", 95),
                value=str(reminder.get("reminder_id")),
            )
            for reminder in reminders
        ]

    @reminder_group.command(name="dashboard", description="Open the interactive reminder dashboard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dashboard_command(self, interaction: discord.Interaction) -> None:
        await self.open_dashboard(interaction)

    @reminder_group.command(name="create", description="Open the reminder creation wizard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def create_command(self, interaction: discord.Interaction) -> None:
        await self.start_create(interaction)

    @reminder_group.command(name="list", description="List every reminder in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_command(self, interaction: discord.Interaction) -> None:
        await self.show_list(interaction, 0)

    @reminder_group.command(name="manage", description="Open the reminder manager")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def manage_command(self, interaction: discord.Interaction) -> None:
        await self.show_manage(interaction, 0)

    @reminder_group.command(name="preview", description="Preview a reminder's message without sending it")
    @app_commands.describe(reminder="The reminder to preview")
    @app_commands.autocomplete(reminder=reminder_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview_command(self, interaction: discord.Interaction, reminder: str) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return
        await self._defer_for_edit(interaction)
        found = await store.get(interaction.guild.id, reminder)
        if found is None:
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Reminder not found",
                    description=f"I have no reminder with the ID `{reminder}`.",
                    color=config.ERROR_COLOR,
                ),
                view=DashboardView(self, interaction.user.id),
            )
            return
        await self.preview_reminder(interaction, found)

    @reminder_group.command(name="pause", description="Pause a reminder")
    @app_commands.describe(reminder="The reminder to pause")
    @app_commands.autocomplete(reminder=reminder_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause_command(self, interaction: discord.Interaction, reminder: str) -> None:
        await self._status_command(interaction, reminder, pause=True)

    @reminder_group.command(name="resume", description="Resume a paused reminder")
    @app_commands.describe(reminder="The reminder to resume")
    @app_commands.autocomplete(reminder=reminder_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resume_command(self, interaction: discord.Interaction, reminder: str) -> None:
        await self._status_command(interaction, reminder, pause=False)

    async def _status_command(self, interaction: discord.Interaction, reminder_id: str, *, pause: bool) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return
        found = await store.get(interaction.guild.id, reminder_id)
        if found is None:
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Reminder not found",
                    description=f"I have no reminder with the ID `{reminder_id}`.",
                    color=config.ERROR_COLOR,
                ),
                view=DashboardView(self, interaction.user.id),
            )
            return
        await self.toggle_pause(interaction, found, pause=pause)

    @reminder_group.command(name="delete", description="Delete a reminder")
    @app_commands.describe(reminder="The reminder to delete")
    @app_commands.autocomplete(reminder=reminder_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delete_command(self, interaction: discord.Interaction, reminder: str) -> None:
        if not await self._ensure_authorized(interaction):
            return
        store = self.store
        if store is None:
            await safe_reply(interaction, content=NO_DATABASE_MESSAGE, ephemeral=True)
            return
        found = await store.get(interaction.guild.id, reminder)
        if found is None:
            await render(
                interaction,
                embed=discord.Embed(
                    title="❌ Reminder not found",
                    description=f"I have no reminder with the ID `{reminder}`.",
                    color=config.ERROR_COLOR,
                ),
                view=DashboardView(self, interaction.user.id),
            )
            return
        await self.show_delete_confirm(interaction, found)

    @reminder_group.command(name="help", description="How the reminder system works")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def help_command(self, interaction: discord.Interaction) -> None:
        await self.show_help(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReminderCog(bot))
