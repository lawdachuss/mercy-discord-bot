# Production-ready sticky cog for Discord.py
# Comprehensive implementation with all edge cases handled

import asyncio
import logging
from collections import defaultdict
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

log = logging.getLogger("sticky")


def mongo_client_closed(client) -> bool:
    """Return True only when we can positively tell the Mongo client is closed.

    Motor's ``AsyncIOMotorClient`` raises AttributeError for any attribute
    starting with ``_`` (its ``__getattr__`` rejects them), so
    ``hasattr(client, '_topology')`` is *always* False. Checking it therefore
    made every "is the client closed?" guard conclude "closed" and silently
    skip the work - which is why stickies were never auto-refreshed. The
    topology actually lives on the pymongo delegate: ``client.delegate``.
    """
    if client is None:
        return True
    try:
        delegate = getattr(client, "delegate", None)
        if delegate is None:
            delegate = client
        topology = getattr(delegate, "_topology", None)
        if topology is None:
            return False  # Can't tell - assume usable, DB errors are handled below
        return bool(getattr(topology, "_closed", False))
    except Exception:
        return False  # Fail open rather than silently skipping work


class StickyMessages(commands.Cog):
    """Manages sticky messages that stay at the bottom of channels."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # MongoDB connection
        self.mongo_client = None
        self.db = None
        self.stickies = None
        
        if hasattr(bot, 'mongo_client') and bot.mongo_client:
            self.mongo_client = bot.mongo_client
            self.db = self.mongo_client.discord_bot
            self.stickies = self.db.stickies
            log.info("Sticky: Using shared MongoDB connection")
        else:
            log.warning("Sticky: No MongoDB connection available")

        # Runtime state tracking
        self.last_sticky_messages: Dict[int, Dict] = {}  # channel_id -> {"message_id": int, "timestamp": datetime}
        self.channel_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # Prevent race conditions
        self.last_repost_time: Dict[int, datetime] = {}  # Rate limiting
        self.processing_channels = set()  # Track channels being processed
        
        # Background task handles
        self._cleanup_task = None
        self._auto_refresh_task = None
        self._memory_cleanup_task = None
        self._tasks_started = False
        self._init_task: Optional[asyncio.Task] = None
        
        # Configuration
        self.repost_cooldown = 2.5  # seconds between reposts in same channel
        self.auto_refresh_interval = 5  # minutes - auto delete and resend stickies
        self.max_content_length = 2000  # Discord limit
        # How far back a safety sweep looks for sticky copies we lost track of.
        self.history_scan_limit = 100

    # ==================== Lifecycle ====================
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize background tasks when bot is ready."""
        await self._init_when_ready()

    async def cog_load(self):
        """Kick off initialization on load.

        on_ready only fires on (re)connect, not when this cog is reloaded
        while the bot stays connected, so start the same guarded init here.
        """
        if self._init_task is None or self._init_task.done():
            self._init_task = asyncio.create_task(self._init_when_ready())

    async def _init_when_ready(self):
        """Restore state and start background tasks exactly once."""
        if self._tasks_started:
            log.debug("Sticky tasks already started, skipping")
            return
            
        self._tasks_started = True
        log.info("Initializing sticky background tasks...")

        # cog_load may run before the bot has connected
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        
        # Wait a moment for bot to fully initialize
        await asyncio.sleep(2)
        
        # Restore state from database
        await self._restore_sticky_state()
        
        # Start background tasks
        if not self._cleanup_task:
            self._cleanup_task = tasks.loop(minutes=5.0)(self._cleanup_loop)
            self._cleanup_task.start()
            log.info("Started cleanup task")

        if not self._auto_refresh_task:
            self._auto_refresh_task = tasks.loop(minutes=self.auto_refresh_interval)(self._auto_refresh_loop)
            self._auto_refresh_task.start()
            log.info(f"Started auto-refresh task ({self.auto_refresh_interval} min interval)")

        if not self._memory_cleanup_task:
            self._memory_cleanup_task = tasks.loop(hours=1.0)(self._memory_cleanup_loop)
            self._memory_cleanup_task.start()
            log.info("Started memory cleanup task")
        
        log.info("✅ Sticky system fully initialized")

    def cog_unload(self):
        """Clean shutdown of all background tasks."""
        log.info("Shutting down sticky system...")
        
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
        
        tasks_to_cancel = [
            ('cleanup', self._cleanup_task),
            ('auto_refresh', self._auto_refresh_task),
            ('memory_cleanup', self._memory_cleanup_task)
        ]
        
        for name, task in tasks_to_cancel:
            if task:
                try:
                    if hasattr(task, 'is_running') and task.is_running():
                        task.cancel()
                        log.info(f"Cancelled {name} task")
                except Exception as e:
                    log.error(f"Error cancelling {name} task: {e}")
        
        # Give tasks a moment to finish cancellation
        try:
            # Create a simple wait for cancellation
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule cleanup but don't block
                asyncio.create_task(asyncio.sleep(0.5))
        except Exception:
            pass
        
        self._tasks_started = False
        log.info("Sticky system shutdown complete")

    # ==================== Utility Methods ====================
    
    def _normalize_content(self, content: Optional[str]) -> str:
        """Normalize and validate content string."""
        if content is None:
            return ""
        content = content.strip()
        if len(content) > self.max_content_length:
            content = content[:self.max_content_length - 3] + "..."
        return content

    async def _check_permissions(self, channel: discord.TextChannel) -> Tuple[bool, str]:
        """
        Check if bot has required permissions.
        Returns (has_permissions, error_message)
        """
        try:
            if not isinstance(channel, discord.TextChannel):
                return False, "Not a text channel"
            
            permissions = channel.permissions_for(channel.guild.me)
            
            if not permissions.send_messages:
                return False, "Missing 'Send Messages' permission"
            if not permissions.read_message_history:
                return False, "Missing 'Read Message History' permission"
            if not permissions.manage_messages:
                return False, "Missing 'Manage Messages' permission (needed to delete old stickies)"
            
            return True, ""
        except Exception as e:
            return False, f"Error checking permissions: {e}"

    def _should_rate_limit(self, channel_id: int) -> bool:
        """Check if we should rate limit reposting in this channel."""
        last_time = self.last_repost_time.get(channel_id)
        if last_time is None:
            return False
        
        time_diff = (datetime.utcnow() - last_time).total_seconds()
        return time_diff < self.repost_cooldown

    async def _sweep_sticky_copies(
        self,
        channel: discord.TextChannel,
        channel_id: int,
        contents: List[str],
        skip_ids: Optional[set] = None,
    ) -> bool:
        """Delete recent copies of the sticky identified by its content.

        This is the safety net for every case where the tracked message id is
        wrong, stale or missing (failed DB write, restart, ``unstick`` error,
        two systems in one channel...). Without it a single lost id turns into
        a permanent duplicate that nothing ever removes.

        Returns True when the channel is verified clean, False when history
        could not be read or a copy could not be deleted.
        """
        contents = [c for c in (self._normalize_content(x) for x in contents) if c]
        if not contents:
            return True

        skip_ids = skip_ids or set()
        try:
            to_delete = []
            async for m in channel.history(limit=self.history_scan_limit):
                if m.id in skip_ids:
                    continue
                if m.author.id != self.bot.user.id:
                    continue
                if self._normalize_content(m.content) not in contents:
                    continue
                to_delete.append(m)

            for m in to_delete:
                try:
                    await m.delete()
                    log.info(f"✅ Swept orphaned sticky {m.id} in channel {channel_id}")
                    await asyncio.sleep(0.2)
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    log.error(f"❌ No permission to delete orphaned sticky {m.id} in channel {channel_id}")
                    return False
                except discord.HTTPException as e:
                    log.error(f"❌ HTTP error deleting orphaned sticky {m.id}: {e}")
                    return False
                except Exception as e:
                    log.exception(f"Unexpected error sweeping sticky {m.id}: {e}")
                    return False
            return True

        except discord.Forbidden:
            log.error(f"❌ Cannot read history in channel {channel_id}; cannot verify old stickies")
            return False
        except Exception as e:
            log.exception(f"Error sweeping stickies in channel {channel_id}: {e}")
            return False

    async def _delete_old_sticky(
        self,
        channel_id: int,
        channel: discord.TextChannel,
        contents: Optional[List[str]] = None,
    ) -> bool:
        """
        Delete every copy of the old sticky in a channel.

        Returns True only when the channel is known to be clean. An unknown
        message id is no longer treated as "nothing to delete": we sweep the
        channel by content instead, because assuming the channel was clean is
        exactly what used to stack duplicates.
        """
        # Candidate ids: what we remember in memory plus what is persisted
        # (memory can be empty after a restart while the sticky is still live).
        candidates: List[int] = []
        sticky_info = self.last_sticky_messages.get(channel_id)
        tracked_id = sticky_info.get("message_id") if isinstance(sticky_info, dict) else sticky_info
        if tracked_id:
            candidates.append(tracked_id)

        try:
            if self.stickies is not None:
                doc = await self.stickies.find_one(
                    {"channel_id": channel_id, "text": {"$exists": False}}
                )
                persisted_id = doc.get("last_message_id") if doc else None
                if persisted_id and persisted_id not in candidates:
                    candidates.append(persisted_id)
        except Exception as e:
            log.warning(f"Could not read persisted sticky state for channel {channel_id}: {e}")
            # Not fatal: the content sweep below does not need the database.

        failed = False
        for msg_id in candidates:
            try:
                log.debug(f"Attempting to delete old sticky {msg_id} in channel {channel_id}")
                old_sticky = await channel.fetch_message(msg_id)
                await old_sticky.delete()
                log.info(f"✅ Deleted old sticky {msg_id} in channel {channel_id}")
            except discord.NotFound:
                log.debug(f"Old sticky {msg_id} already deleted (NotFound)")
            except discord.Forbidden:
                log.error(f"❌ No permission to delete message {msg_id} in channel {channel_id}")
                failed = True
            except discord.HTTPException as e:
                log.error(f"❌ HTTP error deleting sticky {msg_id}: {e}")
                failed = True
            except Exception as e:
                log.exception(f"Unexpected error deleting sticky {msg_id}: {e}")
                failed = True

        # Safety net for copies whose id we never had (or that pointed
        # somewhere else). Keeps the tracked id on failure so the next attempt
        # retries the same message instead of falling back to a stale one.
        if contents and not await self._sweep_sticky_copies(
            channel, channel_id, contents, skip_ids=set(candidates)
        ):
            failed = True

        if not candidates and not contents:
            # Nothing to identify a sticky by (embed-only with no persisted
            # id); there is nothing we can delete, so let the repost through.
            log.warning(f"No id and no content to identify the sticky in channel {channel_id}")
            self.last_sticky_messages.pop(channel_id, None)
            return True

        if not failed:
            self.last_sticky_messages.pop(channel_id, None)
            return True

        return False

    async def _send_sticky_message(
        self, 
        channel: discord.TextChannel, 
        content: Optional[str] = None, 
        embed: Optional[discord.Embed] = None, 
        force_new: bool = False,
        extra_contents: Optional[List[str]] = None
    ) -> Optional[discord.Message]:
        """
        Send a sticky message with proper locking and error handling.
        
        Args:
            channel: The channel to send to
            content: Message content
            embed: Optional embed
            force_new: If True, always delete old and send new
            extra_contents: Previous sticky texts to sweep for as well (used
                when the sticky content is being changed, so the old copy is
                found even if its tracked id was lost)
            
        Returns:
            The sent message or None if failed
        """
        # Validate inputs
        if not isinstance(channel, discord.TextChannel):
            log.warning(f"Invalid channel type: {type(channel)}")
            return None

        normalized_content = self._normalize_content(content)
        if not normalized_content and not embed:
            log.warning(f"Empty content for channel {channel.id}")
            return None

        # Every text we must make sure is gone from the channel before posting.
        contents: List[str] = []
        if normalized_content:
            contents.append(normalized_content)
        for extra in extra_contents or []:
            extra = self._normalize_content(extra)
            if extra and extra not in contents:
                contents.append(extra)

        # Check permissions first
        has_perms, error_msg = await self._check_permissions(channel)
        if not has_perms:
            log.warning(f"Permission issue in {channel.id}: {error_msg}")
            return None

        # Use lock to prevent race conditions
        lock = self.channel_locks[channel.id]
        async with lock:
            try:
                # Delete old sticky if needed
                if force_new or channel.id in self.last_sticky_messages:
                    log.debug(f"Deleting old sticky for channel {channel.id} (force_new={force_new})")
                    deleted = await self._delete_old_sticky(channel.id, channel, contents=contents)
                    if not deleted:
                        # Never send a new copy while the old one is still
                        # there - that is how duplicates pile up.
                        log.warning(
                            f"Failed to delete old sticky in {channel.id}; aborting repost to avoid duplicates"
                        )
                        return None
                    # Small delay to avoid rate limit issues
                    await asyncio.sleep(0.3)
                else:
                    log.debug(f"No old sticky to delete for channel {channel.id}")

                # Prepare message
                send_kwargs = {
                    "allowed_mentions": discord.AllowedMentions.none()
                }
                
                if normalized_content:
                    send_kwargs["content"] = normalized_content
                if embed:
                    send_kwargs["embed"] = embed

                # Send the sticky
                sent = await channel.send(**send_kwargs)
                
                # Update tracking
                now = datetime.utcnow()
                self.last_sticky_messages[channel.id] = {
                    "message_id": sent.id,
                    "timestamp": now
                }
                self.last_repost_time[channel.id] = now
                
                # Update database
                if self.stickies is not None:
                    try:
                        await self.stickies.update_one(
                            {"channel_id": channel.id, "text": {"$exists": False}},
                            {
                                "$set": {
                                    "last_message_id": sent.id,
                                    "last_updated": now
                                }
                            },
                            upsert=False
                        )
                    except Exception as e:
                        log.error(f"Failed to update DB for channel {channel.id}: {e}")

                log.debug(f"Sent sticky {sent.id} in channel {channel.id}")
                return sent
                
            except discord.Forbidden as e:
                log.error(f"Forbidden to send message in {channel.id}: {e}")
                return None
            except discord.HTTPException as e:
                log.error(f"HTTP error sending sticky to {channel.id}: {e}")
                return None
            except Exception as e:
                log.exception(f"Unexpected error sending sticky to {channel.id}: {e}")
                return None

    # ==================== Background Tasks ====================
    
    async def _restore_sticky_state(self):
        """Restore sticky state from database on startup."""
        if self.stickies is None:
            log.warning("Cannot restore state: no database connection")
            return
        
        try:
            log.info("Restoring sticky state from database...")
            restored = 0
            cleaned = 0
            
            async for sticky in self.stickies.find({"text": {"$exists": False}}):
                channel_id = sticky.get("channel_id")
                if not channel_id:
                    continue
                
                last_message_id = sticky.get("last_message_id")
                last_updated = sticky.get("last_updated")
                
                if last_message_id:
                    channel = self.bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        try:
                            # Verify message still exists
                            await channel.fetch_message(last_message_id)
                            
                            # Restore to memory
                            self.last_sticky_messages[channel_id] = {
                                "message_id": last_message_id,
                                "timestamp": last_updated or datetime.utcnow()
                            }
                            restored += 1
                            
                        except discord.NotFound:
                            # Message deleted, clean up DB
                            await self.stickies.update_one(
                                {"_id": sticky["_id"]},
                                {"$unset": {"last_message_id": "", "last_updated": ""}}
                            )
                            cleaned += 1
                        except discord.Forbidden:
                            log.warning(f"No access to channel {channel_id}")
                        except Exception as e:
                            log.error(f"Error verifying message in {channel_id}: {e}")
            
            log.info(f"State restoration complete: {restored} restored, {cleaned} cleaned")
            
        except Exception as e:
            log.exception(f"Error during state restoration: {e}")

    async def _auto_refresh_loop(self):
        """Automatically refresh stickies every 5 minutes (delete old, send new)."""
        if self.stickies is None:
            return
        
        try:
            log.info("Starting auto-refresh cycle...")
            refreshed = 0
            skipped = 0
            failed = 0
            
            # Check if MongoDB client is still open
            if mongo_client_closed(self.mongo_client):
                log.warning("MongoDB client is closed, skipping auto-refresh")
                return
            
            # Only our own stickies (sticky-button docs share this collection)
            async for sticky in self.stickies.find({"text": {"$exists": False}}):
                try:
                    channel_id = sticky.get("channel_id")
                    if not channel_id:
                        continue
                    
                    channel = self.bot.get_channel(channel_id)
                    if not channel or not isinstance(channel, discord.TextChannel):
                        skipped += 1
                        continue
                    
                    # Get content
                    content = sticky.get("content") or ""
                    embed_data = sticky.get("embed")
                    embed = None
                    
                    if not content and not embed_data:
                        skipped += 1
                        continue
                    
                    if embed_data:
                        try:
                            embed = discord.Embed.from_dict(embed_data)
                        except Exception as e:
                            log.error(f"Failed to parse embed for channel {channel_id}: {e}")
                    
                    # Check if enough time has passed
                    sticky_info = self.last_sticky_messages.get(channel_id)
                    if sticky_info and isinstance(sticky_info, dict):
                        last_timestamp = sticky_info.get("timestamp")
                        if last_timestamp:
                            time_diff = datetime.utcnow() - last_timestamp
                            # Only refresh if at least 4.5 minutes have passed
                            # since the last repost (any trigger)
                            if time_diff < timedelta(minutes=4, seconds=30):
                                skipped += 1
                                continue
                    
                    # Refresh the sticky
                    result = await self._send_sticky_message(
                        channel, 
                        content=content, 
                        embed=embed, 
                        force_new=True
                    )
                    
                    if result:
                        refreshed += 1
                        log.debug(f"Refreshed sticky in channel {channel_id}")
                    else:
                        failed += 1
                    
                    # Rate limit protection
                    await asyncio.sleep(1.5)
                    
                except Exception as e:
                    log.exception(f"Error refreshing individual sticky: {e}")
                    failed += 1
            
            log.info(f"Auto-refresh complete: {refreshed} refreshed, {skipped} skipped, {failed} failed")
            
        except Exception as e:
            log.exception(f"Error in auto-refresh loop: {e}")

    async def _cleanup_loop(self):
        """Clean up stale database entries."""
        if self.stickies is None:
            return
        
        try:
            deleted = 0
            
            # Check if MongoDB client is still open
            if mongo_client_closed(self.mongo_client):
                log.warning("MongoDB client is closed, skipping cleanup")
                return
            
            async for sticky in self.stickies.find({"text": {"$exists": False}}):
                chan_id = sticky.get("channel_id")
                
                # Delete entries with no channel ID
                if chan_id is None:
                    try:
                        await self.stickies.delete_one({"_id": sticky["_id"]})
                        deleted += 1
                    except Exception as e:
                        log.error(f"Failed to delete invalid sticky: {e}")
                    continue
                
                # Check if channel still exists
                channel = self.bot.get_channel(chan_id)
                if channel is None:
                    # Only delete if empty content
                    content = sticky.get("content", "").strip()
                    embed = sticky.get("embed")
                    
                    if not content and not embed:
                        try:
                            await self.stickies.delete_one({"_id": sticky["_id"]})
                            deleted += 1
                            
                            # Clean up memory
                            self.last_sticky_messages.pop(chan_id, None)
                            self.last_repost_time.pop(chan_id, None)
                            # NOTE: the channel lock is deliberately kept.
                            # Deleting it while a repost is holding it would
                            # let a second task into the critical section and
                            # post a sticky that nobody tracks.
                                 
                        except Exception as e:
                            log.error(f"Failed to delete empty sticky for {chan_id}: {e}")
            
            if deleted > 0:
                log.info(f"Cleanup: removed {deleted} stale entries")
                
        except Exception as e:
            log.exception(f"Error in cleanup loop: {e}")

    async def _memory_cleanup_loop(self):
        """Clean up memory for inactive channels."""
        if self.stickies is None:
            return
        
        try:
            # Check if MongoDB client is still open
            if mongo_client_closed(self.mongo_client):
                log.warning("MongoDB client is closed, skipping memory cleanup")
                return
            
            # Get active channel IDs from database
            active_channels = set()
            async for sticky in self.stickies.find({}, {"channel_id": 1}):
                chan_id = sticky.get("channel_id")
                if chan_id:
                    active_channels.add(chan_id)
            
            # Clean up memory structures
            cleaned = 0
            
            for chan_id in list(self.last_sticky_messages.keys()):
                if chan_id not in active_channels:
                    self.last_sticky_messages.pop(chan_id, None)
                    self.last_repost_time.pop(chan_id, None)
                    # Locks are kept on purpose - see _cleanup_loop.
                    cleaned += 1
            
            if cleaned > 0:
                log.info(f"Memory cleanup: removed {cleaned} inactive entries")
                
        except Exception as e:
            log.exception(f"Error in memory cleanup: {e}")

    # ==================== Event Listeners ====================
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Repost sticky when a user sends a message."""
        try:
            # Filter out non-user messages
            if message.author.bot:
                return
            if message.webhook_id is not None:
                return
            if message.type != discord.MessageType.default:
                return
            
            # Only text channels
            if not isinstance(message.channel, discord.TextChannel):
                return
            
            # Check database connection
            if self.stickies is None:
                return
            
            channel_id = message.channel.id
            
            # Don't repost if this message is the current sticky
            sticky_info = self.last_sticky_messages.get(channel_id)
            if sticky_info:
                last_msg_id = sticky_info.get("message_id") if isinstance(sticky_info, dict) else sticky_info
                if last_msg_id == message.id:
                    return
            
            # Rate limiting
            if self._should_rate_limit(channel_id):
                log.debug(f"Rate limited repost in channel {channel_id}")
                return
            
            # Check if channel has a sticky (own docs only - sticky-button
            # docs share this collection)
            sticky_doc = await self.stickies.find_one(
                {"channel_id": channel_id, "text": {"$exists": False}}
            )
            if not sticky_doc:
                return
            
            # Get content
            content = sticky_doc.get("content") or ""
            embed_data = sticky_doc.get("embed")
            embed = None
            
            if embed_data:
                try:
                    embed = discord.Embed.from_dict(embed_data)
                except Exception as e:
                    log.error(f"Failed to parse embed: {e}")
            
            # Repost the sticky
            log.debug(f"Reposting sticky for channel {channel_id} (triggered by message from {message.author})")
            result = await self._send_sticky_message(
                message.channel, 
                content=content, 
                embed=embed, 
                force_new=True
            )
            if result:
                log.debug(f"Successfully reposted sticky {result.id} in channel {channel_id}")
            else:
                log.warning(f"Failed to repost sticky in channel {channel_id}")
            
        except Exception as e:
            log.exception(f"Error in on_message handler: {e}")

    # ==================== Commands ====================
    
    @commands.command(name="stick", aliases=["sticky", "sticky_add"])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True, manage_messages=True, read_message_history=True)
    async def stick(self, ctx: commands.Context, *, content: str):
        """
        Set a sticky message for this channel.
        
        Usage: .stick <message>
        Example: .stick Welcome to our server! Please read the rules.
        """
        try:
            if self.stickies is None:
                await ctx.send("❌ Database not available. Cannot save sticky.")
                return
            
            # Validate content
            if len(content) > self.max_content_length:
                await ctx.send(f"❌ Content too long. Maximum {self.max_content_length} characters.")
                return
            
            # Check permissions
            has_perms, error_msg = await self._check_permissions(ctx.channel)
            if not has_perms:
                await ctx.send(f"❌ {error_msg}")
                return
            
            channel_id = ctx.channel.id
            
            # Remember the previous text: if the old copy's tracked message id
            # was lost, content is the only way left to find and remove it.
            old_doc = await self.stickies.find_one(
                {"channel_id": channel_id, "text": {"$exists": False}}
            )
            old_content = (old_doc or {}).get("content")

            # The sticky-button cog keeps its own docs in this collection; a
            # second, independent sticky there cannot be cleaned up by us.
            button_doc = await self.stickies.find_one(
                {"channel_id": channel_id, "guild_id": {"$exists": True}}
            )
            
            # Save to database
            doc = {
                "channel_id": channel_id,
                "content": content,
                "created_by": ctx.author.id,
                "created_at": datetime.utcnow()
            }
            
            await self.stickies.update_one(
                {"channel_id": channel_id, "text": {"$exists": False}}, 
                {"$set": doc}, 
                upsert=True
            )
            
            # Send confirmation
            confirm_msg = await ctx.send("✅ Sticky message saved! Posting now...")
            
            # Post the sticky immediately
            result = await self._send_sticky_message(
                ctx.channel, 
                content=content, 
                force_new=True,
                extra_contents=[old_content] if old_content and old_content != content else None
            )
            
            if result:
                text = "✅ Sticky message is now active!"
                if button_doc:
                    text += ("\n⚠️ This channel also has a `/sticky` (button) sticky — "
                             "the two systems each manage their own message.")
                await confirm_msg.edit(content=text)
            else:
                await confirm_msg.edit(content="⚠️ Sticky saved but failed to post. Check permissions.")
            
            # Delete confirmation after a few seconds
            await asyncio.sleep(5)
            try:
                await confirm_msg.delete()
            except:
                pass
                
        except Exception as e:
            log.exception(f"Error in stick command: {e}")
            await ctx.send("❌ An error occurred. Check logs for details.")

    @commands.command(name="unstick", aliases=["sticky_remove"])
    @commands.has_permissions(manage_messages=True)
    async def unstick(self, ctx: commands.Context):
        """
        Remove the sticky message from this channel.
        
        Usage: .unstick
        """
        try:
            if self.stickies is None:
                await ctx.send("❌ Database not available.")
                return
            
            channel_id = ctx.channel.id
            
            # Read first (own docs only - sticky-button docs share this
            # collection and have a "text" field)
            doc = await self.stickies.find_one(
                {"channel_id": channel_id, "text": {"$exists": False}}
            )
            if not doc:
                await ctx.send("ℹ️ No sticky message is set for this channel.")
                return
            
            content = doc.get("content") or ""
            sticky_info = self.last_sticky_messages.get(channel_id)
            tracked_id = sticky_info.get("message_id") if isinstance(sticky_info, dict) else sticky_info
            if not tracked_id:
                tracked_id = doc.get("last_message_id")
            
            problems = []
            
            # Take the channel lock: running this while a repost is in flight
            # would leave a freshly posted copy behind a doc that no longer
            # exists, and nothing would ever delete that copy again.
            lock = self.channel_locks[channel_id]
            async with lock:
                # Drop the DB entry first so no further repost can start.
                await self.stickies.delete_one({"_id": doc["_id"]})
                self.last_sticky_messages.pop(channel_id, None)
                self.last_repost_time.pop(channel_id, None)
                
                if tracked_id:
                    try:
                        msg = await ctx.channel.fetch_message(tracked_id)
                        await msg.delete()
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        problems.append("missing permission to delete the sticky message")
                    except Exception as e:
                        log.warning(f"unstick: failed to delete {tracked_id} in {channel_id}: {e}")
                        problems.append("a Discord error while deleting the sticky message")
                
                # Sweep anything left over (a previously lost copy, or a repost
                # that slipped through). Without this a failed delete became a
                # permanent duplicate the next time .stick was used.
                if not await self._sweep_sticky_copies(ctx.channel, channel_id, [content]):
                    problems.append("could not verify the channel is clean")
            
            if problems:
                await ctx.send(
                    "ℹ️ Sticky unset, but ⚠️ " + "; ".join(problems) +
                    ". Please check the channel and delete any remaining copy."
                )
            else:
                await ctx.send("✅ Sticky message removed.")
            
        except Exception as e:
            log.exception(f"Error in unstick command: {e}")
            await ctx.send("❌ An error occurred. Check logs for details.")

    @commands.command(name="stickshow", aliases=["sticky_show"])
    async def stickshow(self, ctx: commands.Context):
        """
        Show the current sticky message for this channel.
        
        Usage: .stickshow
        """
        try:
            if self.stickies is None:
                await ctx.send("❌ Database not available.")
                return
            
            doc = await self.stickies.find_one(
                {"channel_id": ctx.channel.id, "text": {"$exists": False}}
            )
            if not doc:
                await ctx.send("ℹ️ No sticky message is set for this channel.")
                return
            
            content = doc.get('content', '')
            created_by = doc.get('created_by')
            created_at = doc.get('created_at')
            
            # Build response
            embed = discord.Embed(
                title="📌 Current Sticky Message",
                description=content[:4000],  # Embed description limit
                color=discord.Color.blue()
            )
            
            if created_by:
                user = ctx.guild.get_member(created_by)
                if user:
                    embed.set_footer(text=f"Created by {user.display_name}")
            
            if created_at:
                embed.timestamp = created_at
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            log.exception(f"Error in stickshow command: {e}")
            await ctx.send("❌ An error occurred. Check logs for details.")

    @commands.command(name="stickrefresh", aliases=["sticky_refresh"])
    @commands.has_permissions(manage_messages=True)
    async def stickrefresh(self, ctx: commands.Context):
        """
        Manually refresh the sticky message in this channel.
        
        Usage: .stickrefresh
        """
        try:
            if self.stickies is None:
                await ctx.send("❌ Database not available.")
                return
            
            sticky_doc = await self.stickies.find_one(
                {"channel_id": ctx.channel.id, "text": {"$exists": False}}
            )
            if not sticky_doc:
                await ctx.send("ℹ️ No sticky message is set for this channel.")
                return
            
            content = sticky_doc.get("content") or ""
            embed_data = sticky_doc.get("embed")
            embed = None
            
            if embed_data:
                try:
                    embed = discord.Embed.from_dict(embed_data)
                except:
                    pass
            
            result = await self._send_sticky_message(
                ctx.channel, 
                content=content, 
                embed=embed, 
                force_new=True
            )
            
            if result:
                confirm = await ctx.send("✅ Sticky message refreshed!")
                await asyncio.sleep(3)
                try:
                    await confirm.delete()
                except:
                    pass
            else:
                await ctx.send("❌ Failed to refresh. Check permissions.")
                
        except Exception as e:
            log.exception(f"Error in stickrefresh command: {e}")
            await ctx.send("❌ An error occurred. Check logs for details.")

    @commands.command(name="sticklist", aliases=["sticky_list"])
    @commands.has_permissions(manage_messages=True)
    async def sticklist(self, ctx: commands.Context):
        """
        List all sticky messages in this server.
        
        Usage: .sticklist
        """
        try:
            if self.stickies is None:
                await ctx.send("❌ Database not available.")
                return
            
            # Get all text channel IDs in this guild
            guild_channels = [c.id for c in ctx.guild.text_channels]
            
            # Find stickies for this guild
            stickies_list = []
            async for sticky in self.stickies.find(
                {"channel_id": {"$in": guild_channels}, "text": {"$exists": False}}
            ):
                channel_id = sticky.get("channel_id")
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    content = sticky.get("content", "")
                    preview = content[:50] + "..." if len(content) > 50 else content
                    stickies_list.append(f"• {channel.mention}: {preview}")
            
            if not stickies_list:
                await ctx.send("ℹ️ No sticky messages are configured in this server.")
                return
            
            # Build embed
            embed = discord.Embed(
                title=f"📌 Sticky Messages in {ctx.guild.name}",
                description="\n".join(stickies_list),
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"Total: {len(stickies_list)} sticky message(s)")
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            log.exception(f"Error in sticklist command: {e}")
            await ctx.send("❌ An error occurred. Check logs for details.")


async def setup(bot: commands.Bot):
    """Add the cog to the bot."""
    await bot.add_cog(StickyMessages(bot))
