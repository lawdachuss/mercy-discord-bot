import time
import logging
import io
import asyncio
import math
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import ReturnDocument

EMBED_COLOR = 0x2F3136
SKIP_BLOCK_MINUTES = 1440  # 24 hours
MAX_DB_CANDIDATES = 2000
# How many pages of the queue to walk when collecting live candidates. Bounds
# the worst case where a guild's queue head is entirely stale rows.
MAX_QUEUE_PAGES = 5
SCAN_WINDOW = 200

# Banner shown on the matchmaking panel embeds.
#
# Must be the *raw* GitHub URL, not the /blob/ link: /blob/ serves an HTML
# page, which Discord cannot render. raw.githubusercontent.com serves the
# bytes directly, with a .gif extension and image/gif content type, which is
# what Discord's image proxy needs to display (and animate) an embed image.
#
# Point this somewhere else to rebrand; the file is ~8 MB, so hosting it
# yourself is worth it if you ever change it.
MATCHMAKER_BANNER_URL = (
    "https://raw.githubusercontent.com/vasud3v/mercy/master/banner.gif"
)

logger = logging.getLogger(__name__)

# ----- Safe Reply -----

async def safe_reply(interaction: discord.Interaction, content: Optional[str] = None, embed: Optional[discord.Embed] = None, ephemeral: bool = True, view: Optional[discord.ui.View] = None):
    try:
        if not interaction.response.is_done():
            if embed:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral, view=view)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral, view=view)
        else:
            if embed:
                await interaction.followup.send(embed=embed, ephemeral=ephemeral, view=view)
            else:
                await interaction.followup.send(content, ephemeral=ephemeral, view=view)
    except Exception:
        try:
            if interaction.channel:
                if embed:
                    await interaction.channel.send(f"{interaction.user.mention}", embed=embed, view=view, delete_after=15 if ephemeral else None)
                else:
                    await interaction.channel.send(f"{interaction.user.mention} {content}", view=view, delete_after=15 if ephemeral else None)
        except Exception:
            pass

# ----- Notification Manager -----

class NotificationManager:
    def __init__(self):
        self.prefs: dict[int, dict] = {}
        self.user_prefs_col = None
        self.dm_messages_col = None

    def set_collections(self, user_prefs_col, dm_messages_col):
        self.user_prefs_col = user_prefs_col
        self.dm_messages_col = dm_messages_col

    async def send(self, user: discord.abc.User, content: str, notif_type: str):
        try:
            uid = int(user.id)
        except Exception:
            uid = None
        allow = True
        try:
            if uid is not None and self.user_prefs_col is not None:
                doc = await self.user_prefs_col.find_one({"user_id": uid}, {"dm_enabled": 1})
                if doc is not None and int(doc.get("dm_enabled", 1)) == 0:
                    allow = False
        except Exception:
            allow = True

        if not allow:
            return
        try:
            msg = await user.send(content)

            delete_after = int(time.time()) + 60
            try:
                if self.dm_messages_col is not None:
                    await self.dm_messages_col.update_one(
                        {"message_id": int(msg.id)},
                        {"$set": {"message_id": int(msg.id), "channel_id": int(msg.channel.id), "user_id": int(user.id), "delete_after": delete_after}},
                        upsert=True
                    )
            except Exception:
                pass

            async def _delete_later():
                try:
                    await asyncio.sleep(60)
                    try:
                        await msg.delete()
                    finally:
                        try:
                            if self.dm_messages_col is not None:
                                await self.dm_messages_col.delete_one({"message_id": int(msg.id)})
                        except Exception:
                            pass
                except Exception:
                    pass
            _spawn_tracked(_delete_later())
        except Exception:
            pass

# Strong references for NotificationManager's fire-and-forget delete tasks.
# asyncio only weakly references tasks, so without this a scheduled DM deletion
# can be garbage collected before it runs.
_NOTIF_TASKS: set = set()


def _spawn_tracked(coro):
    try:
        task = asyncio.ensure_future(coro)
    except Exception:
        return None
    _NOTIF_TASKS.add(task)
    task.add_done_callback(_NOTIF_TASKS.discard)
    return task


notif_manager = NotificationManager()

# ----- Member Roles Cache (to minimize fetch_member in big servers) -----

class MemberRoleCache:
    """Per-(guild, user) role cache.

    Keyed by BOTH ids on purpose. Role *ids* are globally unique, but the set of
    roles a user holds is a property of a (user, guild) pair: the same person in
    two of the bot's servers has different role sets. Keying on user_id alone
    returned the first guild's roles for every other guild, which is wrong for
    any role-based matching rule.
    """

    def __init__(self, max_size: int = 5000, ttl_seconds: int = 300):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[Tuple[int, int], Tuple[int, set[int]]] = {}
        self._order: List[Tuple[int, int]] = []
        self._lock = asyncio.Lock()

    async def get_roles(self, guild: discord.Guild, user_id: int) -> Optional[set[int]]:
        now = int(time.time())
        key = (int(guild.id), int(user_id))
        async with self._lock:
            item = self._cache.get(key)
            if item and now - item[0] <= self.ttl_seconds:
                try:
                    self._order.remove(key)
                except ValueError:
                    pass
                self._order.append(key)
                return set(item[1])
        try:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            roles = {r.id for r in member.roles} if isinstance(member, discord.Member) else None
        except Exception:
            roles = None
        if roles is None:
            return None
        async with self._lock:
            # Re-check: another coroutine may have populated this while we
            # awaited, and _order can already hold a stale entry for it.
            if key in self._cache:
                try:
                    self._order.remove(key)
                except ValueError:
                    pass
            self._cache[key] = (now, set(roles))
            self._order.append(key)
            while len(self._order) > self.max_size:
                try:
                    oldest = self._order.pop(0)
                except IndexError:
                    break
                if oldest in self._cache:
                    del self._cache[oldest]
        return set(roles)

# ----- UI Views -----

class MatchPanel(discord.ui.View):
    def __init__(self, cog: "Matchmaker"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Get A Match", style=discord.ButtonStyle.secondary, custom_id="mm:join")
    async def start_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if isinstance(interaction.channel, discord.Thread):
                embed = discord.Embed(title="Cannot Join Queue", color=0xE74C3C)
                embed.description = "You cannot join the queue while in a match thread. Please leave your current match first."
                return await safe_reply(interaction, embed=embed, ephemeral=True)

            if not interaction.guild.me.guild_permissions.create_private_threads:
                embed = discord.Embed(title="Bot Missing Permissions", color=0xE74C3C)
                embed.description = "The bot needs permission to create private threads to function properly."
                return await safe_reply(interaction, embed=embed, ephemeral=True)

            await self.cog.enqueue(interaction.guild.id, interaction.user.id)
            pos, total, eta = await self.cog.get_position_and_eta(interaction.guild.id, interaction.user.id)
            minutes = eta // 60
            seconds = eta % 60
            embed = discord.Embed(title="Queue Position", color=EMBED_COLOR)
            embed.description = f"⏰ You are currently **#{pos}** in the chat queue\n👥 **Total Users Waiting:** {total}\n\nEstimated wait: {minutes}m {seconds}s"
            await safe_reply(interaction, embed=embed, ephemeral=True)
            
        except ValueError as e:
            embed = discord.Embed(title="Cannot Join Queue", color=0xE74C3C)
            embed.description = str(e)
            await safe_reply(interaction, embed=embed, ephemeral=True)
            
        except Exception:
            embed = discord.Embed(title="Error queuing. Please try again.", color=0xE74C3C)
            embed.description = "An unexpected error occurred. Please try again."
            await safe_reply(interaction, embed=embed, ephemeral=True)

class ThreadControls(discord.ui.View):
    def __init__(self, cog: "Matchmaker", guild_id: int, thread_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.thread_id = thread_id

        skip_btn = discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=f"mm:thread:{thread_id}:skip")
        leave_btn = discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary, custom_id=f"mm:thread:{thread_id}:leave")
        report_btn = discord.ui.Button(label="Report", style=discord.ButtonStyle.danger, custom_id=f"mm:thread:{thread_id}:report")

        async def on_skip(interaction: discord.Interaction):
            await self._on_skip(interaction)

        async def on_leave(interaction: discord.Interaction):
            await self._on_leave(interaction)

        async def on_report(interaction: discord.Interaction):
            await self._on_report(interaction)

        skip_btn.callback = on_skip
        leave_btn.callback = on_leave
        report_btn.callback = on_report

        self.add_item(skip_btn)
        self.add_item(leave_btn)
        self.add_item(report_btn)

    async def _on_skip(self, interaction: discord.Interaction):
        try:
            thread = await self.cog._safe_get_thread(interaction.guild, self.thread_id)
            if not thread:
                return await safe_reply(interaction, embed=discord.Embed(title="Thread not found.", color=0xE74C3C))

            other_id = self.cog._get_other_id(self.thread_id, interaction.user.id)
            if not other_id:
                return await safe_reply(interaction, embed=discord.Embed(title="No active match found.", color=0xE74C3C))

            try:
                await thread.remove_user(interaction.user)
            except Exception:
                pass

            await self.cog.block_pair(self.guild_id, interaction.user.id, other_id)
            try:
                # Record the skip so the dequeue priority bonus and the
                # "recently skipped" filters actually have data to read.
                await self.cog.match_skips_col.insert_one({
                    "guild_id": int(self.guild_id),
                    "user_id": int(interaction.user.id),
                    "thread_id": int(self.thread_id),
                    "skipped_at": int(time.time()),
                })
            except Exception:
                pass
            # The re-queue is the only step that can legitimately fail here, and
            # it must not abort the rest: block_pair, the skip record and the
            # thread teardown below have already happened, so letting this raise
            # skipped the partner notification, the dual-skip vote and the
            # confirmation embed, leaving the user staring at a generic error
            # for an action that in fact mostly succeeded.
            requeued = True
            try:
                await self.cog.enqueue(self.guild_id, interaction.user.id)
            except ValueError:
                # Already waiting (a second panel, or a double click). Fine -
                # they are in the queue either way.
                requeued = False
            except Exception:
                requeued = False
                logger.exception(
                    "Matchmaking: could not re-queue user %s after skip in guild %s",
                    interaction.user.id, self.guild_id,
                )

            await notif_manager.send(
                interaction.user,
                "Skipped your current match. You've been re-queued for a new match."
                if requeued else "Skipped your current match. You are already in the queue.",
                "skip"
            )

            try:
                other_member = interaction.guild.get_member(other_id)
                if other_member:
                    await notif_manager.send(other_member, "Your partner skipped. You'll stay in the room or you can leave and re-queue.", "skip_notice")
            except Exception:
                pass

            embed = discord.Embed(
                title="You skipped. Re-queued for a new match." if requeued else "You skipped. You are already in the queue.",
                color=EMBED_COLOR,
            )
            await safe_reply(interaction, embed=embed, ephemeral=True)

            try:
                meta = self.cog.match_meta.get(self.thread_id)
                if meta is not None:
                    votes: set[int] = meta.setdefault("skip_votes", set())
                    votes.add(int(interaction.user.id))
                    pair_ids = meta.get("pairs", [])
                    other_id2 = next((uid for uid in pair_ids if uid != interaction.user.id), None)
                    if other_id2 and int(other_id2) in votes:
                        try:
                            await thread.delete(reason="Both participants skipped")
                        except Exception:
                            pass
                        await self.cog._close_match_row(self.thread_id)
                        self.cog._forget_thread_state(self.thread_id)
                        try:
                            await self.cog.pending_deletions_col.delete_one({"thread_id": self.thread_id})
                        except Exception:
                            pass
                        await safe_reply(interaction, embed=discord.Embed(title="Both users skipped. Use the matchmaking panel to find a new match.", color=EMBED_COLOR), ephemeral=True)
            except Exception:
                pass
        except Exception:
            embed = discord.Embed(title="Error processing skip. Please try again.", color=0xE74C3C)
            await safe_reply(interaction, embed=embed)

    async def _on_leave(self, interaction: discord.Interaction):
        try:
            thread = await self.cog._safe_get_thread(interaction.guild, self.thread_id)
            if not thread:
                return await safe_reply(interaction, embed=discord.Embed(title="Thread not found.", color=0xE74C3C))

            other_id = self.cog._get_other_id(self.thread_id, interaction.user.id)

            try:
                await thread.remove_user(interaction.user)
            except Exception:
                pass

            if other_id:
                await self.cog.block_pair(self.guild_id, interaction.user.id, other_id)

            try:
                if other_id:
                    other_member = interaction.guild.get_member(other_id)
                    if other_member:
                        await notif_manager.send(other_member, "Your partner left the match. Press Leave to find a new match.", "left_notice")
            except Exception:
                pass

            embed = discord.Embed(title="You left the match. This room will be deleted in 2 minutes.", color=EMBED_COLOR)
            await safe_reply(interaction, embed=embed, ephemeral=True)

            async def _delete_later():
                await asyncio.sleep(120)
                try:
                    t = await self.cog._safe_get_thread(interaction.guild, self.thread_id)
                    if t:
                        await t.delete(reason="User left; scheduled cleanup after 2 minutes")
                except Exception:
                    pass
                await self.cog._close_match_row(self.thread_id)
                self.cog._forget_thread_state(self.thread_id)
                try:
                    await self.cog.pending_deletions_col.delete_one({"thread_id": self.thread_id})
                except Exception:
                    pass

            self.cog._spawn(_delete_later())
            
            try:
                await self.cog.pending_deletions_col.update_one(
                    {"thread_id": self.thread_id},
                    {"$set": {"thread_id": self.thread_id, "guild_id": self.guild_id, "delete_after": int(time.time()) + 120}},
                    upsert=True
                )
            except Exception:
                pass
        except Exception:
            embed = discord.Embed(title="Error processing leave. Please try again.", color=0xE74C3C)
            await safe_reply(interaction, embed=embed)

    async def _on_report(self, interaction: discord.Interaction):
        try:
            meta = self.cog.match_meta.get(self.thread_id)
            if not meta:
                return await safe_reply(interaction, embed=discord.Embed(title="No active match found.", color=0xE74C3C))
            other_id = next((uid for uid in meta.get("pairs", []) if uid != interaction.user.id), None)
            if not other_id:
                return await safe_reply(interaction, embed=discord.Embed(title="Could not find the other participant.", color=0xE74C3C))

            modal = ReportModal(self.cog, self.thread_id, other_id)
            # Register the modal before showing it. dispatch_modal() looks the
            # submission up by custom_id in the view store and silently discards
            # anything it cannot find, so an unregistered modal means on_submit
            # never runs: the user fills the form, submits, and gets "The
            # application did not respond". Each instance generates a fresh
            # custom_id, so this has to happen per report.
            try:
                self.cog.bot.add_view(modal)
            except Exception:
                logger.exception("Matchmaking: could not register report modal")
            await interaction.response.send_modal(modal)
        except Exception:
            embed = discord.Embed(title="Error processing report. Please try again.", color=0xE74C3C)
            await safe_reply(interaction, embed=embed)

# ----- Report Modal -----

class ReportModal(discord.ui.Modal):
    def __init__(self, cog: "Matchmaker", thread_id: int, reported_user_id: int):
        super().__init__(title="Report User", timeout=300)
        self.cog = cog
        self.thread_id = thread_id
        self.reported_user_id = reported_user_id

        self.reason: discord.ui.TextInput = discord.ui.TextInput(
            label="Reason for report",
            placeholder="Please describe the issue...",
            style=discord.TextStyle.short,
            required=True,
            max_length=100
        )
        self.add_item(self.reason)

        self.details: discord.ui.TextInput = discord.ui.TextInput(
            label="Additional details (optional)",
            placeholder="Provide any additional context...",
            style=discord.TextStyle.long,
            required=False,
            max_length=1000
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        # Every report registers a modal with a fresh custom_id, so the store
        # entry has to go or it grows for the life of the process.
        try:
            self.cog.bot.remove_view(self)
        except Exception:
            pass
        try:
            try:
                await interaction.response.send_message("Processing your report...", ephemeral=True)
            except discord.errors.NotFound:
                return
                
            cfg = await self.cog.get_config(interaction.guild.id)
            if not cfg or not cfg.get("report_channel_id"):
                try:
                    await interaction.edit_original_response(content="⚠️ Report channel not configured.")
                except discord.errors.NotFound:
                    pass
                return

            meta = self.cog.match_meta.get(self.thread_id)
            if not meta:
                try:
                    await interaction.edit_original_response(content="⚠️ No active match found.")
                except discord.errors.NotFound:
                    pass
                return

            report_ch = interaction.guild.get_channel(cfg["report_channel_id"])
            # ForumChannel has no .send; posting there would fail every report.
            if not isinstance(report_ch, (discord.TextChannel, discord.Thread)):
                try:
                    await interaction.edit_original_response(content="⚠️ Report channel not found.")
                except discord.errors.NotFound:
                    pass
                return

            embed = discord.Embed(title="New Matchmaking Report", color=0xE74C3C)
            embed.add_field(name="Reporter", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reported", value=f"<@{self.reported_user_id}>", inline=True)
            embed.add_field(name="Thread", value=f"<#{self.thread_id}>", inline=True)
            embed.add_field(name="Reason", value=str(self.reason.value), inline=False)
            if self.details.value:
                embed.add_field(name="Details", value=str(self.details.value), inline=False)

            transcript_file = None
            try:
                thread = interaction.guild.get_thread(self.thread_id)
                if thread:
                    lines: List[str] = []
                    async for m in thread.history(limit=None, oldest_first=True):
                        if not m.author.bot:
                            content = m.content or ""
                            if m.attachments:
                                att_text = " ".join(f"[{att.filename}]({att.url})" for att in m.attachments)
                                content = f"{content} {att_text}".strip()
                            lines.append(f"[{m.created_at.isoformat()}] {m.author} : {content}")
                    if lines:
                        transcript = "\n".join(lines)
                        transcript_file = discord.File(
                            fp=io.BytesIO(transcript.encode("utf-8")),
                            filename=f"transcript_{self.thread_id}.txt"
                        )
            except Exception:
                pass

            try:
                if transcript_file:
                    await report_ch.send(embed=embed, file=transcript_file)
                else:
                    await report_ch.send(embed=embed)
            except Exception:
                try:
                    await interaction.edit_original_response(content="⚠️ Error sending report to staff. Please contact a moderator.")
                except discord.errors.NotFound:
                    pass
                return

            try:
                await interaction.edit_original_response(content="✅ Report submitted successfully. Thank you for helping keep the community safe.")
            except discord.errors.NotFound:
                pass

        except Exception:
            try:
                await interaction.edit_original_response(content="❌ Error submitting report. Please try again.")
            except discord.errors.NotFound:
                pass

# ----- Main Cog -----

class Matchmaker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._paused: set[int] = set()
        self._watch: dict[int, Tuple[set[int], asyncio.Event, asyncio.Task]] = {}
        self.match_meta: dict[int, dict] = {}
        self._initialized = False
        self._cleanup_lock = asyncio.Lock()
        self._roles_cache = MemberRoleCache(max_size=10000, ttl_seconds=300)
        self._on_ready_done = False
        # guild_id -> unix ts until which match attempts are backed off (thread
        # creation failures) so one broken guild cannot livelock its queue.
        self._match_backoff: dict[int, int] = {}
        # thread_id -> unix ts of the last last_activity write (throttle).
        self._activity_write_ts: dict[int, int] = {}
        # Strong references to fire-and-forget tasks. asyncio only holds a weak
        # reference to a task, so an unreferenced create_task can be garbage
        # collected mid-execution and silently drop the work.
        self._bg_tasks: set[asyncio.Task] = set()

        db = self.bot.mongo_client['discord_bot']
        self.guild_config_col = db['matchmaker_guild_config']
        self.waiting_queue_col = db['matchmaker_waiting_queue']
        self.matches_col = db['matchmaker_matches']
        self.recent_blocks_col = db['matchmaker_recent_blocks']
        self.match_skips_col = db['matchmaker_match_skips']
        self.queue_history_col = db['matchmaker_queue_history']
        self.queue_panels_col = db['matchmaker_queue_panels']
        self.pending_deletions_col = db['matchmaker_pending_deletions']
        self.user_prefs_col = db['matchmaker_user_prefs']
        self.dm_messages_col = db['matchmaker_dm_messages']

        notif_manager.set_collections(self.user_prefs_col, self.dm_messages_col)

    def cog_unload(self):
        for loop in (
            getattr(self, "match_loop", None),
            getattr(self, "queue_panel_loop", None),
            getattr(self, "cleanup_loop", None),
            getattr(self, "dm_cleanup_loop", None),
            getattr(self, "cleanup_inactive_threads", None),
        ):
            try:
                if loop and loop.is_running():
                    loop.cancel()
            except Exception:
                pass

    # ----- Helpers for thread actions -----
    def _spawn(self, coro):
        """Run a fire-and-forget coroutine, keeping a strong reference.

        asyncio holds only a weak reference to a running task, so a bare
        create_task can be garbage collected before it finishes. The wrapper
        also swallows the result so a failing background job never surfaces as
        "Task exception was never retrieved".
        """
        async def _runner():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Matchmaking: background task failed")

        try:
            task = asyncio.ensure_future(_runner())
        except Exception:
            return None
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _get_thread_meta(self, thread_id: int):
        return self.match_meta.get(thread_id)

    def _forget_thread_state(self, thread_id: int):
        """Drop every piece of in-memory state for a finished match.

        The view must be unregistered using the *same instance* that was passed
        to add_view. discord.py's ViewStore.remove_view compares against the
        view's own item snapshot, and a freshly constructed ThreadControls has
        an empty one - so removing a new instance silently does nothing and the
        store entry (and its routing) survived for the life of the process.
        """
        meta = self.match_meta.pop(thread_id, None)
        self._activity_write_ts.pop(thread_id, None)
        view = meta.get("view") if isinstance(meta, dict) else None
        if view is not None:
            try:
                self.bot.remove_view(view)
            except Exception:
                pass
        return meta

    def _get_other_id(self, thread_id: int, user_id: int) -> Optional[int]:
        meta = self._get_thread_meta(thread_id)
        if not meta:
            return None
        pairs = meta.get("pairs", [])
        other = next((uid for uid in pairs if uid != user_id), None)
        return other

    async def _safe_get_thread(self, guild: discord.Guild, thread_id: int) -> Optional[discord.Thread]:
        th = guild.get_thread(thread_id)
        if isinstance(th, discord.Thread):
            return th
        try:
            ch = await guild.fetch_channel(thread_id)
            return ch if isinstance(ch, discord.Thread) else None
        except Exception:
            return None

    async def _close_match_row(self, thread_id: int):
        try:
            await self.matches_col.update_one(
                {"thread_id": thread_id},
                {"$set": {"closed_at": int(time.time()), "status": "closed"}}
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            if self._on_ready_done:
                return

            await self.guild_config_col.create_index([("guild_id", 1)], unique=True)
            try:
                await self.waiting_queue_col.create_index(
                    [("guild_id", 1), ("user_id", 1)], unique=True
                )
            except Exception:
                pass
            await self.waiting_queue_col.create_index([("guild_id", 1), ("enqueued_at", 1)])
            await self.waiting_queue_col.create_index([("guild_id", 1), ("priority_score", -1)])
            await self.matches_col.create_index([("guild_id", 1), ("status", 1), ("last_activity", 1)])
            await self.recent_blocks_col.create_index([("guild_id", 1), ("blocked_until", 1)])
            await self.match_skips_col.create_index([("guild_id", 1), ("skipped_at", 1)])
            await self.queue_history_col.create_index([("guild_id", 1), ("user_id", 1)])
            await self.queue_history_col.create_index([("guild_id", 1), ("matched_at", 1)])
            await self.queue_panels_col.create_index([("guild_id", 1)], unique=True)
            await self.pending_deletions_col.create_index([("delete_after", 1)])
            await self.dm_messages_col.create_index([("delete_after", 1)])

            self.bot.add_view(MatchPanel(self))
            try:
                # Dedupe waiting queue: if duplicates existed before adding unique index
                try:
                    for g in self.bot.guilds:
                        cur = self.waiting_queue_col.aggregate([
                            {"$match": {"guild_id": g.id}},
                            {"$group": {
                                "_id": {"guild_id": "$guild_id", "user_id": "$user_id"},
                                "ids": {"$addToSet": "$_id"},
                                "count": {"$sum": 1}
                            }},
                            {"$match": {"count": {"$gt": 1}}}
                        ])
                        async for grp in cur:
                            ids = grp.get("ids") or []
                            for oid in list(ids)[1:]:
                                await self.waiting_queue_col.delete_one({"_id": oid})
                except Exception:
                    pass
            except Exception:
                pass
            try:
                open_rows = await self.matches_col.find(
                    {"status": "open"},
                    {"thread_id": 1, "guild_id": 1, "user1_id": 1, "user2_id": 1, "created_at": 1}
                ).to_list(length=None)
                for r in open_rows or []:
                    tid = int(r["thread_id"])
                    gid = int(r["guild_id"])
                    # Rebuild in-memory match state so skip/report/leave controls
                    # keep working for matches created before a restart.
                    if r.get("user1_id") is not None and r.get("user2_id") is not None:
                        self.match_meta.setdefault(tid, {
                            "guild_id": gid,
                            "pairs": [int(r["user1_id"]), int(r["user2_id"])],
                            "created_at": int(r.get("created_at", 0)),
                        })
                    guild = self.bot.get_guild(gid)
                    if guild:
                        th = await self._safe_get_thread(guild, tid)
                        if th:
                            self.bot.add_view(ThreadControls(self, gid, tid))
            except Exception:
                pass

            # Restore paused guilds. _paused was in-memory only, so a restart
            # silently resumed matchmaking in guilds an admin had shut off.
            try:
                paused_rows = await self.guild_config_col.find(
                    {"paused": True}, {"guild_id": 1}
                ).to_list(length=None)
                self._paused = {int(r["guild_id"]) for r in paused_rows or []}
            except Exception:
                pass

            self._initialized = True
            if not self.match_loop.is_running():
                self.match_loop.start()
            if not self.cleanup_inactive_threads.is_running():
                self.cleanup_inactive_threads.start()
            if not self.queue_panel_loop.is_running():
                self.queue_panel_loop.start()
            if not self.cleanup_loop.is_running():
                self.cleanup_loop.start()
            if not self.dm_cleanup_loop.is_running():
                self.dm_cleanup_loop.start()
            self._on_ready_done = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Re-raising here was the worst possible behaviour: one failed
            # create_index (for example an IndexOptionsConflict, or a
            # transient timeout) propagated out of on_ready and left
            # _initialized False with every loop dead, so the cog looked
            # loaded but did nothing for the life of the process. Startup is
            # best-effort per step; the loops are started either way.
            logger.exception("Matchmaking: startup step failed; continuing")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Keep `last_activity` fresh for open match threads (inactivity cleanup)."""
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        thread_id = message.channel.id
        if thread_id not in self.match_meta:
            self._activity_write_ts.pop(thread_id, None)
            return
        now = int(time.time())
        if now - self._activity_write_ts.get(thread_id, 0) < 60:
            return
        self._activity_write_ts[thread_id] = now
        try:
            if self.matches_col is not None:
                await self.matches_col.update_one(
                    {"thread_id": thread_id},
                    {"$set": {"last_activity": now}}
                )
        except Exception:
            pass

    # ----- Configuration -----

    async def set_parent_channel(self, gid: int, ch: int):
        await self.guild_config_col.update_one(
            {"guild_id": gid},
            {"$set": {"guild_id": gid, "parent_channel_id": int(ch), "channel_id": int(ch)}},
            upsert=True
        )

    async def set_report_channel(self, gid: int, rc: int):
        await self.guild_config_col.update_one(
            {"guild_id": gid},
            {"$set": {"guild_id": gid, "report_channel_id": int(rc)}},
            upsert=True
        )

    async def consume_room_number(self, gid: int) -> int:
        """Atomically hand out the next room number for this guild.

        $inc inside find_one_and_update means two matches created in the same
        tick can never be told they own the same room number. We read the value
        AFTER the increment, so a brand new guild lands on room 1 and an
        existing guild continues from wherever its counter left off.
        """
        try:
            doc = await self.guild_config_col.find_one_and_update(
                {"guild_id": gid},
                {"$inc": {"next_room_number": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except Exception:
            logger.exception("Matchmaking: room number allocation failed for guild %s", gid)
            return 1
        if not doc:
            return 1
        try:
            return max(1, int(doc.get("next_room_number") or 1))
        except (TypeError, ValueError):
            return 1

    # ----- Queue Panel Helpers -----

    async def _get_queue_counts(self, guild_id: int) -> Dict[str, int]:
        total = await self.waiting_queue_col.count_documents({"guild_id": guild_id})
        return {"total": int(total or 0)}

    def _build_queue_embed(self, guild: discord.Guild, counts: Dict[str, int]) -> discord.Embed:
        embed = discord.Embed(title=f"Queue — {guild.name}", color=EMBED_COLOR)
        embed.add_field(name="Users Waiting", value=str(counts.get("total", 0)), inline=True)
        embed.set_footer(text="Updates every ~20s")
        return embed

    async def _get_queue_panel_row(self, guild_id: int) -> Optional[dict]:
        try:
            return await self.queue_panels_col.find_one({"guild_id": guild_id})
        except Exception:
            return None

    async def _upsert_queue_panel_row(self, guild_id: int, channel_id: int, message_id: int):
        await self.queue_panels_col.update_one(
            {"guild_id": guild_id},
            {"$set": {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id}},
            upsert=True
        )

    async def _delete_queue_panel_row(self, guild_id: int):
        try:
            await self.queue_panels_col.delete_one({"guild_id": guild_id})
        except Exception:
            pass

    # ----- Admin Commands -----

    @app_commands.guild_only()
    @app_commands.command(name="mm", description="Configure matchmaking")
    @app_commands.describe(
        action="setup/configure/report_channel/clear/pause/resume/stats/queue/queue_panel",
        channel="Text Channel",
        report="Report Channel",
        days="Stat Days (1-3650)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="configure", value="configure"),
        app_commands.Choice(name="report_channel", value="report_channel"),
        app_commands.Choice(name="clear", value="clear"),
        app_commands.Choice(name="pause", value="pause"),
        app_commands.Choice(name="resume", value="resume"),
        app_commands.Choice(name="stats", value="stats"),
        app_commands.Choice(name="queue", value="queue"),
        app_commands.Choice(name="queue_panel", value="queue_panel"),
    ])
    async def mm(self,
                 interaction: discord.Interaction,
                 action: app_commands.Choice[str],
                 channel: Optional[discord.TextChannel] = None,
                 report: Optional[discord.TextChannel] = None,
                 days: int = 7):
        # Admin check runs before any state change, and the whole body is
        # wrapped: every action below operates on a deferred interaction, so an
        # unhandled raise would leave Discord showing "The application did not
        # respond" instead of an error the admin can act on.
        try:
            perms = interaction.user.guild_permissions if interaction.guild else None
            if perms is None or not perms.administrator:
                return await interaction.response.send_message(
                    "You need administrator permissions to use this command.",
                    ephemeral=True,
                )

            if interaction.response.is_done() is False:
                await interaction.response.defer(ephemeral=True)

            act = action.value
            guild = interaction.guild
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
            except Exception:
                pass
            return

        try:
            return await self._run_mm_action(interaction, act, guild, channel, report, days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Matchmaking: /mm action %s failed in guild %s", act, interaction.guild_id)
            try:
                await interaction.edit_original_response(
                    content="❌ That action failed. Check the bot logs for details."
                )
            except Exception:
                pass

    async def _run_mm_action(self, interaction, act: str, guild, channel, report, days: int):
        if act == "setup":
            if not all([channel, report]):
                return await interaction.edit_original_response(content="Provide channel and report channel.")
            await self.set_parent_channel(guild.id, channel.id)
            await self.set_report_channel(guild.id, report.id)

            embed = discord.Embed(
                title="Private Rooms",
                description="Get paired with a random stranger in a private room for a one-on-one chat.",
                color=EMBED_COLOR
            )
            embed.set_image(url=MATCHMAKER_BANNER_URL)

            try:
                await channel.send(embed=embed, view=MatchPanel(self))
            except Exception:
                logger.exception("Matchmaking: could not post panel in guild %s", guild.id)

            return await interaction.edit_original_response(content="✅ Setup complete! Matchmaking panel created.")

        if act == "configure":
            if not channel:
                return await interaction.edit_original_response(content="Provide channel.")
            await self.set_parent_channel(guild.id, channel.id)

            embed = discord.Embed(
                title="Matchmaking Panel",
                description="Click the button below to find your match.",
                color=EMBED_COLOR
            )
            embed.set_image(url=MATCHMAKER_BANNER_URL)

            try:
                await channel.send(embed=embed, view=MatchPanel(self))
            except Exception:
                logger.exception("Matchmaking: could not post panel in guild %s", guild.id)

            return await interaction.edit_original_response(content=f"✅ Configured to {channel.mention}")

        if act == "report_channel":
            if not report:
                return await interaction.edit_original_response(content="Provide a report channel.")
            await self.set_report_channel(guild.id, report.id)
            return await interaction.edit_original_response(content=f"✅ Report channel set to {report.mention}")

        if act == "clear":
            await self.waiting_queue_col.delete_many({"guild_id": guild.id})
            await self.recent_blocks_col.delete_many({"guild_id": guild.id})
            return await interaction.edit_original_response(content="🧹 Cleared waiting queue and recent blocks.")

        if act == "pause":
            self._paused.add(guild.id)
            try:
                await self.guild_config_col.update_one(
                    {"guild_id": guild.id},
                    {"$set": {"guild_id": guild.id, "paused": True}},
                    upsert=True
                )
            except Exception:
                pass
            return await interaction.edit_original_response(content="⏸️ Matchmaking paused for this server.")

        if act == "resume":
            self._paused.discard(guild.id)
            try:
                await self.guild_config_col.update_one(
                    {"guild_id": guild.id},
                    {"$set": {"guild_id": guild.id, "paused": False}},
                    upsert=True
                )
            except Exception:
                pass
            return await interaction.edit_original_response(content="▶️ Matchmaking resumed for this server.")

        if act == "stats":
            try:
                # An unbounded or negative window silently reported nonsense.
                days = max(1, min(int(days or 7), 3650))
                since = int(time.time()) - days * 86400
                total_matches = await self.matches_col.count_documents(
                    {"guild_id": guild.id, "created_at": {"$gte": since}}
                )
                queued_now = await self.waiting_queue_col.count_documents({"guild_id": guild.id})
                embed = discord.Embed(title="Matchmaking Stats", color=EMBED_COLOR)
                embed.add_field(name="Days", value=str(days), inline=True)
                embed.add_field(name="Matches Created", value=str(int(total_matches or 0)), inline=True)
                embed.add_field(name="Queued Now", value=str(int(queued_now or 0)), inline=True)
                return await interaction.edit_original_response(embed=embed)
            except Exception:
                return await interaction.edit_original_response(content="❌ Failed to compute stats.")

        if act == "queue":
            count = await self.waiting_queue_col.count_documents({"guild_id": guild.id})
            embed = discord.Embed(title="Current Queue", color=EMBED_COLOR)
            embed.add_field(name="Users Waiting", value=str(int(count or 0)), inline=True)
            return await interaction.edit_original_response(embed=embed)

        if act == "queue_panel":
            if not channel:
                return await interaction.edit_original_response(content="Provide channel to host the queue panel.")
            counts = await self._get_queue_counts(guild.id)
            embed = self._build_queue_embed(guild, counts)
            # Try to reuse existing message if present in same channel
            row = await self._get_queue_panel_row(guild.id)
            panel_msg = None
            if row:
                if int(row["channel_id"]) == channel.id:
                    try:
                        panel_msg = await channel.fetch_message(int(row["message_id"]))
                    except Exception:
                        panel_msg = None
                else:
                    # The panel now lives in a different channel. Forget the old
                    # row so the 20s loop stops editing the previous message,
                    # which would otherwise update forever in a channel nobody
                    # is looking at.
                    await self._delete_queue_panel_row(guild.id)
            if panel_msg:
                await panel_msg.edit(embed=embed)
                await self._upsert_queue_panel_row(guild.id, channel.id, panel_msg.id)
                return await interaction.edit_original_response(content=f"✅ Queue panel updated in {channel.mention}.")
            else:
                sent = await channel.send(embed=embed)
                await self._upsert_queue_panel_row(guild.id, channel.id, sent.id)
                return await interaction.edit_original_response(content=f"✅ Queue panel created in {channel.mention}.")

        return await interaction.edit_original_response(content="Unknown action.")

    # ----- Queue Helpers -----
    #
    # There used to be calculate_priority/update_priority here. Neither had a
    # caller: priority is actually computed fresh inside dequeue_pair on every
    # scan (waiting minutes, plus a skip bonus), because a value stored at
    # enqueue time goes stale immediately. The stored priority_score therefore
    # stayed 0 forever, which is harmless - it just means ranking falls through
    # to enqueued_at, i.e. first-in-first-out.

    async def enqueue(self, guild_id: int, user_id: int):
        if not isinstance(guild_id, int) or guild_id <= 0:
            raise ValueError(f"Invalid guild_id: {guild_id}")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError(f"Invalid user_id: {user_id}")

        ts = int(time.time())
        max_retries = 3
        last_error = None
        
        try:
            existing = await self.waiting_queue_col.find_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"_id": 1}
            )
            if existing:
                raise ValueError("You are already in the queue!")
        except ValueError:
            raise
        except Exception:
            pass
            
        for attempt in range(max_retries):
            try:
                wait = 0
                score = wait // 60
                await self.waiting_queue_col.update_one(
                    {"guild_id": guild_id, "user_id": user_id},
                    {"$set": {
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "enqueued_at": ts,
                        "priority_score": score,
                        "boost_until": None
                    }},
                    upsert=True
                )
                return
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue
        
        raise last_error

    async def get_position_and_eta(self, guild_id: int, user_id: int) -> Tuple[int, int, int]:
        # Ranked with counting queries rather than by pulling the whole queue
        # into memory and sorting it client-side: a busy guild's queue is
        # unbounded, and this runs on every button press.
        row = await self.waiting_queue_col.find_one(
            {"guild_id": guild_id, "user_id": int(user_id)},
            {"priority_score": 1, "enqueued_at": 1},
        )
        if not row:
            return (0, await self.waiting_queue_col.count_documents({"guild_id": guild_id}), 0)

        my_score = int(row.get("priority_score") or 0)
        my_ts = int(row.get("enqueued_at") or 0)
        # Same ordering as dequeue_pair: highest priority first, then oldest.
        ahead = await self.waiting_queue_col.count_documents({
            "guild_id": guild_id,
            "$or": [
                {"priority_score": {"$gt": my_score}},
                {"priority_score": my_score, "enqueued_at": {"$lt": my_ts}},
            ],
        })
        total = await self.waiting_queue_col.count_documents({"guild_id": guild_id})
        position = min(int(ahead) + 1, int(total) or 1)
        # Two people are consumed per match, so a slot frees roughly every other
        # match_loop tick (~20s) when the rest of the queue is matchable.
        pair_slots = max(1, math.ceil(ahead / 2))
        eta_seconds = pair_slots * 20
        return (position, total, eta_seconds)

    async def _purge_dead_queue_rows(self, guild: discord.Guild, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop queue rows that can never be matched.

        Two kinds of row are dead weight:

        * a user who has left the guild (or is otherwise unresolvable), and
        * a duplicate row for a user who already has one.

        Neither could ever produce a match - the pairing loop skips them - but
        they stayed in the collection forever and, because candidates are read
        oldest-first under a LIMIT, a few thousand stale rows at the front of
        the queue meant the real waiters were never even fetched, so the guild
        stopped matching altogether.
        """
        alive: List[Dict[str, Any]] = []
        seen: set[int] = set()
        dead_ids: List[object] = []
        for row in candidates:
            try:
                uid = int(row["user_id"])
            except (KeyError, TypeError, ValueError):
                dead_ids.append(row.get("_id"))
                continue
            if uid in seen:
                dead_ids.append(row.get("_id"))
                continue
            if await self._roles_cache.get_roles(guild, uid) is None:
                dead_ids.append(row.get("_id"))
                continue
            seen.add(uid)
            alive.append(row)

        dead_ids = [i for i in dead_ids if i is not None]
        if dead_ids:
            try:
                await self.waiting_queue_col.delete_many({"_id": {"$in": dead_ids}})
            except Exception:
                logger.exception(
                    "Matchmaking: could not prune %d dead queue rows in guild %s",
                    len(dead_ids), guild.id,
                )
        return alive

    async def _load_live_candidates(self, guild: discord.Guild, want: int) -> List[Dict[str, Any]]:
        """Return up to `want` queue rows that could actually be matched.

        Reads in pages of MAX_DB_CANDIDATES and drops the unusable rows as it
        goes. Paging matters: candidates are read oldest-first under a LIMIT, so
        if the head of a guild's queue is made up of members who have since
        left, a single page can be entirely dead weight and the live waiters
        behind them are never even fetched - the guild then stops matching
        entirely, even though nothing about the bot has changed. Deleting the
        dead rows as we go both unblocks the window and stops the queue length
        from overstating how many people are really waiting.
        """
        alive: List[Dict[str, Any]] = []
        for _ in range(MAX_QUEUE_PAGES):
            if len(alive) >= want:
                break
            # skip(len(alive)) is exact because of what _purge_dead_queue_rows
            # did on the previous pass: it deleted every row it stepped over and
            # kept every row in `alive`. So the rows still ahead of the cursor are
            # precisely the ones already collected, and the deleted ones no
            # longer shift anything.
            cursor = self.waiting_queue_col.find(
                {"guild_id": guild.id},
                {"user_id": 1, "enqueued_at": 1, "boost_until": 1}
            ).sort("enqueued_at", 1).skip(len(alive)).limit(MAX_DB_CANDIDATES)
            page = await cursor.to_list(length=MAX_DB_CANDIDATES)
            if not page:
                break
            fresh = await self._purge_dead_queue_rows(guild, [dict(r) for r in page])
            if not fresh:
                if len(page) < MAX_DB_CANDIDATES:
                    break
                # Whole page was dead and is now deleted, so the next iteration's
                # window slides forward by a full page on its own.
                continue
            alive.extend(fresh)
        return alive[:want] if want else alive

    async def dequeue_pair(self, guild: discord.Guild):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                cands: List[Dict[str, Any]] = await self._load_live_candidates(
                    guild, 2 * SCAN_WINDOW + 1
                )
                if len(cands) < 2:
                    return None

                now_ts = int(time.time())
                pipeline = [
                    {"$match": {"guild_id": guild.id, "skipped_at": {"$gte": now_ts - 300}}},
                    {"$group": {"_id": "$user_id", "last_skip": {"$max": "$skipped_at"}}}
                ]
                skip_cursor = self.match_skips_col.aggregate(pipeline)
                skips = await skip_cursor.to_list(length=None)
                
                recent_skips = {int(r["_id"]): int(r["last_skip"]) for r in skips if r.get("_id") is not None}
                
                for c in cands:
                    user_id = int(c["user_id"])
                    waited = max(0, now_ts - int(c["enqueued_at"]))
                    base_score = waited // 60
                    
                    if user_id in recent_skips:
                        skip_bonus = 120
                        c["priority_score"] = base_score + skip_bonus
                    else:
                        c["priority_score"] = base_score
                
                cands.sort(key=lambda x: (-x["priority_score"], x["enqueued_at"]))

                now = int(time.time())
                # Purge expired blocks, then read back only the ones still active.
                await self.recent_blocks_col.delete_many({"guild_id": guild.id, "blocked_until": {"$lte": now}})
                blk_cursor = self.recent_blocks_col.find(
                    {"guild_id": guild.id, "blocked_until": {"$gt": now}},
                    {"user1_id": 1, "user2_id": 1}
                )
                blk = await blk_cursor.to_list(length=None)
                blocks = {(int(r["user1_id"]), int(r["user2_id"])) for r in blk}
                
                day_ago = now - (24 * 60 * 60)
                skip_threads = await self.match_skips_col.distinct("thread_id", {"guild_id": guild.id, "skipped_at": {"$gte": day_ago}})
                matched_cursor = self.matches_col.find(
                    {
                        "guild_id": guild.id,
                        "created_at": {"$gte": day_ago},
                        "status": "open",
                        "thread_id": {"$nin": skip_threads}
                    },
                    {"user1_id": 1, "user2_id": 1}
                )
                matched = await matched_cursor.to_list(length=None)
                recently_matched = set()
                for match in matched:
                    recently_matched.add(int(match["user1_id"]))
                    recently_matched.add(int(match["user2_id"]))

                # Only the first 2 * SCAN_WINDOW + 1 candidates can ever take
                # part in a pair (see the slice below), so resolve their roles
                # once here. The old code awaited a locked cache lookup twice per
                # *pair*, which is up to 2 * SCAN_WINDOW**2 awaits - around
                # 80 000 per guild per tick, enough to stall the event loop
                # while the queue was busy and nothing matched.
                window = cands[: 2 * SCAN_WINDOW + 1]
                roles_by_user: Dict[int, Optional[set[int]]] = {}
                for row in window:
                    uid = int(row["user_id"])
                    if uid not in roles_by_user:
                        roles_by_user[uid] = await self._roles_cache.get_roles(guild, uid)
                # Someone who is not (or is no longer) a guild member can never
                # be added to a private thread. Tested with `is None` rather than
                # truthiness: an empty role set is falsy but still a real member.
                window = [r for r in window if roles_by_user.get(int(r["user_id"])) is not None]
                if len(window) < 2:
                    return None

                for i, u1 in enumerate(window[:SCAN_WINDOW]):
                    for u2 in window[i + 1:i + 1 + SCAN_WINDOW]:
                        uid1 = int(u1["user_id"])
                        uid2 = int(u2["user_id"])
                        if uid1 == uid2:
                            continue
                        pair_sorted = (uid1, uid2) if uid1 < uid2 else (uid2, uid1)
                        if pair_sorted in blocks:
                            continue
                        if uid1 in recently_matched or uid2 in recently_matched:
                            continue
                        return (u1, u2)
                return None
            except Exception:
                logger.exception(
                    "Matchmaking: candidate scan failed for guild %s (attempt %d/%d)",
                    guild.id, attempt + 1, max_retries,
                )
                if attempt == max_retries - 1:
                    return None
                await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def _ensure_thread_perms(self, channel: discord.TextChannel, member: discord.Member):
        try:
            perms = channel.permissions_for(member)
            if hasattr(perms, "send_messages_in_threads") and perms.send_messages_in_threads is False:
                try:
                    await channel.set_permissions(member, send_messages_in_threads=True, view_channel=True)
                except Exception:
                    pass
        except Exception:
            pass

    async def _grant_thread_overwrites(self, thread: discord.Thread, member: discord.Member):
        try:
            ow = discord.PermissionOverwrite()
            for name in [
                "view_channel",
                "send_messages",
                "attach_files",
                "embed_links",
                "add_reactions",
                "use_external_emojis",
                "use_external_stickers",
                "send_voice_messages",
                "send_tts_messages",
                "use_application_commands",
            ]:
                try:
                    setattr(ow, name, True)
                except Exception:
                    pass
            await thread.set_permissions(member, overwrite=ow)
        except Exception:
            pass

    async def block_pair(self, guild_id: int, a: int, b: int, minutes: int = SKIP_BLOCK_MINUTES):
        u1, u2 = sorted((int(a), int(b)))
        until = int(time.time()) + minutes * 60
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.recent_blocks_col.update_one(
                    {"guild_id": guild_id, "user1_id": u1, "user2_id": u2},
                    {"$set": {"guild_id": guild_id, "user1_id": u1, "user2_id": u2, "blocked_until": until}},
                    upsert=True
                )
                return
            except Exception:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))

    # ----- Matching -----

    async def get_config(self, guild_id: int) -> Optional[dict]:
        try:
            cfg = await self.guild_config_col.find_one({"guild_id": guild_id})
            return cfg
        except Exception:
            return None

    async def _create_match_thread(self, guild: discord.Guild, u1_id: int, u2_id: int) -> Optional[discord.Thread]:
        try:
            cfg = await self.get_config(guild.id)
            # parent_channel_id is what /mm writes; channel_id is the older field
            # name, still honoured for guilds configured before the rename.
            channel_id = None
            if cfg:
                channel_id = cfg.get("parent_channel_id") or cfg.get("channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                # Only a channel the guild explicitly configured may host rooms.
                # Guessing at "any text channel the bot can post in" put match
                # threads in #general for unconfigured or blipped guilds.
                logger.warning(
                    "Matchmaking: guild %s has no usable configured parent channel; "
                    "run /mm action:configure to set one",
                    guild.id,
                )
                return None
            if not channel.permissions_for(guild.me).create_private_threads:
                logger.warning(
                    "Matchmaking: configured channel %s in guild %s is missing "
                    "Create Private Threads; cannot create rooms there",
                    channel.id, guild.id,
                )
                return None

            room_no = await self.consume_room_number(guild.id)
            ts = int(time.time())

            m1 = guild.get_member(u1_id) or await guild.fetch_member(u1_id)
            m2 = guild.get_member(u2_id) or await guild.fetch_member(u2_id)
            for m in (m1, m2):
                if m:
                    await self._ensure_thread_perms(channel, m)

            thread = await channel.create_thread(
                name=f"Room {room_no}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="Matchmaking"
            )

            for m in (m1, m2):
                if m:
                    try:
                        await thread.add_user(m)
                    except Exception:
                        pass
                    await self._grant_thread_overwrites(thread, m)

            self.match_meta[thread.id] = {
                "guild_id": guild.id,
                "pairs": [u1_id, u2_id],
                "created_at": ts,
                "room_no": room_no,
            }

            await self.matches_col.update_one(
                {"thread_id": thread.id},
                {"$set": {
                    "thread_id": thread.id,
                    "guild_id": guild.id,
                    "user1_id": u1_id,
                    "user2_id": u2_id,
                    "status": "open",
                    "created_at": ts,
                    "last_activity": ts,
                    "room_no": room_no
                }},
                upsert=True
            )

            # Post the room embed carrying the Skip/Leave/Report controls. The
            # view is also registered via add_view so those buttons keep routing
            # after a restart, but this message is what makes them visible.
            controls = ThreadControls(self, guild.id, thread.id)
            try:
                self.bot.add_view(controls)
            except Exception:
                controls = None
            # Keep the instance so the view can actually be unregistered later;
            # see _forget_thread_state.
            self.match_meta[thread.id]["view"] = controls
            if controls is not None:
                try:
                    embed = discord.Embed(
                        title=f"Room {room_no}",
                        description="This private room is just for you two. Be respectful, have fun, and enjoy your chat!",
                        color=EMBED_COLOR
                    )
                    await thread.send(embed=embed, view=controls)
                except Exception:
                    pass
            return thread
        except Exception:
            return None

    async def _attempt_match(self, guild: discord.Guild):
        try:
            if int(time.time()) < self._match_backoff.get(guild.id, 0):
                return
            async with self._locks[guild.id]:
                pair = await self.dequeue_pair(guild)
                if pair is None:
                    return
                u1_doc, u2_doc = pair
                u1_id = int(u1_doc["user_id"])
                u2_id = int(u2_doc["user_id"])

                # Resolve both members BEFORE removing anyone from the queue so a
                # failed match never silently drops the other participant.
                if u1_id == u2_id:
                    # dequeue_pair can no longer produce this - it de-duplicates
                    # and drops unresolvable rows before returning - so reaching
                    # here means the invariant broke. Log loudly and drop the row
                    # rather than pairing someone with themselves.
                    logger.error(
                        "Matchmaking: dequeue_pair returned the same user twice in guild %s (%s); purging",
                        guild.id, u1_id,
                    )
                    await self.waiting_queue_col.delete_many({"guild_id": guild.id, "user_id": u1_id})
                    return
                m1 = guild.get_member(u1_id)
                if m1 is None:
                    try:
                        m1 = await guild.fetch_member(u1_id)
                    except discord.NotFound:
                        # User left the guild: drop only their row and retry later.
                        await self.waiting_queue_col.delete_many({"guild_id": guild.id, "user_id": u1_id})
                        return
                m2 = guild.get_member(u2_id)
                if m2 is None:
                    try:
                        m2 = await guild.fetch_member(u2_id)
                    except discord.NotFound:
                        await self.waiting_queue_col.delete_many({"guild_id": guild.id, "user_id": u2_id})
                        return

                thread = await self._create_match_thread(guild, u1_id, u2_id)
                if not thread:
                    # Keep both users queued (a transient failure must not eat their
                    # place); back off so a persistent failure cannot livelock the
                    # queue by retrying the same pair every tick.
                    self._match_backoff[guild.id] = int(time.time()) + 60
                    logger.warning(
                        "Matchmaking: could not create match thread in guild %s for %s/%s; users left in queue",
                        guild.id, u1_id, u2_id,
                    )
                    return

                await self.waiting_queue_col.delete_many({"guild_id": guild.id, "user_id": u1_id})
                await self.waiting_queue_col.delete_many({"guild_id": guild.id, "user_id": u2_id})

                # Record the pairing, now that the thread actually exists.
                matched_at = int(time.time())
                for doc in (u1_doc, u2_doc):
                    try:
                        await self.queue_history_col.insert_one({
                            "guild_id": guild.id,
                            "user_id": int(doc["user_id"]),
                            "enqueued_at": int(doc["enqueued_at"]),
                            "matched_at": matched_at,
                        })
                    except Exception:
                        pass

                try:
                    if m1:
                        await notif_manager.send(m1, f"Match found! Join your private thread: {thread.mention}", "match")
                    if m2:
                        await notif_manager.send(m2, f"Match found! Join your private thread: {thread.mention}", "match")
                except Exception:
                    pass
        except Exception:
            # This used to be a bare `pass`, which meant any failure in the
            # matching path left no trace at all: the queue simply stopped
            # producing rooms and the only symptom was silence.
            logger.exception("Matchmaking: match attempt failed in guild %s", guild.id)

    @tasks.loop(seconds=5)
    async def match_loop(self):
        if not self._initialized:
            return
        for guild in list(self.bot.guilds):
            if guild.id in self._paused:
                continue
            lock = self._locks[guild.id]
            if lock.locked():
                continue
            try:
                # _spawn, not loop.create_task: asyncio only holds a weak
                # reference to a task, so a bare create_task could be garbage
                # collected mid-flight. That silently abandoned attempts in the
                # middle - sometimes after the Discord thread had been created
                # but before the pair left the queue - which is exactly how rooms
                # ended up duplicated or orphaned.
                self._spawn(self._attempt_match(guild))
            except Exception:
                logger.exception("Matchmaking: could not schedule a match attempt for guild %s", guild.id)

    @tasks.loop(minutes=1)
    async def cleanup_inactive_threads(self):
        if not self._initialized:
            return
            
        try:
            now = int(time.time())
            inactive_threshold = now - (30 * 60)
            
            # Fetch once for all guilds: doing a distinct() per guild in the
            # loop was a query per guild per minute for no extra correctness.
            try:
                pending_thread_ids = await self.pending_deletions_col.distinct("thread_id")
            except Exception:
                pending_thread_ids = []

            for guild in self.bot.guilds:
                try:
                    cursor = self.matches_col.find({
                        "guild_id": guild.id,
                        "status": "open",
                        "last_activity": {"$lte": inactive_threshold},
                        "thread_id": {"$nin": pending_thread_ids}
                    })
                    rows = await cursor.to_list(length=None)
                    
                    for row in rows:
                        thread_id = int(row["thread_id"])
                        try:
                            thread = await self._safe_get_thread(guild, thread_id)
                            if thread:
                                # Cross-check against Discord's own last message
                                # timestamp so a live thread is never closed just
                                # because our DB write was missed or throttled.
                                if thread.last_message_id:
                                    last_msg_ts = ((int(thread.last_message_id) >> 22) + 1420070400000) // 1000
                                    if now - last_msg_ts < 30 * 60:
                                        if self.matches_col is not None:
                                            await self.matches_col.update_one(
                                                {"thread_id": thread_id},
                                                {"$set": {"last_activity": last_msg_ts}}
                                            )
                                        continue
                                try:
                                    warning_embed = discord.Embed(
                                        title="Thread Closed - Inactivity",
                                        description="This thread has been closed due to 30 minutes of inactivity.",
                                        color=0xE74C3C
                                    )
                                    await thread.send(embed=warning_embed)

                                    for user_id in [int(row["user1_id"]), int(row["user2_id"])]:
                                        try:
                                            member = await guild.fetch_member(user_id)
                                            if member:
                                                await notif_manager.send(
                                                    member,
                                                    "Your match thread was closed due to 30 minutes of inactivity. Feel free to queue again!",
                                                    "inactive"
                                                )
                                        except Exception:
                                            continue

                                    await thread.delete(reason="Inactive for 30 minutes")
                                except Exception:
                                    await thread.delete(reason="Inactive for 30 minutes")

                            # Close the row and clear in-memory state in their own
                            # try blocks. Previously both sat inside the block
                            # above, so any failure while notifying or deleting
                            # left the match "open" forever and the same two
                            # users were re-notified every single minute.
                            try:
                                await self.matches_col.update_one(
                                    {"thread_id": thread_id, "status": "open"},
                                    {"$set": {"status": "closed", "closed_at": now, "close_reason": "inactivity"}}
                                )
                            except Exception:
                                pass
                            # Drops match_meta, the activity throttle and the
                            # thread's control view (now that it is gone).
                            self._forget_thread_state(thread_id)

                        except Exception:
                            continue

                except Exception:
                    continue
                    
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @cleanup_inactive_threads.before_loop
    async def before_cleanup_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=20)
    async def queue_panel_loop(self):
        if not self._initialized:
            return
        try:
            for guild in list(self.bot.guilds):
                try:
                    row = await self._get_queue_panel_row(guild.id)
                    if not row:
                        continue
                    channel = guild.get_channel(int(row["channel_id"]))
                    if not isinstance(channel, discord.TextChannel):
                        continue
                    try:
                        msg = await channel.fetch_message(int(row["message_id"]))
                    except Exception:
                        # Panel message was deleted by hand; stop tracking it
                        # rather than re-fetching it forever every 20 seconds.
                        await self._delete_queue_panel_row(guild.id)
                        continue
                    counts = await self._get_queue_counts(guild.id)
                    embed = self._build_queue_embed(guild, counts)
                    try:
                        await msg.edit(embed=embed)
                    except Exception:
                        pass
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        if not self._initialized:
            return

        try:
            now = int(time.time())

            # --- Scheduled thread deletions ---
            # _on_leave records a row here so a restart does not strand the
            # room. Nothing else acted on these rows, so every thread whose
            # user left stayed alive forever. Delete the thread, close the match
            # row and clear the in-memory state.
            try:
                due = await self.pending_deletions_col.find(
                    {"delete_after": {"$lte": now}}, {"thread_id": 1, "guild_id": 1}
                ).to_list(length=200)
                for row in due:
                    tid = int(row["thread_id"])
                    gid = int(row.get("guild_id") or 0)
                    try:
                        guild = self.bot.get_guild(gid) if gid else None
                        if guild:
                            thread = await self._safe_get_thread(guild, tid)
                            if thread:
                                await thread.delete(reason="User left; scheduled cleanup")
                    except Exception:
                        pass
                    await self._close_match_row(tid)
                    self._forget_thread_state(tid)
                    try:
                        await self.pending_deletions_col.delete_one({"thread_id": tid})
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

            # --- Expire skip-blocks whose window has elapsed ---
            # Without this the blocks table grows without bound and every
            # dequeue scan reads dead rows.
            try:
                await self.recent_blocks_col.delete_many({"blocked_until": {"$lte": now}})
            except Exception:
                pass

            # --- Prune the write-only collections ---
            # match_skips only affects a 24h window, queue_history only
            # powers stats, and both grow forever otherwise.
            try:
                await self.match_skips_col.delete_many(
                    {"skipped_at": {"$lte": now - 2 * 86400}}
                )
            except Exception:
                pass
            try:
                await self.queue_history_col.delete_many(
                    {"matched_at": {"$lte": now - 90 * 86400}}
                )
            except Exception:
                pass

            # Closed match rows older than a week are only kept so a stale
            # view can still be reconciled; drop them once nothing needs them.
            try:
                await self.matches_col.delete_many({
                    "status": "closed",
                    "closed_at": {"$lte": now - 7 * 86400},
                })
            except Exception:
                pass

            # Thread ids already gone from Discord are safe to forget.
            try:
                stale = self._activity_write_ts
                cutoff = now - 86400
                for tid in [t for t, ts in stale.items() if ts < cutoff]:
                    stale.pop(tid, None)
            except Exception:
                pass

            # _locks and _match_backoff are keyed by guild_id and grew for the
            # life of the process, so a bot in many guilds (or one that had been
            # in many) never released them.
            try:
                live = {g.id for g in self.bot.guilds}
                for gid in [gid for gid in self._locks if gid not in live]:
                    lock = self._locks[gid]
                    if not lock.locked():
                        self._locks.pop(gid, None)
                for gid in [gid for gid in self._match_backoff if gid not in live]:
                    self._match_backoff.pop(gid, None)
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @tasks.loop(minutes=5)
    async def dm_cleanup_loop(self):
        if not self._initialized:
            return

        try:
            now = int(time.time())
            # NotificationManager deletes each DM via an asyncio task 60s after
            # sending, but that task dies with the process. These rows record
            # what still needs deleting, so a restart does not leave DMs (and
            # their database records) behind forever.
            rows = await self.dm_messages_col.find({"delete_after": {"$lte": now}}).to_list(length=200)
            for row in rows:
                # Resolve the DM channel via the cache first, then fetch it. A
                # DM channel is not in the cache after a restart, so a bare
                # get_channel left the message undeleted.
                chan = None
                if row.get("channel_id"):
                    chan = self.bot.get_channel(int(row["channel_id"]))
                    if chan is None:
                        try:
                            chan = await self.bot.fetch_channel(int(row["channel_id"]))
                        except Exception:
                            chan = None
                try:
                    if isinstance(chan, discord.DMChannel) and row.get("message_id"):
                        try:
                            await chan.delete_message(int(row["message_id"]))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    # The row goes regardless: the message is either deleted or
                    # unreachable, and keeping it would retry forever.
                    await self.dm_messages_col.delete_one({"message_id": int(row["message_id"])})
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @match_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        if not self.queue_panel_loop.is_running():
            self.queue_panel_loop.start()
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()
        if not self.dm_cleanup_loop.is_running():
            self.dm_cleanup_loop.start()
        if not self.cleanup_inactive_threads.is_running():
            self.cleanup_inactive_threads.start()

async def setup(bot):
    await bot.add_cog(Matchmaker(bot))
