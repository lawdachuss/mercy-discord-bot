# cogs/thread.py

import os
import logging
import time
import asyncio
import re
from datetime import datetime, timezone
from collections import defaultdict
from typing import Any, Optional

import discord
from discord.ext import commands
from discord import app_commands, Interaction, TextChannel
from discord.app_commands import checks
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trigger modes: what a message must contain for a thread to be created.
# Stored per channel as guild_configs.trigger. Docs written before this field
# existed are treated as "attachments" to keep behaviour unchanged.
# ---------------------------------------------------------------------------
TRIGGER_MODES: dict[str, str] = {
    "attachments": "📎 Attachments only",
    "text": "💬 Text only",
    "both": "📎💬 Attachments + Text",
    "all": "🌐 Everything (media, text, embeds, stickers)",
}
DEFAULT_TRIGGER = "attachments"

# Modes that look at message text, and so respect min_text_length.
TEXT_TRIGGERS = ("text", "both", "all")

# Presets surfaced in the interactive /thread_setup panel.
COOLDOWN_CHOICES: list[tuple[str, int]] = [
    ("No cooldown (0s)", 0),
    ("5 seconds", 5),
    ("10 seconds", 10),
    ("30 seconds", 30),
    ("1 minute", 60),
    ("5 minutes", 300),
    ("10 minutes", 600),
    ("30 minutes", 1800),
]

ARCHIVE_CHOICES: list[tuple[str, int]] = [
    ("1 hour", 60),
    ("24 hours (1 day)", 1440),
    ("3 days (needs boost level 2)", 4320),
    ("7 days (needs boost level 3)", 10080),
]

MIN_TEXT_CHOICES: list[tuple[str, int]] = [
    ("Off - any non-empty text", 0),
    ("1+ characters", 1),
    ("3+ characters (default)", 3),
    ("5+ characters", 5),
    ("10+ characters", 10),
    ("25+ characters", 25),
]

DEFAULT_COOLDOWN = 30
DEFAULT_ARCHIVE = 1440
DEFAULT_MIN_TEXT_LENGTH = 3

# How many configured channels /thread_setup shows and offers at once. Kept in
# step with the select-menu cap (25 options, one reserved for "add a channel").
MAX_PICKER_CHANNELS = 20

# Discord rejects an embed with more than 25 fields, so /thread_status pages.
EMBED_FIELDS_PER_PAGE = 20

# on_message runs for every non-bot guild message, so channel configs are held
# in a short-lived cache. Invalidated on every write.
CONFIG_CACHE_TTL = 30
# Hard cap on cached channel configs. The listener caches every channel it
# sees, including unconfigured ones, so without a bound this grows forever.
CONFIG_CACHE_MAX = 10_000

# Token buckets are pruned at most this often, and a bucket untouched for
# RATE_LIMIT_PRUNE_AFTER seconds is discarded.
RATE_LIMIT_PRUNE_INTERVAL = 300
RATE_LIMIT_PRUNE_AFTER = 3600

_WHITESPACE = re.compile(r"\s+")


def is_admin_check(interaction: Interaction) -> bool:
    """True when the interaction user may configure autothreads.

    Uses getattr because `Member.guild_permissions` reaches into the guild
    cache and raises for a user object or an uncached guild - which would
    otherwise blow up every component callback with "This interaction failed".
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.administrator or perms.manage_guild)


def archive_text(minutes: int, *, short: bool = False) -> str:
    """Human readable auto-archive duration."""
    return {
        60: "1h" if short else "1 hour",
        1440: "1d" if short else "24 hours (1 day)",
        4320: "3d" if short else "3 days",
        10080: "7d" if short else "7 days",
    }.get(minutes, f"{minutes}m" if short else f"{minutes} minutes")


def cooldown_text(seconds: int) -> str:
    """Human readable per-user cooldown."""
    if seconds <= 0:
        return "off"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" + ("s" if hours > 1 else "")
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes > 1 else "")
    return f"{seconds}s"


def config_int(config: dict, key: str, default: int) -> int:
    """Read an int out of a config doc, tolerating missing/garbage values."""
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


class ThreadCreatorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # MongoDB setup - use shared client if available, otherwise create new
        if hasattr(bot, 'mongo_client') and bot.mongo_client:
            self.mongo_client = bot.mongo_client
            self._owns_client = False
        else:
            mongo_uri = os.getenv("MONGO_URL")
            if not mongo_uri:
                raise ValueError("MONGO_URL is not set in the environment variables.")
            self.mongo_client = AsyncIOMotorClient(
                mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=45000,
                maxPoolSize=50,
                minPoolSize=5,
                maxIdleTimeMS=45000,
                retryWrites=True,
                retryReads=True
            )
            self._owns_client = True

        self.db = self.mongo_client["threads"]
        self.guild_configs = self.db["guild_configs"]
        self.cooldowns = self.db["cooldowns"]
        self.stats = self.db["stats"]

        # ESSENTIAL: Channel-wide rate limiting (prevents Discord API bans)
        # Token bucket: max 5 threads per channel per 10 seconds
        self.channel_rate_limits = defaultdict(lambda: {
            "tokens": 5.0,
            "last_refill": time.time()
        })

        # (guild_id, channel_id) -> (fetched_at, config doc or None)
        self._config_cache: dict[tuple[str, str], tuple[float, Optional[dict]]] = {}
        self._last_rate_limit_prune = 0.0

        # Create indexes for better performance - schedule as async task
        bot.loop.create_task(self._ensure_indexes())

    # ------------------------------------------------------------------
    # Indexes / lifecycle
    # ------------------------------------------------------------------
    async def _ensure_indexes(self):
        """Create database indexes if they don't exist."""
        try:
            # Compound index for guild_configs
            await self.guild_configs.create_index(
                [("guild_id", ASCENDING), ("channel_id", ASCENDING)],
                unique=True,
                background=True
            )

            # Compound index for cooldowns
            await self.cooldowns.create_index(
                [("guild_id", ASCENDING), ("user_id", ASCENDING)],
                unique=True,
                background=True
            )

            # TTL index to auto-delete old cooldown entries after 24 hours
            try:
                await self.cooldowns.create_index(
                    "last_used",
                    expireAfterSeconds=86400,
                    background=True
                )
            except Exception as idx_error:
                # If index exists with different options, drop and recreate
                if "IndexOptionsConflict" in str(idx_error) or "already exists" in str(idx_error):
                    await self.cooldowns.drop_index("last_used_1")
                    await self.cooldowns.create_index(
                        "last_used",
                        expireAfterSeconds=86400,
                        background=True
                    )
                else:
                    raise

            # Index for stats queries
            await self.stats.create_index(
                [("guild_id", ASCENDING), ("date", ASCENDING)],
                background=True
            )
            logger.info("Thread indexes created successfully")
        except Exception as e:
            logger.error(f"Error creating indexes: {e}")

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        try:
            # Only close connection if this cog created it
            if self._owns_client:
                self.mongo_client.close()
        except Exception:
            logger.debug("Error while closing autothread's Mongo client", exc_info=True)

    # ------------------------------------------------------------------
    # Config access
    # ------------------------------------------------------------------
    async def get_channel_config(
        self,
        guild_id: str,
        channel_id: str,
        *,
        force: bool = False
    ) -> Optional[dict]:
        """Fetch a channel config, cached briefly.

        Unconfigured channels are cached as None too, so channels that never
        opt in don't cost a Mongo round trip on every message.
        """
        key = (guild_id, channel_id)
        now = time.monotonic()

        if not force:
            cached = self._config_cache.get(key)
            if cached is not None and now - cached[0] < CONFIG_CACHE_TTL:
                return cached[1]

        try:
            config = await self.guild_configs.find_one(
                {"guild_id": guild_id, "channel_id": channel_id}
            )
        except Exception as e:
            logger.error(
                f"Error fetching thread config guild={guild_id} channel={channel_id}: {e}"
            )
            # Don't cache failures - retry on the next message.
            return None

        self._store_config(key, now, config)
        return config

    def _store_config(
        self,
        key: tuple[str, str],
        now: float,
        config: Optional[dict]
    ):
        """Cache a config, keeping the map bounded.

        This listener runs for every non-bot message, so the cache is keyed by
        (guild, channel) and would otherwise grow forever. Expired entries are
        swept, and the oldest one is evicted if sweeping is not enough.
        """
        if len(self._config_cache) >= CONFIG_CACHE_MAX:
            cutoff = now - CONFIG_CACHE_TTL
            for cached_key, (fetched_at, _) in list(self._config_cache.items()):
                if fetched_at < cutoff:
                    del self._config_cache[cached_key]

            # Still full (everything is hot) - drop the stalest entry.
            while len(self._config_cache) >= CONFIG_CACHE_MAX:
                oldest = min(
                    self._config_cache.items(), key=lambda kv: kv[1][0]
                )[0]
                del self._config_cache[oldest]

        self._config_cache[key] = (now, config)

    def invalidate_config(self, guild_id: str, channel_id: str):
        """Drop cached state for a channel so the next read hits the database."""
        self._config_cache.pop((guild_id, channel_id), None)
        # The rate-limit bucket is dead weight once the channel is off.
        self.channel_rate_limits.pop(channel_id, None)

    async def get_guild_configs(self, guild_id: str) -> list[dict]:
        """All configured channels for a guild."""
        return await self.guild_configs.find({"guild_id": guild_id}).to_list(length=None)

    # ------------------------------------------------------------------
    # Rate limiting / cooldowns / stats
    # ------------------------------------------------------------------
    def check_channel_rate_limit(self, channel_id: str) -> bool:
        """
        Token bucket rate limiter for channel-wide thread creation.
        Prevents hitting Discord's 50 req/sec global limit.
        Returns True if request is allowed, False if rate limited.
        """
        now = time.time()
        self._prune_rate_limits(now)
        bucket = self.channel_rate_limits[channel_id]

        # Calculate time elapsed since last refill
        elapsed = now - bucket["last_refill"]

        # Refill tokens: 1 token per 2 seconds, max 5 tokens
        tokens_to_add = elapsed / 2.0
        bucket["tokens"] = min(5.0, bucket["tokens"] + tokens_to_add)
        bucket["last_refill"] = now

        # Check if we have at least 1 token available
        if bucket["tokens"] >= 1.0:
            bucket["tokens"] -= 1.0
            return True

        return False

    def _prune_rate_limits(self, now: float):
        """Drop buckets for channels nobody has touched recently.

        `channel_rate_limits` is a defaultdict, so indexing inserts an entry
        for every channel the bot ever threads in. Sweeping occasionally keeps
        it proportional to the number of *active* channels.
        """
        if now - self._last_rate_limit_prune < RATE_LIMIT_PRUNE_INTERVAL:
            return

        self._last_rate_limit_prune = now
        cutoff = now - RATE_LIMIT_PRUNE_AFTER
        stale = [
            key
            for key, bucket in self.channel_rate_limits.items()
            if bucket["last_refill"] < cutoff
        ]
        for key in stale:
            del self.channel_rate_limits[key]

    async def is_on_cooldown(self, guild_id: str, user_id: str, cooldown: int) -> tuple[bool, float]:
        """Check if user is on cooldown. Returns (is_on_cooldown, time_remaining)."""
        if cooldown <= 0:
            return False, 0

        now = datetime.now(timezone.utc)
        try:
            entry = await self.cooldowns.find_one({"guild_id": guild_id, "user_id": user_id})
        except Exception as e:
            # Fail open: a Mongo hiccup must not block a user, and this runs
            # on the message listener's critical path.
            logger.error(f"Error reading cooldown for user {user_id}: {e}")
            return False, 0

        if entry:
            last_used = entry.get("last_used")
            # Handle both timezone-aware and naive datetimes from MongoDB
            if isinstance(last_used, datetime):
                if last_used.tzinfo is None:
                    last_used = last_used.replace(tzinfo=timezone.utc)

                elapsed = (now - last_used).total_seconds()
                if elapsed < cooldown:
                    return True, cooldown - elapsed
            elif last_used is not None:
                logger.warning(
                    f"Ignoring non-datetime last_used for user {user_id}: "
                    f"{type(last_used).__name__}"
                )

        return False, 0

    async def update_cooldown(self, guild_id: str, user_id: str):
        """Update the last used timestamp for a user."""
        now = datetime.now(timezone.utc)
        try:
            await self.cooldowns.update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"$set": {"last_used": now}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error updating cooldown for user {user_id}: {e}")

    async def record_stats(self, guild_id: str, channel_id: str, user_id: str):
        """Record thread creation statistics."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await self.stats.update_one(
                {
                    "guild_id": guild_id,
                    "date": today
                },
                {
                    "$inc": {
                        "total_threads": 1,
                        f"channels.{channel_id}": 1,
                        f"users.{user_id}": 1
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error recording stats: {e}")

    # ------------------------------------------------------------------
    # Trigger matching
    # ------------------------------------------------------------------
    def should_create_thread(self, message: discord.Message, config: dict) -> bool:
        """Decide whether a message should spawn a thread.

        The channel's `trigger` mode decides which kinds of content qualify.
        When a message carries nothing but text, `min_text_length` is enforced
        so short replies ("ok", "lol") don't each create a thread.
        """
        trigger = config.get("trigger", DEFAULT_TRIGGER)
        if trigger not in TRIGGER_MODES:
            trigger = DEFAULT_TRIGGER

        has_attachments = bool(message.attachments)
        has_stickers = bool(message.stickers)
        has_embeds = bool(message.embeds)
        text = (message.content or "").strip()
        has_text = bool(text)

        if trigger == "attachments":
            return has_attachments
        if trigger == "text":
            return has_text
        if trigger == "both":
            return has_attachments or has_text
        # "all" - anything worth a thread of its own
        return has_attachments or has_text or has_embeds or has_stickers

    def _passes_min_length(self, message: discord.Message, config: dict) -> bool:
        """Enforce min_text_length, but only for text-only messages."""
        trigger = config.get("trigger", DEFAULT_TRIGGER)
        if trigger not in TEXT_TRIGGERS:
            return True

        has_other_media = bool(
            message.attachments or message.embeds or message.stickers
        )
        if has_other_media:
            # The message is more than just chatter.
            return True

        min_len = config_int(config, "min_text_length", DEFAULT_MIN_TEXT_LENGTH)
        if min_len <= 0:
            return True

        return len((message.content or "").strip()) >= min_len

    def matches_trigger(self, message: discord.Message, config: dict) -> bool:
        """Full gate: trigger mode match plus the minimum-length guard."""
        return self.should_create_thread(message, config) and self._passes_min_length(
            message, config
        )

    # ------------------------------------------------------------------
    # Thread naming
    # ------------------------------------------------------------------
    def sanitize_thread_name(self, name: str, author_name: str) -> str:
        """Sanitize thread name to prevent Discord API issues."""
        # Thread names are single-line: collapse all whitespace runs.
        sanitized = _WHITESPACE.sub(" ", name or "").strip()

        # Strip non-printable leftovers
        sanitized = "".join(c for c in sanitized if c.isprintable()).strip()

        # Limit to 100 characters (Discord's thread name cap)
        sanitized = sanitized[:100]

        # If empty after sanitization, use author's name
        if not sanitized:
            sanitized = f"Thread by {author_name}"[:100]

        return sanitized

    def derive_thread_name(self, message: discord.Message) -> str:
        """Pick the best available label for the thread.

        Message content is preferred, then an attachment filename, then an
        embed title, then the author's name.
        """
        author_name = message.author.display_name

        if (message.content or "").strip():
            return self.sanitize_thread_name(message.content, author_name)

        if message.attachments:
            filename = message.attachments[0].filename
            if filename:
                return self.sanitize_thread_name(filename, author_name)

        for embed in message.embeds:
            if embed.title:
                return self.sanitize_thread_name(embed.title, author_name)

        return self.sanitize_thread_name("", author_name)

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------
    async def _notify(
        self,
        message: discord.Message,
        content: str,
        *,
        delete_after: float = 5.0
    ):
        """Best-effort user-facing notice. Never raises.

        A failure to post a notice must not abort thread creation, and must
        not bubble up into the listener.
        """
        try:
            await message.channel.send(content, delete_after=delete_after)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.debug(f"Could not send autothread notice: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Watch configured channels and open a thread for matching messages.

        This cog is the only place in the bot that calls `bot.process_commands`,
        so that call sits in a `finally`: letting any exception escape would
        silently disable every prefix command in the server.
        """
        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return

        try:
            await self._maybe_start_thread(message)
        finally:
            # CRITICAL: process commands no matter what happened above.
            await self.bot.process_commands(message)

    async def _maybe_start_thread(self, message: discord.Message):
        """Create a thread for `message` if its channel is configured for it."""
        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)

        # Check if this channel is configured, and whether the message content
        # matches the channel's trigger mode.
        config = await self.get_channel_config(guild_id, channel_id)

        if not config or not self.matches_trigger(message, config):
            return

        # A message can only ever carry one thread. If it already has one,
        # skip rather than spend retries on an error Discord always returns.
        if getattr(message, "thread", None) is not None:
            return

        # Check per-user cooldown
        cooldown = config_int(config, "cooldown", DEFAULT_COOLDOWN)
        on_cd, remaining = await self.is_on_cooldown(guild_id, user_id, cooldown)

        if on_cd:
            await self._notify(
                message,
                f"⏳ {message.author.mention}, cooldown active. "
                f"Try again in **{remaining:.0f}s**."
            )
            return

        # Sanitize thread name
        thread_name = self.derive_thread_name(message)

        # Get archive duration from config
        archive_duration = self.resolve_archive_duration(message.guild, config)

        # ESSENTIAL: Check channel-wide rate limit (prevents API spam) right
        # before the actual thread creation so skipped messages (cooldown,
        # missing config, etc.) do not consume the rate-limit budget.
        if not self.check_channel_rate_limit(channel_id):
            # Silently skip - don't spam users with messages
            return

        thread = await self._create_thread_with_retry(
            message, thread_name, archive_duration
        )
        if thread is None:
            return

        # Side effects run only once the thread definitely exists, and are
        # never retried - a hiccup here must not re-enter the create loop.
        await self.update_cooldown(guild_id, user_id)
        await self.record_stats(guild_id, channel_id, user_id)

        # Send confirmation message in thread
        try:
            await thread.send(
                f"{self._trigger_emoji(config.get('trigger', DEFAULT_TRIGGER))}"
                f" Thread created by {message.author.mention}"
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            # The thread exists; a failed welcome message is not worth
            # telling the user about in-channel.
            logger.warning(
                f"Thread created but welcome message failed: "
                f"guild={guild_id} channel={channel_id} error={e}"
            )

    async def _create_thread_with_retry(
        self,
        message: discord.Message,
        thread_name: str,
        archive_duration: int
    ) -> Optional[discord.Thread]:
        """Create the thread, retrying only transient failures.

        The try/except wraps *only* the create call. If post-creation work
        were inside it, a failure there would retry the create on a message
        that already has a thread - which Discord rejects, producing a bogus
        "failed to create thread" notice for a thread that was in fact made.
        """
        guild_id = message.guild.id
        channel_id = message.channel.id
        user_id = message.author.id

        # ESSENTIAL: Retry logic with exponential backoff
        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                return await message.create_thread(
                    name=thread_name,
                    auto_archive_duration=archive_duration
                )

            except discord.errors.RateLimited as e:
                # Discord rate limited us - wait and retry
                if attempt < max_retries - 1:
                    await asyncio.sleep(e.retry_after)
                else:
                    # Max retries reached
                    await self._notify(
                        message, "⚠️ Server is busy. Please try again in a moment."
                    )
                    logger.error(
                        f"Failed to create thread after {max_retries} attempts due to "
                        f"rate limiting: guild={guild_id} channel={channel_id} user={user_id}"
                    )

            except discord.Forbidden:
                # Missing permissions - retrying cannot help
                await self._notify(
                    message, "❌ I don't have permission to create threads here."
                )
                logger.warning(
                    f"Missing thread permissions: "
                    f"guild={guild_id} channel={channel_id}"
                )
                return None

            except discord.HTTPException as e:
                # "Unknown Message" (10008) covers "the message already has a
                # thread" as well as a deleted message, and 160004 is the newer
                # "a thread has already been created for this message". Neither
                # is worth retrying, and a second thread is impossible.
                if e.code in (10008, 160004):
                    logger.info(
                        f"Message unusable for a new thread "
                        f"(already threaded or deleted): "
                        f"guild={guild_id} channel={channel_id} code={e.code}"
                    )
                    return None

                # Guard against Discord wording this differently.
                detail = f"{getattr(e, 'text', '')} {e}".lower()
                if e.status == 400 and "already" in detail:
                    logger.info(
                        f"Thread already existed on message: "
                        f"guild={guild_id} channel={channel_id}"
                    )
                    return None
                    logger.info(
                        f"Thread already existed on message: "
                        f"guild={guild_id} channel={channel_id}"
                    )
                    return None

                if attempt < max_retries - 1:
                    # Retry with exponential backoff
                    await asyncio.sleep(retry_delay * (2 ** attempt))
                else:
                    # Max retries reached
                    await self._notify(
                        message, "⚠️ Failed to create thread. Please try again later."
                    )
                    logger.error(
                        f"Failed to create thread after {max_retries} attempts: "
                        f"guild={guild_id} channel={channel_id} error={e}",
                        exc_info=True
                    )

            except Exception as e:
                # Unexpected error
                logger.error(
                    f"Unexpected error creating thread: "
                    f"guild={guild_id} channel={channel_id} error={e}",
                    exc_info=True
                )
                await self._notify(message, "❌ An unexpected error occurred.")
                return None  # Don't retry on unexpected errors

        return None

    @staticmethod
    def resolve_archive_duration(guild: discord.Guild, config: dict) -> int:
        """Clamp the stored auto-archive value to one Discord will accept.

        Only 60/1440/4320/10080 are legal, and the long durations are a
        Server Boost perk (3 days needs Level 2, 7 days needs Level 3).
        Discord has relaxed this restriction before, so the thresholds here
        are deliberately conservative: allowing a duration the server is not
        entitled to turns every single thread creation into a failed request,
        whereas downgrading early just quietly uses 1 day.
        """
        valid = (60, 1440, 4320, 10080)
        value = config_int(config, "archive_duration", DEFAULT_ARCHIVE)
        if value not in valid:
            return DEFAULT_ARCHIVE

        tier = getattr(guild, "premium_tier", 0) or 0
        if value == 4320 and tier < 2:
            return DEFAULT_ARCHIVE
        if value == 10080 and tier < 3:
            return DEFAULT_ARCHIVE
        return value

    @staticmethod
    def _trigger_emoji(trigger: str) -> str:
        return {
            "attachments": "📎",
            "text": "💬",
            "both": "🧵",
            "all": "🌐",
        }.get(trigger, "🧵")

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------
    def build_settings_embed(self, guild: discord.Guild, config: dict) -> discord.Embed:
        """Embed summarising one channel's autothread configuration."""
        channel_id = str(config.get("channel_id", "0"))
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        target = channel.mention if channel else f"`{channel_id}`"

        trigger = config.get("trigger", DEFAULT_TRIGGER)
        if trigger not in TRIGGER_MODES:
            trigger = DEFAULT_TRIGGER

        cooldown = config_int(config, "cooldown", DEFAULT_COOLDOWN)
        stored_archive = config_int(config, "archive_duration", DEFAULT_ARCHIVE)
        archive = self.resolve_archive_duration(guild, config)
        min_len = config_int(config, "min_text_length", DEFAULT_MIN_TEXT_LENGTH)

        # Show what actually happens, not just what is stored: a boost-gated
        # duration that the server is not entitled to is silently downgraded.
        if archive != stored_archive:
            archive_value = (
                f"**{archive_text(archive)}** of inactivity\n"
                f"_Server boost required for {archive_text(stored_archive)} "
                f"— using {archive_text(archive)} instead._"
            )
        else:
            archive_value = f"**{archive_text(archive)}** of inactivity"

        embed = discord.Embed(
            title="🧵 Autothread Settings",
            description=f"Active in {target}\nUse the menus below to change how threads are triggered.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Triggers on",
            value=TRIGGER_MODES[trigger],
            inline=False,
        )
        if trigger in TEXT_TRIGGERS:
            min_value = "Off — any non-empty text" if min_len <= 0 else f"**{min_len}+** characters"
            embed.add_field(name="Minimum text length", value=min_value, inline=True)
        else:
            embed.add_field(
                name="Minimum text length",
                value="_Not used in this mode_",
                inline=True,
            )
        embed.add_field(
            name="Cooldown", value=f"**{cooldown_text(cooldown)}** per user", inline=True
        )
        embed.add_field(
            name="Auto-archive",
            value=archive_value,
            inline=True,
        )
        embed.set_footer(text="Changes are saved immediately.")
        return embed

    def build_config_summary_line(self, config: dict) -> str:
        """One-line summary used by /thread_status."""
        trigger = config.get("trigger", DEFAULT_TRIGGER)
        if trigger not in TRIGGER_MODES:
            trigger = DEFAULT_TRIGGER
        return TRIGGER_MODES[trigger]

    # ------------------------------------------------------------------
    # /thread_channel - toggle + set options
    # ------------------------------------------------------------------
    @app_commands.command(
        name="thread_channel",
        description="Toggle thread-creation on a channel (add/remove)."
    )
    @app_commands.describe(
        channel="The channel to configure for automatic thread creation",
        cooldown="Cooldown in seconds between thread creations per user (default: 30)",
        archive_duration="Auto-archive duration: 60 (1h), 1440 (1d), 4320 (3d), 10080 (1w)",
        trigger="What kind of messages should create a thread",
        min_text_length="For text-based modes, ignore messages shorter than this (0 = off)"
    )
    @app_commands.choices(archive_duration=[
        app_commands.Choice(name=label, value=value)
        for label, value in ARCHIVE_CHOICES
    ])
    @app_commands.choices(trigger=[
        app_commands.Choice(name=label, value=value)
        for value, label in TRIGGER_MODES.items()
    ])
    @app_commands.guild_only()
    @checks.has_permissions(administrator=True)
    async def configure_channel(
        self,
        interaction: Interaction,
        channel: TextChannel,
        cooldown: int = 30,
        archive_duration: int = 1440,
        trigger: Optional[app_commands.Choice[str]] = None,
        min_text_length: Optional[int] = None
    ):
        """Configure a channel for automatic thread creation."""
        # Validate cooldown
        if cooldown < 0 or cooldown > 3600:
            return await interaction.response.send_message(
                "❌ Cooldown must be between 0 and 3600 seconds (1 hour).",
                ephemeral=True
            )

        if min_text_length is not None and not 0 <= min_text_length <= 500:
            return await interaction.response.send_message(
                "❌ Minimum text length must be between 0 and 500 characters.",
                ephemeral=True
            )

        guild_id = str(interaction.guild_id)
        channel_id = str(channel.id)

        # Cheap validation above is done - acknowledge before the Mongo
        # reads/writes below, which can exceed the 3s interaction deadline.
        await interaction.response.defer(ephemeral=True)

        # Check if already configured
        existing = await self.guild_configs.find_one(
            {"guild_id": guild_id, "channel_id": channel_id}
        )

        if existing:
            # Remove configuration
            try:
                await self.guild_configs.delete_one(
                    {"guild_id": guild_id, "channel_id": channel_id}
                )
                self.invalidate_config(guild_id, channel_id)
                return await interaction.followup.send(
                    f"🗑️ Thread creation **disabled** in {channel.mention}.\n"
                    f"💡 Run it again to re-enable with new settings.",
                    ephemeral=True
                )
            except Exception as e:
                logger.error(f"Error removing channel config: {e}", exc_info=True)
                return await interaction.followup.send(
                    "❌ An error occurred while removing the configuration.",
                    ephemeral=True
                )

        # Validate bot permissions
        missing = self.missing_thread_permissions(channel, interaction.guild.me)
        if missing:
            return await interaction.followup.send(missing, ephemeral=True)

        # Resolve the trigger mode
        trigger_value = trigger.value if trigger is not None else DEFAULT_TRIGGER
        if trigger_value not in TRIGGER_MODES:
            return await interaction.followup.send(
                "❌ Unknown trigger mode.",
                ephemeral=True
            )

        if min_text_length is None:
            min_text_length = DEFAULT_MIN_TEXT_LENGTH

        # Add new configuration
        try:
            await self.guild_configs.update_one(
                {"guild_id": guild_id, "channel_id": channel_id},
                {"$set": {
                    "cooldown": cooldown,
                    "archive_duration": archive_duration,
                    "trigger": trigger_value,
                    "min_text_length": min_text_length
                }},
                upsert=True
            )
            self.invalidate_config(guild_id, channel_id)

            min_text_line = (
                f"📏 Minimum text length: **{min_text_length}** characters\n"
                if trigger_value in TEXT_TRIGGERS
                else ""
            )

            await interaction.followup.send(
                f"✅ Thread creation **enabled** in {channel.mention}\n"
                f"🧵 Triggers on: **{TRIGGER_MODES[trigger_value]}**\n"
                f"{min_text_line}"
                f"⏱️ Cooldown: **{cooldown_text(cooldown)}** per user\n"
                f"📦 Auto-archive: **{archive_text(archive_duration)}** of inactivity\n"
                f"💡 Use `/thread_setup` to fine-tune this without toggling it off.",
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"Error adding channel config: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while saving the configuration.",
                ephemeral=True
            )

    @staticmethod
    def missing_thread_permissions(channel: TextChannel, me: discord.Member) -> Optional[str]:
        """Return an error string if the bot can't create threads here."""
        perms = channel.permissions_for(me)

        if not perms.view_channel:
            return f"❌ I need **View Channel** permission in {channel.mention} first."

        if not perms.create_public_threads:
            return f"❌ I need **Create Public Threads** permission in {channel.mention} first."

        if not perms.send_messages_in_threads:
            return f"❌ I need **Send Messages in Threads** permission in {channel.mention} first."

        return None

    # ------------------------------------------------------------------
    # /thread_setup - interactive admin panel
    # ------------------------------------------------------------------
    @app_commands.command(
        name="thread_setup",
        description="Interactive panel to add channels and choose what creates threads."
    )
    @app_commands.guild_only()
    @checks.has_permissions(administrator=True)
    async def thread_setup(self, interaction: Interaction):
        """Open the interactive autothread configuration panel."""
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id)
        try:
            configs = await self.get_guild_configs(guild_id)
        except Exception as e:
            logger.error(f"Error fetching configs: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ An error occurred while fetching configurations.",
                ephemeral=True
            )

        embed = self.build_picker_embed(interaction.guild, configs)
        view = ThreadSetupView(self, guild_id, interaction.guild, list(configs))
        sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(sent)

    def build_picker_embed(self, guild: discord.Guild, configs: list[dict]) -> discord.Embed:
        """Embed shown by the channel picker step of /thread_setup."""
        embed = discord.Embed(
            title="🧵 Autothread Setup",
            description=(
                "Pick a channel to configure, or add a new one.\n\n"
                "Each channel can trigger threads on **attachments**, **text**, "
                "**both**, or **everything**."
            ),
            color=discord.Color.blurple(),
        )

        lines = []
        for cfg in configs[:MAX_PICKER_CHANNELS]:
            channel_id = str(cfg.get("channel_id", ""))
            channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            name = channel.name if channel else f"deleted ({channel_id})"
            lines.append(
                f"`#{name}` — {self.build_config_summary_line(cfg)}"
            )

        if not lines:
            lines.append("_No channels configured yet._")
        elif len(configs) > MAX_PICKER_CHANNELS:
            lines.append(
                f"_…and {len(configs) - MAX_PICKER_CHANNELS} more "
                f"(manage those with `/thread_channel`)_"
            )

        # Discord caps an embed field value at 1024 characters; going over
        # makes the whole message fail to send.
        listing = "\n".join(lines)[:1024]

        embed.add_field(name="Configured channels", value=listing, inline=False)
        embed.set_footer(text="Nothing is saved until you choose a channel.")
        return embed

    async def _open_settings_panel(
        self,
        interaction: Interaction,
        guild_id: str,
        channel_id: str
    ) -> bool:
        """Send a fresh settings panel for a channel.

        Returns True when the panel was shown, False when the channel is no
        longer configured.
        """
        config = await self.get_channel_config(guild_id, channel_id, force=True)
        if not config:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ That channel is no longer configured.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ That channel is no longer configured.",
                    ephemeral=True
                )
            return False

        embed = self.build_settings_embed(interaction.guild, config)
        view = ThreadSettingsView(self, guild_id, channel_id, config)

        if interaction.response.is_done():
            sent = await interaction.followup.send(
                embed=embed, view=view, ephemeral=True
            )
        else:
            sent = await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )
        view.bind(sent)
        return True

    # ------------------------------------------------------------------
    # /thread_status
    # ------------------------------------------------------------------
    @app_commands.command(
        name="thread_status",
        description="View all channels configured for automatic thread creation."
    )
    @app_commands.guild_only()
    @checks.has_permissions(administrator=True)
    async def thread_status(self, interaction: Interaction):
        """Display thread configuration status for the server."""
        guild_id = str(interaction.guild_id)

        # Acknowledge before the Mongo reads below, which can exceed the 3s
        # interaction deadline.
        await interaction.response.defer(ephemeral=True)

        try:
            configs = await self.get_guild_configs(guild_id)
        except Exception as e:
            logger.error(f"Error fetching configs: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ An error occurred while fetching configurations.",
                ephemeral=True
            )

        if not configs:
            return await interaction.followup.send(
                "❌ No channels configured for automatic thread creation.\n"
                "💡 Use `/thread_channel` or `/thread_setup` to configure a channel.",
                ephemeral=True
            )

        # Build the channel list first, then page it: Discord rejects an embed
        # with more than 25 fields, which used to break the command outright.
        entries = []
        for cfg in configs:
            channel_id = str(cfg.get("channel_id", ""))
            chan = interaction.guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            if chan:
                archive_duration = self.resolve_archive_duration(
                    interaction.guild, cfg
                )
                stored_archive = config_int(
                    cfg, "archive_duration", DEFAULT_ARCHIVE
                )
                cooldown = config_int(cfg, "cooldown", DEFAULT_COOLDOWN)
                trigger = cfg.get("trigger", DEFAULT_TRIGGER)
                if trigger not in TRIGGER_MODES:
                    trigger = DEFAULT_TRIGGER

                min_line = ""
                if trigger in TEXT_TRIGGERS:
                    min_len = config_int(cfg, "min_text_length", DEFAULT_MIN_TEXT_LENGTH)
                    min_line = (
                        f"📏 Min text: **{min_len}** chars\n"
                        if min_len > 0
                        else "📏 Min text: **off**\n"
                    )

                # A boost-gated duration that the server cannot use is
                # downgraded at creation time - report the effective one.
                archive_line = f"📦 Archive: **{archive_text(archive_duration, short=True)}**\n"
                if archive_duration != stored_archive:
                    archive_line += (
                        f"_(boost needed for "
                        f"{archive_text(stored_archive, short=True)})_\n"
                    )

                entries.append((
                    f"#{chan.name}",
                    f"🧵 Triggers on: **{TRIGGER_MODES[trigger]}**\n"
                    f"{min_line}"
                    f"⏱️ Cooldown: **{cooldown_text(cooldown)}**\n"
                    f"{archive_line}"
                    f"🆔 ID: `{chan.id}`",
                ))
            else:
                # Channel was deleted, clean up config
                try:
                    await self.guild_configs.delete_one(
                        {"guild_id": guild_id, "channel_id": channel_id}
                    )
                    self.invalidate_config(guild_id, channel_id)
                except Exception as e:
                    logger.error(f"Error cleaning up config: {e}", exc_info=True)

        valid_configs = len(entries)

        if valid_configs == 0:
            return await interaction.followup.send(
                "❌ All configured channels have been deleted.\n"
                "💡 Use `/thread_channel` or `/thread_setup` to configure a new channel.",
                ephemeral=True
            )

        total_pages = max(
            1,
            (valid_configs + EMBED_FIELDS_PER_PAGE - 1) // EMBED_FIELDS_PER_PAGE,
        )
        for page in range(total_pages):
            embed = discord.Embed(
                title="📊 Thread Configuration",
                description=f"Configured channels in **{interaction.guild.name}**",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            chunk = entries[page * EMBED_FIELDS_PER_PAGE:(page + 1) * EMBED_FIELDS_PER_PAGE]
            for name, value in chunk:
                embed.add_field(name=name, value=value, inline=False)
            embed.set_footer(
                text=f"Total: {valid_configs} channel(s)"
                + (f" • page {page + 1}/{total_pages}" if total_pages > 1 else "")
                + " • /thread_setup to change settings"
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /thread_stats
    # ------------------------------------------------------------------
    @app_commands.command(
        name="thread_stats",
        description="View thread creation statistics for this server."
    )
    @app_commands.guild_only()
    @checks.has_permissions(administrator=True)
    async def thread_stats(self, interaction: Interaction):
        """Display thread creation statistics."""
        guild_id = str(interaction.guild_id)

        # Acknowledge before the Mongo reads below, which can exceed the 3s
        # interaction deadline.
        await interaction.response.defer(ephemeral=True)

        try:
            # Get today's stats
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_stats = await self.stats.find_one({
                "guild_id": guild_id,
                "date": today
            })

            # Get last 7 days total
            from datetime import timedelta
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            week_stats = await self.stats.find({
                "guild_id": guild_id,
                "date": {"$gte": week_ago}
            }).to_list(length=None)

            total_week = sum(s.get("total_threads", 0) for s in week_stats)
            total_today = today_stats.get("total_threads", 0) if today_stats else 0

            embed = discord.Embed(
                title="📊 Thread Creation Statistics",
                description=f"Activity overview for **{interaction.guild.name}**",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="📅 Today",
                value=f"**{total_today}** threads",
                inline=True
            )

            embed.add_field(
                name="📆 Last 7 Days",
                value=f"**{total_week}** threads",
                inline=True
            )

            # Top channels today
            if today_stats and "channels" in today_stats:
                top_channels = sorted(
                    today_stats["channels"].items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]

                if top_channels:
                    channels_text = "\n".join([
                        f"<#{ch_id}>: **{count}** threads"
                        for ch_id, count in top_channels
                    ])

                    embed.add_field(
                        name="🔥 Top Channels Today",
                        value=channels_text,
                        inline=False
                    )

            embed.set_footer(text="Statistics are recorded daily")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error fetching stats: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An error occurred while fetching statistics.",
                ephemeral=True
            )

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------
    async def _generic_error(self, name: str, interaction: Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Administrator** permission to use this command.",
                ephemeral=True
            )
        else:
            logger.error(f"Error in {name}: {error}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ An unexpected error occurred.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True
                )

    @configure_channel.error
    async def configure_channel_error(self, interaction: Interaction, error):
        """Handle errors for the configure_channel command."""
        await self._generic_error("configure_channel", interaction, error)

    @thread_setup.error
    async def thread_setup_error(self, interaction: Interaction, error):
        """Handle errors for the thread_setup command."""
        await self._generic_error("thread_setup", interaction, error)

    @thread_status.error
    async def thread_status_error(self, interaction: Interaction, error):
        """Handle errors for the thread_status command."""
        await self._generic_error("thread_status", interaction, error)

    @thread_stats.error
    async def thread_stats_error(self, interaction: Interaction, error):
        """Handle errors for the thread_stats command."""
        await self._generic_error("thread_stats", interaction, error)


# =====================================================================
# Interactive UI
# =====================================================================

class AddChannelModal(discord.ui.Modal, title="Add a channel to autothreads"):
    """Ask for a channel by name or ID to enable autothreads in."""

    channel_input = discord.ui.TextInput(
        label="Channel name or ID",
        placeholder="#media  or  123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, cog: ThreadCreatorCog, guild_id: str):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: Interaction):
        if not is_admin_check(interaction):
            return await interaction.response.send_message(
                "You must be an administrator to use this.", ephemeral=True
            )

        raw = (self.channel_input.value or "").strip().lstrip("#").strip()
        if not raw:
            return await interaction.response.send_message(
                "❌ Please provide a channel name or ID.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            return await interaction.followup.send(
                "❌ This can only be used in a server.", ephemeral=True
            )

        channel: Optional[TextChannel] = None
        if raw.isdigit():
            candidate = guild.get_channel(int(raw))
            if isinstance(candidate, TextChannel):
                channel = candidate
        else:
            lowered = raw.lower()
            # Exact name wins outright.
            for text_channel in guild.text_channels:
                if text_channel.name.lower() == lowered:
                    channel = text_channel
                    break

            if channel is None:
                # Only fall back to a partial match when it is unambiguous.
                # Guessing here would silently enable autothreads in a channel
                # the admin never named.
                partial = [
                    c for c in guild.text_channels if lowered in c.name.lower()
                ]
                if len(partial) == 1:
                    channel = partial[0]
                elif len(partial) > 1:
                    names = ", ".join(f"`#{c.name}`" for c in partial[:10])
                    return await interaction.followup.send(
                        f"❌ **{raw}** matches {len(partial)} channels: {names}\n"
                        f"💡 Please use the exact channel name or its ID.",
                        ephemeral=True
                    )

        if channel is None:
            return await interaction.followup.send(
                f"❌ Could not find a text channel matching **{raw}**.\n"
                f"💡 Tip: you can also paste the channel ID with Developer Mode on.",
                ephemeral=True
            )

        channel_id = str(channel.id)
        existing = await self.cog.get_channel_config(self.guild_id, channel_id, force=True)
        if existing:
            return await self.cog._open_settings_panel(
                interaction, self.guild_id, channel_id
            )

        missing = ThreadCreatorCog.missing_thread_permissions(channel, guild.me)
        if missing:
            return await interaction.followup.send(missing, ephemeral=True)

        try:
            await self.cog.guild_configs.update_one(
                {"guild_id": self.guild_id, "channel_id": channel_id},
                {
                    "$set": {
                        "cooldown": DEFAULT_COOLDOWN,
                        "archive_duration": DEFAULT_ARCHIVE,
                        "trigger": DEFAULT_TRIGGER,
                        "min_text_length": DEFAULT_MIN_TEXT_LENGTH,
                    }
                },
                upsert=True,
            )
            self.cog.invalidate_config(self.guild_id, channel_id)
        except Exception as e:
            logger.error(f"Error saving channel config: {e}", exc_info=True)
            return await interaction.followup.send(
                "❌ An error occurred while saving the configuration.",
                ephemeral=True
            )

        await interaction.followup.send(
            f"✅ Autothreads **enabled** in {channel.mention} with default settings.",
            ephemeral=True,
        )
        await self.cog._open_settings_panel(interaction, self.guild_id, channel_id)


class ThreadChannelSelect(discord.ui.Select):
    """Step 1: pick which configured channel to edit, or add a new one."""

    def __init__(
        self,
        cog: ThreadCreatorCog,
        guild_id: str,
        guild: discord.Guild,
        configs: list[dict],
    ):
        self.cog = cog
        self.guild_id = guild_id

        # Leave one slot for the "add" option (Discord caps menus at 25).
        options = []
        for cfg in configs[:MAX_PICKER_CHANNELS]:
            channel_id = str(cfg.get("channel_id", ""))
            channel = (
                guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            )
            # Discord rejects empty labels, so always fall back to something.
            label = f"#{channel.name}" if channel else (channel_id or "unknown channel")
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=channel_id or "__unknown__",
                    description=cog.build_config_summary_line(cfg)[:100],
                )
            )

        options.append(
            discord.SelectOption(
                label="➕ Add a new channel",
                value="__add__",
                description="Enable autothreads in another channel",
            )
        )

        super().__init__(
            placeholder="Choose a channel to configure...",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: Interaction):
        if not is_admin_check(interaction):
            return await interaction.response.send_message(
                "You must be an administrator to use this.", ephemeral=True
            )

        value = self.values[0]
        if value == "__add__":
            return await interaction.response.send_modal(
                AddChannelModal(self.cog, self.guild_id)
            )

        await self.cog._open_settings_panel(interaction, self.guild_id, value)


class ThreadSetupView(discord.ui.View):
    """Channel picker shown by /thread_setup."""

    def __init__(
        self,
        cog: ThreadCreatorCog,
        guild_id: str,
        guild: discord.Guild,
        configs: list[dict],
        *,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        # Set by `bind()` once the panel has been sent; discord.ui.View has no
        # message reference of its own, so on_timeout would have nothing to
        # edit without this.
        self.panel_message: Optional[discord.Message] = None
        self.add_item(ThreadChannelSelect(cog, guild_id, guild, configs))

    def bind(self, message: Optional[discord.Message]):
        """Remember the message this view is attached to, for on_timeout."""
        self.panel_message = message
        return self

    async def on_timeout(self):
        # Without this the panel just goes dead and every later click fails
        # with "This interaction failed" and no explanation.
        for item in self.children:
            if item.is_dispatchable():
                item.disabled = True
        if self.panel_message is None:
            return
        try:
            await self.panel_message.edit(
                content="⏱️ This panel expired. Re-run `/thread_setup` to start a new one.",
                embed=None,
                view=None,
            )
        except Exception as e:
            logger.debug(f"Could not disable expired autothread panel: {e}")


class _FieldSelect(discord.ui.Select):
    """Base select that writes one config field and re-renders the panel."""

    field: str = ""
    parser: Any = staticmethod(int)

    def __init__(
        self,
        cog: ThreadCreatorCog,
        guild_id: str,
        channel_id: str,
        placeholder: str,
        options: list[discord.SelectOption],
        *,
        disabled: bool = False,
    ):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
        )
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: Interaction):
        if not is_admin_check(interaction):
            return await interaction.response.send_message(
                "You must be an administrator to use this.", ephemeral=True
            )

        value = self.parser(self.values[0])

        try:
            await self.cog.guild_configs.update_one(
                {"guild_id": self.guild_id, "channel_id": self.channel_id},
                {"$set": {self.field: value}},
                upsert=False,
            )
            self.cog.invalidate_config(self.guild_id, self.channel_id)
        except Exception as e:
            logger.error(f"Error updating {self.field}: {e}", exc_info=True)
            return await interaction.response.send_message(
                "❌ An error occurred while saving the configuration.",
                ephemeral=True,
            )

        config = await self.cog.get_channel_config(
            self.guild_id, self.channel_id, force=True
        )
        if not config:
            return await interaction.response.edit_message(
                content="This channel is no longer configured.", embed=None, view=None
            )

        try:
            new_view = ThreadSettingsView(
                self.cog, self.guild_id, self.channel_id, config
            )
            sent = await interaction.response.edit_message(
                embed=self.cog.build_settings_embed(interaction.guild, config),
                view=new_view,
            )
            new_view.bind(sent)
        except discord.HTTPException as e:
            # The value is already saved; a failed refresh is cosmetic.
            logger.warning(f"Could not refresh autothread panel: {e}")
        except Exception:
            # Embed/View construction sits inside this try, so a bug there must
            # not leave the interaction hanging with no response at all.
            logger.exception("Failed to rebuild autothread settings panel")
            try:
                await interaction.response.send_message(
                    "❌ Saved, but the panel could not be redrawn. "
                    "Re-run `/thread_setup`.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass


class ThreadTriggerSelect(_FieldSelect):
    """Choose which kinds of messages create a thread."""

    field = "trigger"
    parser = staticmethod(str)

    def __init__(self, cog, guild_id, channel_id, current: str):
        options = []
        for value, label in TRIGGER_MODES.items():
            description = {
                "attachments": "Images, videos, files",
                "text": "Text messages only",
                "both": "Attachments or text",
                "all": "Also embeds and stickers",
            }[value]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=description,
                    default=(value == current),
                )
            )
        super().__init__(
            cog, guild_id, channel_id,
            placeholder="🧵 What creates a thread?",
            options=options,
        )


class ThreadCooldownSelect(_FieldSelect):
    """Per-user cooldown between thread creations."""

    field = "cooldown"

    def __init__(self, cog, guild_id, channel_id, current: int):
        options = [
            discord.SelectOption(
                label=label, value=str(value), default=(value == current)
            )
            for label, value in COOLDOWN_CHOICES
        ]
        super().__init__(
            cog, guild_id, channel_id,
            placeholder="⏱️ Per-user cooldown",
            options=options,
        )


class ThreadArchiveSelect(_FieldSelect):
    """Thread auto-archive duration."""

    field = "archive_duration"

    def __init__(self, cog, guild_id, channel_id, current: int):
        options = [
            discord.SelectOption(
                label=label, value=str(value), default=(value == current)
            )
            for label, value in ARCHIVE_CHOICES
        ]
        super().__init__(
            cog, guild_id, channel_id,
            placeholder="📦 Auto-archive after",
            options=options,
        )


class ThreadMinTextSelect(_FieldSelect):
    """Minimum text length for text-based modes."""

    field = "min_text_length"

    def __init__(self, cog, guild_id, channel_id, current: int, *, disabled: bool):
        options = [
            discord.SelectOption(
                label=label, value=str(value), default=(value == current)
            )
            for label, value in MIN_TEXT_CHOICES
        ]
        super().__init__(
            cog, guild_id, channel_id,
            placeholder="📏 Minimum text length",
            options=options,
            disabled=disabled,
        )


class ThreadRemoveButton(discord.ui.Button):
    """Turn autothreads off for this channel."""

    def __init__(self, cog: ThreadCreatorCog, guild_id: str, channel_id: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Disable here",
            emoji="🗑️",
        )
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: Interaction):
        if not is_admin_check(interaction):
            return await interaction.response.send_message(
                "You must be an administrator to use this.", ephemeral=True
            )

        try:
            await self.cog.guild_configs.delete_one(
                {"guild_id": self.guild_id, "channel_id": self.channel_id}
            )
            self.cog.invalidate_config(self.guild_id, self.channel_id)
        except Exception as e:
            logger.error(f"Error removing channel config: {e}", exc_info=True)
            return await interaction.response.send_message(
                "❌ An error occurred while removing the configuration.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(int(self.channel_id)) if self.channel_id.isdigit() else None
        target = channel.mention if channel else f"`{self.channel_id}`"

        try:
            await interaction.response.edit_message(
                content=f"🗑️ Thread creation **disabled** in {target}.",
                embed=None,
                view=None,
            )
        except discord.HTTPException as e:
            logger.warning(f"Could not update autothread panel: {e}")


class ThreadSettingsView(discord.ui.View):
    """Per-channel settings panel shown after picking a channel."""

    def __init__(
        self,
        cog: ThreadCreatorCog,
        guild_id: str,
        channel_id: str,
        config: dict,
        *,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)

        # Set by `bind()` once the panel has been sent; see ThreadSetupView.
        self.panel_message: Optional[discord.Message] = None

        trigger = config.get("trigger", DEFAULT_TRIGGER)
        if trigger not in TRIGGER_MODES:
            trigger = DEFAULT_TRIGGER

        cooldown = config_int(config, "cooldown", DEFAULT_COOLDOWN)
        archive = config_int(config, "archive_duration", DEFAULT_ARCHIVE)
        min_len = config_int(config, "min_text_length", DEFAULT_MIN_TEXT_LENGTH)

        self.add_item(ThreadTriggerSelect(cog, guild_id, channel_id, trigger))
        self.add_item(ThreadCooldownSelect(cog, guild_id, channel_id, cooldown))
        self.add_item(ThreadArchiveSelect(cog, guild_id, channel_id, archive))
        self.add_item(
            ThreadMinTextSelect(
                cog, guild_id, channel_id, min_len,
                # Only meaningful when the mode looks at text.
                disabled=trigger not in TEXT_TRIGGERS,
            )
        )
        self.add_item(ThreadRemoveButton(cog, guild_id, channel_id))

    def bind(self, message: Optional[discord.Message]):
        """Remember the message this view is attached to, for on_timeout."""
        self.panel_message = message
        return self

    async def on_timeout(self):
        for item in self.children:
            if item.is_dispatchable():
                item.disabled = True
        if self.panel_message is None:
            return
        try:
            await self.panel_message.edit(
                content="⏱️ This panel expired. Re-run `/thread_setup` to start a new one.",
                embed=None,
                view=None,
            )
        except Exception as e:
            logger.debug(f"Could not disable expired autothread panel: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ThreadCreatorCog(bot))
