# cogs/threads.py

import os
import logging
import time
import asyncio
from datetime import datetime, timezone
from collections import defaultdict

import discord
from discord.ext import commands
from discord import app_commands, Interaction, TextChannel
from discord.app_commands import checks
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

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
        
        # Create indexes for better performance - schedule as async task
        bot.loop.create_task(self._ensure_indexes())

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
        except Exception as e:
            pass

    def check_channel_rate_limit(self, channel_id: str) -> bool:
        """
        Token bucket rate limiter for channel-wide thread creation.
        Prevents hitting Discord's 50 req/sec global limit.
        Returns True if request is allowed, False if rate limited.
        """
        now = time.time()
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

    async def is_on_cooldown(self, guild_id: str, user_id: str, cooldown: int) -> tuple[bool, float]:
        """Check if user is on cooldown. Returns (is_on_cooldown, time_remaining)."""
        now = datetime.now(timezone.utc)
        entry = await self.cooldowns.find_one({"guild_id": guild_id, "user_id": user_id})
        
        if entry:
            last_used = entry.get("last_used")
            # Handle both timezone-aware and naive datetimes from MongoDB
            if last_used:
                if last_used.tzinfo is None:
                    last_used = last_used.replace(tzinfo=timezone.utc)
                
                elapsed = (now - last_used).total_seconds()
                if elapsed < cooldown:
                    return True, cooldown - elapsed
        
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

    def sanitize_thread_name(self, name: str, author_name: str) -> str:
        """Sanitize thread name to prevent Discord API issues."""
        # Remove non-printable characters and trim
        sanitized = ''.join(c for c in name if c.isprintable()).strip()
        
        # Limit to 50 characters
        sanitized = sanitized[:50]
        
        # If empty after sanitization, use author's name
        if not sanitized:
            sanitized = f"Thread by {author_name}"[:50]
        
        return sanitized

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for messages with attachments in configured channels."""
        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return

        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)

        # Check if this channel is configured for thread creation
        config = await self.guild_configs.find_one(
            {"guild_id": guild_id, "channel_id": channel_id}
        )
        
        # If not configured or no attachments, skip
        if not config or not message.attachments:
            # CRITICAL: Process commands at the end
            await self.bot.process_commands(message)
            return

        # ESSENTIAL: Check channel-wide rate limit first (prevents API spam)
        if not self.check_channel_rate_limit(channel_id):
            # Silently skip - don't spam users with messages
            await self.bot.process_commands(message)
            return

        # Check per-user cooldown
        cooldown = config.get("cooldown", 30)
        on_cd, remaining = await self.is_on_cooldown(guild_id, user_id, cooldown)
        
        if on_cd:
            try:
                await message.channel.send(
                    f"⏳ {message.author.mention}, cooldown active. Try again in **{remaining:.0f}s**.",
                    delete_after=5
                )
            except discord.Forbidden:
                pass
            
            await self.bot.process_commands(message)
            return

        # Sanitize thread name
        thread_name = self.sanitize_thread_name(
            message.content,
            message.author.display_name
        )
        
        # Get archive duration from config
        archive_duration = config.get("archive_duration", 1440)

        # ESSENTIAL: Retry logic with exponential backoff
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Create the thread
                thread = await message.create_thread(
                    name=thread_name,
                    auto_archive_duration=archive_duration
                )
                
                # Success! Update cooldown and record stats
                await self.update_cooldown(guild_id, user_id)
                await self.record_stats(guild_id, channel_id, user_id)
                
                # Send confirmation message in thread
                try:
                    await thread.send(
                        f"📎 Thread created by {message.author.mention}"
                    )
                except discord.Forbidden:
                    pass
                
                # ESSENTIAL: Structured logging with context
                break  # Success - exit retry loop
                
            except discord.errors.RateLimited as e:
                # Discord rate limited us - wait and retry
                wait_time = e.retry_after
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    # Max retries reached
                    try:
                        await message.channel.send(
                            "⚠️ Server is busy. Please try again in a moment.",
                            delete_after=5
                        )
                    except discord.Forbidden:
                        pass
                    logger.error(
                        f"Failed to create thread after {max_retries} attempts due to rate limiting: "
                        f"guild={guild_id} channel={channel_id} user={user_id}"
                    )
                    
            except discord.Forbidden:
                # Missing permissions
                try:
                    await message.channel.send(
                        "❌ I don't have permission to create threads here.",
                        delete_after=5
                    )
                except discord.Forbidden:
                    pass
                break  # Don't retry on permission errors
                
            except discord.HTTPException as e:
                # Generic HTTP error
                # Check if it's an "Unknown Message" error (10008) - message was deleted
                if e.code == 10008:
                    logger.warning(
                        f"Message was deleted before thread could be created: "
                        f"guild={guild_id} channel={channel_id} user={user_id}"
                    )
                    break  # Don't retry if message no longer exists
                
                if attempt < max_retries - 1:
                    # Retry with exponential backoff
                    wait_time = retry_delay * (2 ** attempt)
                    await asyncio.sleep(wait_time)
                else:
                    # Max retries reached
                    try:
                        await message.channel.send(
                            "⚠️ Failed to create thread. Please try again later.",
                            delete_after=5
                        )
                    except discord.Forbidden:
                        pass
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
                try:
                    await message.channel.send(
                        "❌ An unexpected error occurred.",
                        delete_after=5
                    )
                except discord.Forbidden:
                    pass
                break  # Don't retry on unexpected errors
        
        # CRITICAL: Process commands at the END - not at the beginning
        await self.bot.process_commands(message)

    @app_commands.command(
        name="thread_channel",
        description="Toggle thread-creation on a channel (add/remove)."
    )
    @app_commands.describe(
        channel="The channel to configure for automatic thread creation",
        cooldown="Cooldown in seconds between thread creations per user (default: 30)",
        archive_duration="Auto-archive duration: 60 (1h), 1440 (1d), 4320 (3d), 10080 (1w)"
    )
    @app_commands.choices(archive_duration=[
        app_commands.Choice(name="1 hour", value=60),
        app_commands.Choice(name="24 hours (1 day)", value=1440),
        app_commands.Choice(name="3 days (requires boost)", value=4320),
        app_commands.Choice(name="7 days (requires boost)", value=10080),
    ])
    @checks.has_permissions(administrator=True)
    async def configure_channel(
        self,
        interaction: Interaction,
        channel: TextChannel,
        cooldown: int = 30,
        archive_duration: int = 1440
    ):
        """Configure a channel for automatic thread creation on image uploads."""
        # Validate cooldown
        if cooldown < 0 or cooldown > 3600:
            return await interaction.response.send_message(
                "❌ Cooldown must be between 0 and 3600 seconds (1 hour).",
                ephemeral=True
            )
        
        guild_id = str(interaction.guild_id)
        channel_id = str(channel.id)

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
                return await interaction.response.send_message(
                    f"🗑️ Thread creation **disabled** in {channel.mention}.",
                    ephemeral=True
                )
            except Exception as e:
                logger.error(f"Error removing channel config: {e}", exc_info=True)
                return await interaction.response.send_message(
                    "❌ An error occurred while removing the configuration.",
                    ephemeral=True
                )

        # Validate bot permissions
        perms = channel.permissions_for(interaction.guild.me)
        
        if not perms.create_public_threads:
            return await interaction.response.send_message(
                f"❌ I need **Create Public Threads** permission in {channel.mention} first.",
                ephemeral=True
            )
        
        if not perms.send_messages_in_threads:
            return await interaction.response.send_message(
                f"❌ I need **Send Messages in Threads** permission in {channel.mention} first.",
                ephemeral=True
            )
        
        if not perms.view_channel:
            return await interaction.response.send_message(
                f"❌ I need **View Channel** permission in {channel.mention} first.",
                ephemeral=True
            )

        # Add new configuration
        try:
            await self.guild_configs.update_one(
                {"guild_id": guild_id, "channel_id": channel_id},
                {"$set": {
                    "cooldown": cooldown,
                    "archive_duration": archive_duration
                }},
                upsert=True
            )
            
            # Format archive duration for display
            duration_text = {
                60: "1 hour",
                1440: "24 hours (1 day)",
                4320: "3 days",
                10080: "7 days"
            }.get(archive_duration, f"{archive_duration} minutes")
            
            await interaction.response.send_message(
                f"✅ Thread creation **enabled** in {channel.mention}\n"
                f"⏱️ Cooldown: **{cooldown}s** per user\n"
                f"📦 Auto-archive: **{duration_text}** of inactivity\n"
                f"💡 Users can now upload images to automatically create threads!",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error adding channel config: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ An error occurred while saving the configuration.",
                ephemeral=True
            )

    @app_commands.command(
        name="thread_status",
        description="View all channels configured for automatic thread creation."
    )
    @checks.has_permissions(administrator=True)
    async def thread_status(self, interaction: Interaction):
        """Display thread configuration status for the server."""
        guild_id = str(interaction.guild_id)
        
        try:
            configs = await self.guild_configs.find({"guild_id": guild_id}).to_list(length=None)
        except Exception as e:
            logger.error(f"Error fetching configs: {e}", exc_info=True)
            return await interaction.response.send_message(
                "❌ An error occurred while fetching configurations.",
                ephemeral=True
            )

        if not configs:
            return await interaction.response.send_message(
                "❌ No channels configured for automatic thread creation.\n"
                "💡 Use `/thread_channel` to configure a channel.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="📊 Thread Configuration",
            description=f"Configured channels in **{interaction.guild.name}**",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        valid_configs = 0
        for cfg in configs:
            chan = interaction.guild.get_channel(int(cfg["channel_id"]))
            if chan:
                archive_duration = cfg.get("archive_duration", 1440)
                duration_text = {
                    60: "1h",
                    1440: "1d",
                    4320: "3d",
                    10080: "7d"
                }.get(archive_duration, f"{archive_duration}m")
                
                embed.add_field(
                    name=f"#{chan.name}",
                    value=f"⏱️ Cooldown: **{cfg.get('cooldown', 30)}s**\n"
                          f"📦 Archive: **{duration_text}**\n"
                          f"🆔 ID: `{chan.id}`",
                    inline=False
                )
                valid_configs += 1
            else:
                # Channel was deleted, clean up config
                try:
                    await self.guild_configs.delete_one(
                        {"guild_id": guild_id, "channel_id": cfg["channel_id"]}
                    )
                except Exception as e:
                    logger.error(f"Error cleaning up config: {e}", exc_info=True)

        if valid_configs == 0:
            return await interaction.response.send_message(
                "❌ All configured channels have been deleted.\n"
                "💡 Use `/thread_channel` to configure a new channel.",
                ephemeral=True
            )

        embed.set_footer(text=f"Total: {valid_configs} channel(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="thread_stats",
        description="View thread creation statistics for this server."
    )
    @checks.has_permissions(administrator=True)
    async def thread_stats(self, interaction: Interaction):
        """Display thread creation statistics."""
        guild_id = str(interaction.guild_id)
        
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
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error fetching stats: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ An error occurred while fetching statistics.",
                ephemeral=True
            )

    @configure_channel.error
    async def configure_channel_error(self, interaction: Interaction, error):
        """Handle errors for the configure_channel command."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Administrator** permission to use this command.",
                ephemeral=True
            )
        else:
            logger.error(f"Error in configure_channel: {error}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True
                )

    @thread_status.error
    async def thread_status_error(self, interaction: Interaction, error):
        """Handle errors for the thread_status command."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Administrator** permission to use this command.",
                ephemeral=True
            )
        else:
            logger.error(f"Error in thread_status: {error}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True
                )

    @thread_stats.error
    async def thread_stats_error(self, interaction: Interaction, error):
        """Handle errors for the thread_stats command."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Administrator** permission to use this command.",
                ephemeral=True
            )
        else:
            logger.error(f"Error in thread_stats: {error}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True
                )

async def setup(bot: commands.Bot):
    await bot.add_cog(ThreadCreatorCog(bot))
