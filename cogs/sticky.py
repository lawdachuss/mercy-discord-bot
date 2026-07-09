# Production-ready sticky cog for Discord.py
# Comprehensive implementation with all edge cases handled

import asyncio
import logging
from collections import defaultdict
from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

log = logging.getLogger("sticky")


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
        
        # Configuration
        self.repost_cooldown = 2.5  # seconds between reposts in same channel
        self.auto_refresh_interval = 30  # minutes - auto delete and resend stickies
        self.max_content_length = 2000  # Discord limit

    # ==================== Lifecycle ====================
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize background tasks when bot is ready."""
        if self._tasks_started:
            log.debug("Sticky tasks already started, skipping")
            return
            
        self._tasks_started = True
        log.info("Initializing sticky background tasks...")
        
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

    async def _delete_old_sticky(self, channel_id: int, channel: discord.TextChannel) -> bool:
        """
        Delete the old sticky message for a channel.
        Returns True if successful or message didn't exist.
        """
        sticky_info = self.last_sticky_messages.get(channel_id)
        if not sticky_info:
            log.debug(f"No sticky info found for channel {channel_id}")
            return True
        
        last_msg_id = sticky_info.get("message_id") if isinstance(sticky_info, dict) else sticky_info
        if not last_msg_id:
            log.debug(f"No message ID in sticky info for channel {channel_id}")
            return True
        
        try:
            log.debug(f"Attempting to delete old sticky {last_msg_id} in channel {channel_id}")
            old_sticky = await channel.fetch_message(last_msg_id)
            await old_sticky.delete()
            log.info(f"✅ Deleted old sticky {last_msg_id} in channel {channel_id}")
            # Clear from memory after successful deletion
            self.last_sticky_messages.pop(channel_id, None)
            return True
            
        except discord.NotFound:
            # Message already deleted, that's fine - clear from memory
            log.debug(f"Old sticky {last_msg_id} already deleted (NotFound)")
            self.last_sticky_messages.pop(channel_id, None)
            return True
        except discord.Forbidden:
            log.error(f"❌ No permission to delete message {last_msg_id} in channel {channel_id}")
            # Clear from memory anyway to avoid repeated failures
            self.last_sticky_messages.pop(channel_id, None)
            return False
        except discord.HTTPException as e:
            log.error(f"❌ HTTP error deleting sticky {last_msg_id}: {e}")
            # Clear from memory to avoid repeated failures
            self.last_sticky_messages.pop(channel_id, None)
            return False
        except Exception as e:
            log.exception(f"Unexpected error deleting sticky: {e}")
            # Clear from memory to avoid repeated failures
            self.last_sticky_messages.pop(channel_id, None)
            return False

    async def _send_sticky_message(
        self, 
        channel: discord.TextChannel, 
        content: Optional[str] = None, 
        embed: Optional[discord.Embed] = None, 
        force_new: bool = False
    ) -> Optional[discord.Message]:
        """
        Send a sticky message with proper locking and error handling.
        
        Args:
            channel: The channel to send to
            content: Message content
            embed: Optional embed
            force_new: If True, always delete old and send new
            
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
                    deleted = await self._delete_old_sticky(channel.id, channel)
                    if not deleted:
                        log.warning(f"Failed to delete old sticky in {channel.id}, continuing anyway")
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
                            {"channel_id": channel.id},
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
            
            async for sticky in self.stickies.find({}):
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
                                {"channel_id": channel_id},
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
        """Automatically refresh stickies every 10 minutes."""
        if self.stickies is None:
            return
        
        try:
            log.info("Starting auto-refresh cycle...")
            refreshed = 0
            skipped = 0
            failed = 0
            
            # Check if MongoDB client is still open
            if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                log.warning("MongoDB client is closed, skipping auto-refresh")
                return
            
            async for sticky in self.stickies.find({}):
                try:
                    channel_id = sticky.get("channel_id")
                    if not channel_id:
                        continue
                    
                    channel = self.bot.get_channel(channel_id)
                    if not channel or not isinstance(channel, discord.TextChannel):
                        skipped += 1
                        continue
                    
                    # Check if enough time has passed
                    sticky_info = self.last_sticky_messages.get(channel_id)
                    if sticky_info and isinstance(sticky_info, dict):
                        last_timestamp = sticky_info.get("timestamp")
                        if last_timestamp:
                            time_diff = datetime.utcnow() - last_timestamp
                            # Only refresh if at least 29.5 minutes have passed
                            if time_diff < timedelta(minutes=29, seconds=30):
                                skipped += 1
                                continue
                    
                    # Get content
                    content = sticky.get("content") or ""
                    embed_data = sticky.get("embed")
                    embed = None
                    
                    if embed_data:
                        try:
                            embed = discord.Embed.from_dict(embed_data)
                        except Exception as e:
                            log.error(f"Failed to parse embed for channel {channel_id}: {e}")
                    
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
            if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                log.warning("MongoDB client is closed, skipping cleanup")
                return
            
            async for sticky in self.stickies.find({}):
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
                            if chan_id in self.channel_locks:
                                del self.channel_locks[chan_id]
                                
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
            if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
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
                    if chan_id in self.channel_locks:
                        del self.channel_locks[chan_id]
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
            
            # Check if channel has a sticky
            sticky_doc = await self.stickies.find_one({"channel_id": channel_id})
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
            
            # Save to database
            doc = {
                "channel_id": channel_id,
                "content": content,
                "created_by": ctx.author.id,
                "created_at": datetime.utcnow()
            }
            
            await self.stickies.update_one(
                {"channel_id": channel_id}, 
                {"$set": doc}, 
                upsert=True
            )
            
            # Send confirmation
            confirm_msg = await ctx.send("✅ Sticky message saved! Posting now...")
            
            # Post the sticky immediately
            result = await self._send_sticky_message(
                ctx.channel, 
                content=content, 
                force_new=True
            )
            
            if result:
                await confirm_msg.edit(content="✅ Sticky message is now active!")
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
            
            # Delete from database
            result = await self.stickies.delete_one({"channel_id": channel_id})
            
            if result.deleted_count == 0:
                await ctx.send("ℹ️ No sticky message is set for this channel.")
                return
            
            # Delete the sticky message
            sticky_info = self.last_sticky_messages.pop(channel_id, None)
            if sticky_info:
                last_msg_id = sticky_info.get("message_id") if isinstance(sticky_info, dict) else sticky_info
                if last_msg_id:
                    try:
                        msg = await ctx.channel.fetch_message(last_msg_id)
                        await msg.delete()
                    except:
                        pass
            
            # Clean up memory
            self.last_repost_time.pop(channel_id, None)
            if channel_id in self.channel_locks:
                del self.channel_locks[channel_id]
            
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
            
            doc = await self.stickies.find_one({"channel_id": ctx.channel.id})
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
            
            sticky_doc = await self.stickies.find_one({"channel_id": ctx.channel.id})
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
            async for sticky in self.stickies.find({"channel_id": {"$in": guild_channels}}):
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
