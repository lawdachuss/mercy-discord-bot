import os
import time
import random
import json
import io
import asyncio
import math
from collections import defaultdict
from typing import Optional, Tuple, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

EMBED_COLOR = 0x2F3136
SKIP_BLOCK_MINUTES = 1440  # 24 hours
MAX_DB_CANDIDATES = 2000
SCAN_WINDOW = 200

# ----- Safe Reply -----

async def safe_reply(interaction: discord.Interaction, content: Optional[str] = None, embed: Optional[discord.Embed] = None, ephemeral: bool = True):
    try:
        if not interaction.response.is_done():
            if embed:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
        else:
            if embed:
                await interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.followup.send(content, ephemeral=ephemeral)
    except Exception:
        try:
            if interaction.channel:
                if embed:
                    await interaction.channel.send(f"{interaction.user.mention}", embed=embed, delete_after=15 if ephemeral else None)
                else:
                    await interaction.channel.send(f"{interaction.user.mention} {content}", delete_after=15 if ephemeral else None)
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
                        {"": {"message_id": int(msg.id), "channel_id": int(msg.channel.id), "user_id": int(user.id), "delete_after": delete_after}},
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
            asyncio.create_task(_delete_later())
        except Exception:
            pass

notif_manager = NotificationManager()

# ----- Member Roles Cache (to minimize fetch_member in big servers) -----

class MemberRoleCache:
    def __init__(self, max_size: int = 5000, ttl_seconds: int = 300):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[int, Tuple[int, set[int]]] = {}
        self._order: List[int] = []
        self._lock = asyncio.Lock()

    async def get_roles(self, guild: discord.Guild, user_id: int) -> Optional[set[int]]:
        now = int(time.time())
        async with self._lock:
            item = self._cache.get(user_id)
            if item and now - item[0] <= self.ttl_seconds:
                try:
                    self._order.remove(user_id)
                except ValueError:
                    pass
                self._order.append(user_id)
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
            self._cache[user_id] = (now, set(roles))
            self._order.append(user_id)
            if len(self._order) > self.max_size:
                try:
                    oldest = self._order.pop(0)
                    self._cache.pop(oldest, None)
                except Exception:
                    pass
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
            
        except Exception as e:
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
            await self.cog.enqueue(self.guild_id, interaction.user.id)

            await notif_manager.send(
                interaction.user,
                "Skipped your current match. You've been re-queued for a new match.",
                "skip"
            )

            try:
                other_member = interaction.guild.get_member(other_id)
                if other_member:
                    await notif_manager.send(other_member, "Your partner skipped. You'll stay in the room or you can leave and re-queue.", "skip_notice")
            except Exception:
                pass

            embed = discord.Embed(title="You skipped. Re-queued for a new match.", color=EMBED_COLOR)
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
                        self.cog.match_meta.pop(self.thread_id, None)
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
                self.cog.match_meta.pop(self.thread_id, None)
                try:
                    await self.cog.pending_deletions_col.delete_one({"thread_id": self.thread_id})
                except Exception:
                    pass

            asyncio.create_task(_delete_later())
            
            try:
                await self.cog.pending_deletions_col.update_one(
                    {"thread_id": self.thread_id},
                    {"": {"thread_id": self.thread_id, "guild_id": self.guild_id, "delete_after": int(time.time()) + 120}},
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
            if not isinstance(report_ch, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
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
            except Exception as e:
                pass

            try:
                if transcript_file:
                    await report_ch.send(embed=embed, file=transcript_file)
                else:
                    await report_ch.send(embed=embed)
            except Exception as e:
                try:
                    await interaction.edit_original_response(content="⚠️ Error sending report to staff. Please contact a moderator.")
                except discord.errors.NotFound:
                    pass
                return

            try:
                await interaction.edit_original_response(content="✅ Report submitted successfully. Thank you for helping keep the community safe.")
            except discord.errors.NotFound:
                pass

        except Exception as e:
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
        try:
            if self.match_loop.is_running():
                self.match_loop.cancel()
        except Exception:
            pass
        try:
            if self.queue_panel_loop.is_running():
                self.queue_panel_loop.cancel()
        except Exception:
            pass
        try:
            if self.cleanup_loop.is_running():
                self.cleanup_loop.cancel()
        except Exception:
            pass
        try:
            if self.dm_cleanup_loop.is_running():
                self.dm_cleanup_loop.cancel()
        except Exception:
            pass

    # ----- Helpers for thread actions -----
    def _get_thread_meta(self, thread_id: int):
        return self.match_meta.get(thread_id)

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
                {"": {"closed_at": int(time.time()), "status": "closed"}}
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            if self._on_ready_done:
                return

            await self.waiting_queue_col.create_index([("guild_id", 1), ("enqueued_at", 1)])
            await self.waiting_queue_col.create_index([("guild_id", 1), ("priority_score", -1)])
            await self.matches_col.create_index([("guild_id", 1), ("status", 1), ("last_activity", 1)])
            await self.recent_blocks_col.create_index([("guild_id", 1), ("blocked_until", 1)])
            await self.match_skips_col.create_index([("guild_id", 1), ("skipped_at", 1)])
            await self.pending_deletions_col.create_index([("delete_after", 1)])
            await self.dm_messages_col.create_index([("delete_after", 1)])

            self.bot.add_view(MatchPanel(self))
            try:
                open_rows = await self.matches_col.find({"status": "open"}, {"thread_id": 1, "guild_id": 1}).to_list(length=None)
                for r in open_rows or []:
                    tid = int(r["thread_id"])
                    gid = int(r["guild_id"])
                    guild = self.bot.get_guild(gid)
                    if guild:
                        th = await self._safe_get_thread(guild, tid)
                        if th:
                            self.bot.add_view(ThreadControls(self, gid, tid))
            except Exception:
                pass

            self._initialized = True
            if not self.match_loop.is_running():
                self.match_loop.start()
            self._on_ready_done = True
        except Exception:
            raise

    # ----- Queue Helpers -----

    async def calculate_priority(self, guild_id: int, user_id: int) -> int:
        doc = await self.waiting_queue_col.find_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"enqueued_at": 1, "boost_until": 1}
        )
        if not doc:
            return 0
        wait = int(time.time()) - int(doc["enqueued_at"])
        score = wait // 60
        return int(score)

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
        except Exception as e:
            pass
            
        for attempt in range(max_retries):
            try:
                wait = 0
                score = wait // 60
                await self.waiting_queue_col.update_one(
                    {"guild_id": guild_id, "user_id": user_id},
                    {"": {
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
        cursor = self.waiting_queue_col.find(
            {"guild_id": guild_id},
            {"user_id": 1, "enqueued_at": 1, "priority_score": 1}
        )
        rows = await cursor.to_list(length=None)
        if not rows:
            return (0, 0, 0)
        cands: List[Dict[str, Any]] = [dict(r) for r in rows]
        cands.sort(key=lambda x: (-int(x["priority_score"]), int(x["enqueued_at"])))
        total = len(cands)
        position = next((i + 1 for i, r in enumerate(cands) if int(r["user_id"]) == int(user_id)), 0)
        if position == 0:
            return (0, total, 0)
        ahead = max(0, position - 1)
        pair_slots = max(1, math.ceil(ahead / 2))
        eta_seconds = pair_slots * 20
        return (position, total, eta_seconds)

    async def update_priority(self, guild_id: int, user_id: int):
        score = await self.calculate_priority(guild_id, user_id)
        await self.waiting_queue_col.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"": {"priority_score": score}}
        )

    async def dequeue_pair(self, guild: discord.Guild):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                cursor = self.waiting_queue_col.find(
                    {"guild_id": guild.id},
                    {"user_id": 1, "enqueued_at": 1, "boost_until": 1}
                ).sort("enqueued_at", 1).limit(MAX_DB_CANDIDATES)
                candidates = await cursor.to_list(length=MAX_DB_CANDIDATES)
                if len(candidates) < 2:
                    return None

                cands: List[Dict[str, Any]] = [dict(r) for r in candidates]
                now_ts = int(time.time())
                pipeline = [
                    {"": {"guild_id": guild.id, "skipped_at": {"": now_ts - 300}}},
                    {"": {"_id": "", "last_skip": {"": ""}}}
                ]
                skip_cursor = self.match_skips_col.aggregate(pipeline)
                skips = await skip_cursor.to_list(length=None)
                
                recent_skips = {int(r["_id"]): int(r["last_skip"]) for r in skips}
                
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
                await self.recent_blocks_col.delete_many({"guild_id": guild.id, "blocked_until": {"": now}})
                blk_cursor = self.recent_blocks_col.find(
                    {"guild_id": guild.id, "blocked_until": {"": now}},
                    {"user1_id": 1, "user2_id": 1}
                )
                blk = await blk_cursor.to_list(length=None)
                blocks = {(int(r["user1_id"]), int(r["user2_id"])) for r in blk}
                
                day_ago = now - (24 * 60 * 60)
                skip_threads = await self.match_skips_col.distinct("thread_id", {"guild_id": guild.id, "skipped_at": {"": day_ago}})
                matched_cursor = self.matches_col.find(
                    {
                        "guild_id": guild.id,
                        "created_at": {"": day_ago},
                        "status": "open",
                        "thread_id": {"": skip_threads}
                    },
                    {"user1_id": 1, "user2_id": 1}
                )
                matched = await matched_cursor.to_list(length=None)
                recently_matched = set()
                for match in matched:
                    recently_matched.add(int(match["user1_id"]))
                    recently_matched.add(int(match["user2_id"]))

                async def get_roles(uid: int) -> Optional[set[int]]:
                    return await self._roles_cache.get_roles(guild, int(uid))

                def ok(pref: str, roles: Optional[set[int]]) -> bool:
                        return roles is not None

                for i, u1 in enumerate(cands[:SCAN_WINDOW]):
                    for u2 in cands[i + 1:i + 1 + SCAN_WINDOW]:
                        pair_sorted = tuple(sorted((int(u1["user_id"]), int(u2["user_id"])) ))
                        if pair_sorted in blocks:
                            continue
                        if int(u1["user_id"]) in recently_matched or int(u2["user_id"]) in recently_matched:
                            continue
                        r1 = await get_roles(int(u1["user_id"]))
                        r2 = await get_roles(int(u2["user_id"]))
                        if not r1 or not r2:
                            continue
                        if ok("random", r2) and ok("random", r1):
                            return (u1, u2)
                return None
            except Exception:
                if attempt == max_retries - 1:
                    return None
                await asyncio.sleep(0.1 * (attempt + 1))

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
                    {"": {"guild_id": guild_id, "user1_id": u1, "user2_id": u2, "blocked_until": until}},
                    upsert=True
                )
                return
            except Exception:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))

    # ----- Matching Loop -----

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
            asyncio.create_task(self._attempt_match(guild))

    @tasks.loop(minutes=1)
    async def cleanup_inactive_threads(self):
        if not self._initialized:
            return
            
        try:
            now = int(time.time())
            inactive_threshold = now - (30 * 60)
            
            for guild in self.bot.guilds:
                try:
                    pending_thread_ids = await self.pending_deletions_col.distinct("thread_id")
                    cursor = self.matches_col.find({
                        "status": "open",
                        "last_activity": {"": inactive_threshold, "": inactive_threshold - 60},
                        "thread_id": {"": pending_thread_ids}
                    })
                    rows = await cursor.to_list(length=None)
                    
                    for row in rows:
                        thread_id = int(row["thread_id"])
                        try:
                            thread = await self._safe_get_thread(guild, thread_id)
                            if thread:
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
                            
                            await self.matches_col.update_one(
                                {"thread_id": thread_id},
                                {"": {"status": "closed", "closed_at": now, "close_reason": "inactivity"}}
                            )
                            
                            if thread_id in self.match_meta:
                                self.match_meta.pop(thread_id, None)
                            
                        except Exception as e:
                            continue
                    
                except Exception as e:
                    continue
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pass

    @cleanup_inactive_threads.before_loop
    async def before_cleanup_loop(self):
        await self.bot.wait_until_ready()

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
