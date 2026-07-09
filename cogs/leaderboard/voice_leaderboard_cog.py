"""
Voice Leaderboard Cog - Tracks voice time with live leaderboards matching JSON template exactly
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
import os
from typing import Optional, List, Dict
import pytz
from dotenv import load_dotenv
import logging
import asyncio
from .leaderboard_config import (
    EMBED_COLOR, Emojis, Images, VoiceTemplates, 
    PeriodConfig, ButtonConfig, LeaderboardSettings
)
from .validators import DataValidator
from .utils import SafePaginator, SafeInteractionHandler, UserDisplayFormatter, ResetTimeCalculator
from .data_validator import LeaderboardDataValidator
from .safe_data import SafeLeaderboardData
from .safe_formatter import SafeFormatter

load_dotenv()


class VoiceLeaderboardPaginator(discord.ui.View):
    def __init__(self, cog, guild_id: int, period: str, page: int = 0, vibe_channel_id: int = None):
        self.cog = cog
        self.guild_id = guild_id
        self.period = period
        self.page = page
        self.vibe_channel_id = vibe_channel_id
        self.max_pages_cache = None  # Cache max pages
        
        # Initialize the view with no timeout for persistent buttons
        super().__init__(timeout=None)  # Persistent buttons that don't expire
        
        # Clear default buttons (they're added by decorators)
        self.clear_items()
        
        # Only add pagination buttons for daily period
        if period == 'daily':
            left_emoji = discord.PartialEmoji(name=Emojis.LEFT_BUTTON_NAME, id=Emojis.LEFT_BUTTON_ID)
            right_emoji = discord.PartialEmoji(name=Emojis.RIGHT_BUTTON_NAME, id=Emojis.RIGHT_BUTTON_ID)
            
            # FIX #12: Validate custom_id length (Discord limit is 100 characters)
            left_custom_id = f"{ButtonConfig.VOICE_LEFT_PREFIX}_{period}_{guild_id}"
            right_custom_id = f"{ButtonConfig.VOICE_RIGHT_PREFIX}_{period}_{guild_id}"
            
            if len(left_custom_id) > 100 or len(right_custom_id) > 100:
                # Fallback to shorter IDs
                left_custom_id = f"vl_{guild_id}"[:100]
                right_custom_id = f"vr_{guild_id}"[:100]
            
            # Create pagination buttons
            left_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=left_emoji,
                custom_id=left_custom_id
            )
            left_button.callback = self.previous_page
            
            right_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=right_emoji,
                custom_id=right_custom_id
            )
            right_button.callback = self.next_page
            
            self.add_item(left_button)
            self.add_item(right_button)
        
        # Add Join the Vibe button for monthly and weekly only
        if period in ['monthly', 'weekly'] and vibe_channel_id:
            vibe_emoji = discord.PartialEmoji(name='original_Peek', id=1429151221939441776, animated=True)
            vibe_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Join the Vibe",
                emoji=vibe_emoji,
                url=f"https://discord.com/channels/{guild_id}/{vibe_channel_id}"
            )
            self.add_item(vibe_button)
    
    async def previous_page(self, interaction: discord.Interaction):
        try:
            if self.page > 0:
                self.page -= 1
                # Update the embed for this period
                new_embed = await self.cog._build_period_embed(self.guild_id, self.period, page=self.page)
                success = await SafeInteractionHandler.safe_edit(interaction, embed=new_embed, view=self)
                if not success:
                    self.cog.logger.warning("Failed to edit message for previous_page")
            else:
                await SafeInteractionHandler.safe_respond(interaction, content="You're on the first page!", ephemeral=True)
        except Exception as e:
            self.cog.logger.error(f"Error in previous_page (voice): {e}", exc_info=True)
            await SafeInteractionHandler.send_error_message(interaction, "An error occurred while updating the leaderboard.")
    
    async def next_page(self, interaction: discord.Interaction):
        try:
            # Use cached max_pages if available
            if self.max_pages_cache is None:
                stats = await self.cog._get_top_users(self.guild_id, self.period, limit=LeaderboardSettings.MAX_MEMBERS_FETCH)
                if not stats:
                    await SafeInteractionHandler.safe_respond(interaction, content="No data available!", ephemeral=True)
                    return
                # Use SafePaginator to calculate max pages safely
                paginator = SafePaginator(stats, LeaderboardSettings.MEMBERS_PER_PAGE)
                self.max_pages_cache = paginator.get_max_pages()
            
            if self.page < self.max_pages_cache:
                self.page += 1
                # Update the embed for this period
                new_embed = await self.cog._build_period_embed(self.guild_id, self.period, page=self.page)
                success = await SafeInteractionHandler.safe_edit(interaction, embed=new_embed, view=self)
                if not success:
                    self.cog.logger.warning("Failed to edit message for next_page")
            else:
                await SafeInteractionHandler.safe_respond(interaction, content="You're on the last page!", ephemeral=True)
        except Exception as e:
            self.cog.logger.error(f"Error in next_page (voice): {e}", exc_info=True)
            await SafeInteractionHandler.send_error_message(interaction, "An error occurred while updating the leaderboard.")


class VoiceLeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.voice_sessions = {}
        self.voice_sessions_lock = asyncio.Lock()  # Prevent race conditions
        self.session_save_queue = {}  # Buffer for pending saves - {(guild_id, user_id): [minutes]}
        self.save_queue_lock = asyncio.Lock()  # Protect save queue from race conditions
        self.save_queue_processing = False  # FIX: Prevent concurrent queue processing
        self.MAX_SAVE_QUEUE_SIZE = 100  # FIX: Hard limit for memory leak protection
        self.max_session_duration = 10080  # Max 7 days in minutes
        self.last_daily_reset = {}  # {guild_id: datetime}
        self.last_weekly_reset = {}  # {guild_id: datetime}
        self.last_monthly_reset = {}  # {guild_id: datetime}
        self.view_cache = {}  # {(guild_id, period): view_instance} - cache views to preserve state
        self.last_month_winner_cache = {}  # FIX #10: Cache last month winner {guild_id: (winner_data, cached_at)}
        self.creation_locks = {}  # {guild_id: asyncio.Lock} - prevent concurrent message creation
        self.logger = logging.getLogger('discord.bot.voice_leaderboard')
    
    def _validate_timezone(self, tz_name: str, guild_id: int) -> str:
        """Validate timezone with case-insensitive matching"""
        if tz_name in pytz.all_timezones:
            return tz_name
        
        # Try case-insensitive match
        tz_lower = tz_name.lower()
        for valid_tz in pytz.all_timezones:
            if valid_tz.lower() == tz_lower:
                self.logger.info(f"Auto-corrected timezone '{tz_name}' to '{valid_tz}' for guild {guild_id}")
                return valid_tz
        
        self.logger.warning(f"Invalid timezone '{tz_name}' for guild {guild_id}, using UTC")
        return 'UTC'
    
    async def cog_load(self):
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Voice Leaderboard Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Voice Leaderboard Cog: MongoDB connected")
        
        # Initialize safe data retrieval
        self.safe_data = SafeLeaderboardData(self.db, self.logger)
        
        # Create indexes for optimal performance
        await self._create_indexes()
        
        # Tasks will auto-start when bot is ready (via before_loop hooks)
        # Don't manually start them here to avoid deadlock during cog loading
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Start tasks when bot is ready to avoid deadlock during cog loading"""
        if not self.update_leaderboards.is_running():
            self.update_leaderboards.start()
            self.daily_reset.start()
            self.weekly_reset.start()
            self.monthly_reset.start()
            self.save_voice_sessions_periodically.start()  # Periodic session saves
            self.periodic_session_cleanup.start()  # Hourly cleanup
            self.logger.info("Voice leaderboard tasks started")
    
    async def cog_unload(self):
        await self._save_all_voice_sessions()
        self.update_leaderboards.cancel()
        self.daily_reset.cancel()
        self.weekly_reset.cancel()
        self.monthly_reset.cancel()
        self.save_voice_sessions_periodically.cancel()
        self.periodic_session_cleanup.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client is not None and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Clean up data when bot is removed from a guild"""
        try:
            self.logger.info(f"Bot removed from guild {guild.name} ({guild.id}), cleaning up voice leaderboard data")
            
            # Clear view cache for this guild
            for period in ['daily', 'weekly', 'monthly']:
                cache_key = (guild.id, period)
                if cache_key in self.view_cache:
                    del self.view_cache[cache_key]
            
            # Save any active voice sessions for this guild before cleanup
            async with self.voice_sessions_lock:
                sessions_to_remove = [(gid, uid) for (gid, uid) in self.voice_sessions.keys() if gid == guild.id]
                for session_key in sessions_to_remove:
                    del self.voice_sessions[session_key]
            
            # Clear save queue for this guild (FIX #3: Memory leak)
            async with self.save_queue_lock:
                queue_keys_to_remove = [(gid, uid) for (gid, uid) in self.session_save_queue.keys() if gid == guild.id]
                for queue_key in queue_keys_to_remove:
                    del self.session_save_queue[queue_key]
            
            # Delete leaderboard messages
            await self.db.leaderboard_messages.delete_one({'guild_id': guild.id, 'type': 'voice'})
            
            # Delete user stats (only voice fields - chat cog will handle chat)
            result = await self.db.user_stats.update_many(
                {'guild_id': guild.id},
                {'$set': {'voice_daily': 0, 'voice_weekly': 0, 'voice_monthly': 0, 'voice_alltime': 0}}
            )
            
            # Update guild config to disable voice
            await self.db.guild_configs.update_one(
                {'guild_id': guild.id},
                {'$set': {'voice_enabled': False}}
            )
            
            self.logger.info(f"Voice leaderboard cleanup complete for guild {guild.id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up voice data for guild {guild.id}: {e}", exc_info=True)
    
    async def _initialize_voice_sessions(self):
        # Bot is already ready when this is called from before_loop
        async with self.voice_sessions_lock:
            for guild in self.bot.guilds:
                config = await self._get_guild_config(guild.id)
                if not config or not config.get('voice_enabled'):
                    continue
                for channel in guild.voice_channels:
                    if guild.afk_channel and channel.id == guild.afk_channel.id:
                        continue
                    for member in channel.members:
                        if not member.bot:
                            self.voice_sessions[(guild.id, member.id)] = datetime.utcnow()
            self.logger.info(f"Initialized {len(self.voice_sessions)} active voice sessions")
    
    async def _save_all_voice_sessions(self):
        """Save all active voice sessions to database before shutdown with memory cleanup"""
        if not self.voice_sessions:
            return
        
        async with self.voice_sessions_lock:
            saved_count = 0
            error_count = 0
            sessions_to_save = list(self.voice_sessions.items())
            
            # Process in batches to avoid overwhelming the database
            batch_size = 50
            for i in range(0, len(sessions_to_save), batch_size):
                batch = sessions_to_save[i:i + batch_size]
                for (guild_id, user_id), joined_at in batch:
                    try:
                        # Validate session data
                        if not isinstance(joined_at, datetime):
                            self.logger.warning(f"Invalid session data for user {user_id}: {joined_at}")
                            continue
                            
                        minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
                        if minutes > 0:
                            await self._increment_voice_time(guild_id, user_id, minutes)
                            saved_count += 1
                    except Exception as e:
                        error_count += 1
                        self.logger.error(f"Error saving voice session for user {user_id} in guild {guild_id}: {e}")
                
                # Small delay between batches
                if i + batch_size < len(sessions_to_save):
                    await asyncio.sleep(0.1)
            
            # Clear all sessions and save queue
            self.voice_sessions.clear()
            self.session_save_queue.clear()
            
            if saved_count > 0 or error_count > 0:
                self.logger.info(f"Session save complete: {saved_count} saved, {error_count} errors")
    
    async def _create_indexes(self):
        """Create database indexes for voice leaderboard collections"""
        try:
            # Guild configs indexes
            await self.db.guild_configs.create_index('guild_id', unique=True)
            await self.db.guild_configs.create_index([('voice_enabled', 1)])
            
            # User stats indexes for voice queries
            await self.db.user_stats.create_index([('guild_id', 1), ('user_id', 1)], unique=True)
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_daily', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_weekly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_monthly', -1)])
            await self.db.user_stats.create_index([('guild_id', 1), ('voice_alltime', -1)])
            
            # Leaderboard messages indexes
            await self.db.leaderboard_messages.create_index([('guild_id', 1), ('type', 1)], unique=True)
            await self.db.leaderboard_messages.create_index([('channel_id', 1)])
            
            # Weekly history indexes for archives
            await self.db.weekly_history.create_index([('guild_id', 1), ('type', 1), ('period', 1), ('reset_date', -1)])
            
            # TTL index to auto-delete archives older than 1 year (31536000 seconds)
            # Drop any conflicting old index first
            try:
                await self.db.weekly_history.drop_index('reset_date_1')
            except:
                pass  # Index doesn't exist, that's fine
            
            await self.db.weekly_history.create_index(
                [('reset_date', 1)],
                expireAfterSeconds=31536000,
                name='archive_ttl_1year'
            )
            
            self.logger.info("Voice Leaderboard: Database indexes created/verified")
        except Exception as e:
            self.logger.warning(f"Error creating indexes (may already exist): {e}")
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _ensure_guild_config(self, guild_id: int) -> Dict:
        config = await self._get_guild_config(guild_id)
        if not config:
            config = {'guild_id': guild_id, 'voice_enabled': False, 'voice_channel_id': None, 'timezone': 'UTC', 'leaderboard_limit': 10, 'created_at': datetime.utcnow()}
            await self.db.guild_configs.insert_one(config)
        return config
    
    async def _increment_voice_time(self, guild_id: int, user_id: int, minutes: float, max_retries: int = 3):
        """Increment voice time with validation, error handling, and retry logic"""
        # FIX: Protect against integer overflow - reject extremely large values
        MAX_TOTAL_MINUTES = 525600  # 1 year
        if minutes > MAX_TOTAL_MINUTES:
            self.logger.error(f"Rejecting excessive voice time: {minutes} minutes for user {user_id} in guild {guild_id}")
            return
        
        # Validate input using DataValidator
        is_valid, sanitized_minutes, error_msg = DataValidator.validate_voice_minutes(minutes)
        
        if not is_valid:
            self.logger.error(f"Invalid voice minutes for user {user_id} in guild {guild_id}: {error_msg}")
            return
        
        if sanitized_minutes == 0:
            self.logger.debug(f"Skipping voice increment for user {user_id}: minutes={sanitized_minutes}")
            return
        
        # Use sanitized value
        minutes = sanitized_minutes
        
        # Additional logging for long sessions
        if minutes > 1440:  # More than 24 hours
            self.logger.info(f"Long voice session detected: {minutes:.1f} minutes ({minutes/60:.1f} hours) for user {user_id} in guild {guild_id}")
        
        # Retry logic for database operations
        for attempt in range(max_retries):
            try:
                # Check if MongoDB client is still available
                if self.mongo_client is None or self.db is None:
                    self.logger.warning(f"MongoDB connection unavailable, cannot save voice time for user {user_id}")
                    return
                
                result = await self.db.user_stats.update_one(
                    {'guild_id': guild_id, 'user_id': user_id},
                    {
                        '$inc': {
                            'voice_daily': minutes, 
                            'voice_weekly': minutes, 
                            'voice_monthly': minutes, 
                            'voice_alltime': minutes
                        }, 
                        '$set': {'last_update': datetime.utcnow()}
                    },
                    upsert=True
                )
                if result.acknowledged:
                    return
            except Exception as e:
                # Check if error is due to MongoDB being closed or bot shutting down
                error_msg = str(e).lower()
                if 'cannot use mongoclient after close' in error_msg or 'connection closed' in error_msg or self.bot.is_closed():
                    # MongoDB is closing/closed or bot is shutting down, don't retry or log error
                    self.logger.debug(f"MongoDB connection closed during voice time save for user {user_id}")
                    return
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
                    self.logger.warning(f"Retry {attempt + 1}/{max_retries} for voice increment: {e}")
                else:
                    self.logger.error(f"Failed to increment voice time after {max_retries} attempts for user {user_id} in guild {guild_id}: {e}")
    
    async def _get_top_users(self, guild_id: int, period: str, limit: int = 100) -> List[Dict]:
        """Get top users with error handling and validation (FIX #18: optimized limit)"""
        # Use safe data retrieval with validation
        return await self.safe_data.get_top_users_safe(guild_id, period, limit, stat_type='voice')
    
    async def _get_last_month_winner(self, guild_id: int) -> Optional[Dict]:
        """Get last month's top active member from archive (FIX #10: with caching)"""
        # Check cache first (cache for 1 hour)
        if guild_id in self.last_month_winner_cache:
            cached_data, cached_at = self.last_month_winner_cache[guild_id]
            cache_age = (datetime.utcnow() - cached_at).total_seconds()
            if cache_age < 3600:  # 1 hour cache
                return cached_data
        
        # Use safe data retrieval
        result = await self.safe_data.get_last_month_winner_safe(guild_id, stat_type='voice')
        
        # Update cache
        self.last_month_winner_cache[guild_id] = (result, datetime.utcnow())
        
        return result
    
    async def _get_leaderboard_message(self, guild_id: int) -> Optional[Dict]:
        return await self.db.leaderboard_messages.find_one({'guild_id': guild_id, 'type': 'voice'})
    
    async def _save_leaderboard_messages(self, guild_id: int, channel_id: int, daily_id: int, weekly_id: int, monthly_id: int):
        """Save all three leaderboard message IDs"""
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'voice'},
            {'$set': {
                'channel_id': channel_id,
                'daily_message_id': daily_id,
                'weekly_message_id': weekly_id,
                'monthly_message_id': monthly_id,
                'last_update': datetime.utcnow()
            }},
            upsert=True
        )
    
    async def _save_leaderboard_message(self, guild_id: int, channel_id: int, message_id: int):
        """Legacy method - kept for compatibility"""
        await self.db.leaderboard_messages.update_one(
            {'guild_id': guild_id, 'type': 'voice'},
            {'$set': {'channel_id': channel_id, 'daily_message_id': message_id, 'last_update': datetime.utcnow()}},
            upsert=True
        )
    
    def _format_time(self, minutes: float) -> str:
        """Format minutes into human-readable time string with validation"""
        # Use SafeFormatter for consistent, validated formatting
        return SafeFormatter.format_voice_time(minutes)
    
    async def _build_all_embeds(self, guild_id: int, period: str, page: int = 0) -> List[discord.Embed]:
        """Build ALL embeds: header image + monthly + weekly + daily"""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return [discord.Embed(title="Error", description="Guild not found")]
        
        embeds = []
        
        # Embed 0: Header image only
        header_embed = discord.Embed(color=EMBED_COLOR)
        header_embed.set_image(url=Images.VOICE_HEADER)
        embeds.append(header_embed)
        
        # Embeds 1-3: Monthly, Weekly, Daily
        periods = ['monthly', 'weekly', 'daily']
        for p in periods:
            embed = await self._build_period_embed(guild_id, p, page)
            embeds.append(embed)
        
        return embeds
    
    async def _build_period_embed(self, guild_id: int, period: str, page: int) -> discord.Embed:
        """Build a single period embed with dynamic data"""
        try:
            # Validate inputs
            guild_id = LeaderboardDataValidator.validate_guild_id(guild_id)
            period = LeaderboardDataValidator.validate_period(period)
            page = max(0, int(page))  # Protect against negative page numbers
            
            guild = self.bot.get_guild(guild_id)
            if not guild:
                self.logger.error(f"Guild {guild_id} not found in _build_period_embed")
                return discord.Embed(title="Error", description="Guild not found", color=EMBED_COLOR)
            
            # Get stats with safe data retrieval (already validated)
            stats = await self._get_top_users(guild_id, period, limit=LeaderboardSettings.MAX_MEMBERS_FETCH)
            
            # Calculate pagination
            start_idx = page * LeaderboardSettings.MEMBERS_PER_PAGE
            end_idx = start_idx + LeaderboardSettings.MEMBERS_PER_PAGE
            page_stats = stats[start_idx:end_idx]
            
            # Calculate total with validation
            total_minutes = 0.0
            for s in stats:
                minutes = LeaderboardDataValidator.validate_count(s.get(f'voice_{period}', 0), allow_float=True)
                total_minutes += minutes
            total_hours = int(total_minutes // 60)
            
            # Build leaderboard lines with safe formatting
            leaderboard_lines = []
            for idx, user_stat in enumerate(page_stats, start=start_idx + 1):
                # Get username with validation
                username = await UserDisplayFormatter.get_display_name(guild, user_stat['user_id'], max_length=20)
                username = LeaderboardDataValidator.validate_username(username, max_length=20)
                username = SafeFormatter.escape_markdown(username)
                
                # Get and validate minutes
                minutes = LeaderboardDataValidator.validate_count(user_stat.get(f'voice_{period}', 0), allow_float=True)
                
                # Format with safe formatters
                rank_str = SafeFormatter.format_rank(idx)
                time_str = SafeFormatter.format_voice_time(minutes)
                
                leaderboard_lines.append(f"- `{rank_str}` | `{username}` {Emojis.ARROW} `{time_str}`")
            
            leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "No data yet"
            
            # Top user with validation
            top_user_name = "No one yet"
            top_user_time = "0m"
            if stats:
                top_user_name = await UserDisplayFormatter.get_display_name(guild, stats[0]['user_id'], max_length=20)
                top_user_name = LeaderboardDataValidator.validate_username(top_user_name, max_length=20)
                top_user_name = SafeFormatter.escape_markdown(top_user_name)
                top_user_minutes = LeaderboardDataValidator.validate_count(stats[0].get(f'voice_{period}', 0), allow_float=True)
                top_user_time = SafeFormatter.format_voice_time(top_user_minutes)
            
            # Period display with safe formatting
            config = await self._get_guild_config(guild_id)
            tz_name = config.get('timezone', LeaderboardSettings.DEFAULT_TIMEZONE) if config else LeaderboardSettings.DEFAULT_TIMEZONE
            period_display = SafeFormatter.format_period_display(period, tz_name)
            
            period_title = PeriodConfig.VOICE_TITLES.get(period, 'Rankings')
            subtitle = PeriodConfig.VOICE_SUBTITLES.get(period, 'Most active voice members are here!')
            
            # Next reset time with validation
            try:
                tz = pytz.timezone(tz_name)
            except Exception as e:
                self.logger.warning(f"Invalid timezone '{tz_name}': {e}, using UTC")
                tz = pytz.UTC
            
            if period == 'daily':
                next_reset = ResetTimeCalculator.get_next_daily_reset(tz)
            elif period == 'weekly':
                next_reset = ResetTimeCalculator.get_next_weekly_reset(tz, LeaderboardSettings.WEEKLY_RESET_DAY, LeaderboardSettings.WEEKLY_RESET_HOUR)
            else:  # monthly
                next_reset = ResetTimeCalculator.get_next_monthly_reset(tz)
            
            reset_timestamp = int(next_reset.timestamp())
            
            # Get last month's winner for monthly embeds
            last_month_winner_text = None
            if period == 'monthly':
                last_month_data = await self._get_last_month_winner(guild_id)
                if last_month_data:
                    winner_name = await UserDisplayFormatter.get_display_name(guild, last_month_data['user_id'], max_length=20)
                    winner_name = LeaderboardDataValidator.validate_username(winner_name, max_length=20)
                    winner_name = SafeFormatter.escape_markdown(winner_name)
                    
                    winner_minutes = LeaderboardDataValidator.validate_count(last_month_data.get('minutes', 0), allow_float=True)
                    time_str = SafeFormatter.format_voice_time(winner_minutes)
                    
                    month_name = last_month_data['month'].strftime('%B %Y')
                    last_month_winner_text = f"`{winner_name}` with `{time_str}` in {month_name}"
            
            # Build description using config template
            description = VoiceTemplates.build_description(
                period_title=period_title,
                total_hours=total_hours,
                period_display=period_display,
                top_user_name=top_user_name,
                top_user_time=top_user_time,
                leaderboard_text=leaderboard_text,
                reset_timestamp=reset_timestamp,
                subtitle=subtitle,
                period=period,
                last_month_winner=last_month_winner_text
            )
            
            # Sanitize description for embed
            description = SafeFormatter.sanitize_for_embed(description)
            
            # Create embed
            embed = discord.Embed(description=description, color=EMBED_COLOR)
            
            # Footer
            footer_text = VoiceTemplates.FOOTER_TEXT
            if len(stats) > LeaderboardSettings.MEMBERS_PER_PAGE:
                page_size = max(1, LeaderboardSettings.MEMBERS_PER_PAGE)
                total_pages = (len(stats) - 1) // page_size + 1
                footer_text = f"Page {page + 1}/{total_pages} • {footer_text}"
            embed.set_footer(text=footer_text, icon_url=Images.FOOTER_ICON)
            
            # Divider image
            embed.set_image(url=Images.DIVIDER)
            
            return embed
            
        except ValueError as e:
            self.logger.error(f"Validation error in _build_period_embed: {e}")
            return discord.Embed(title="Error", description="Invalid data", color=EMBED_COLOR)
        except Exception as e:
            self.logger.error(f"Error building period embed: {e}", exc_info=True)
            return discord.Embed(title="Error", description="Failed to build leaderboard", color=EMBED_COLOR)
        
        # Divider image
        embed.set_image(url=Images.DIVIDER)
        
        return embed
    
    async def _create_full_leaderboard_message(self, channel: discord.TextChannel, guild_id: int, vibe_channel_id: int = None):
        """Create separate leaderboard messages for each period with individual buttons"""
        # Use lock to prevent concurrent creation (race condition fix)
        if guild_id not in self.creation_locks:
            self.creation_locks[guild_id] = asyncio.Lock()
        
        async with self.creation_locks[guild_id]:
            try:
                # Check if messages were already created by another task
                existing_msg_data = await self._get_leaderboard_message(guild_id)
                if existing_msg_data:
                    daily_id = existing_msg_data.get('daily_message_id')
                    weekly_id = existing_msg_data.get('weekly_message_id')
                    monthly_id = existing_msg_data.get('monthly_message_id')
                    
                    # If all messages exist, don't recreate
                    if daily_id and weekly_id and monthly_id:
                        self.logger.info(f"Voice leaderboard messages already exist for guild {guild_id}, skipping creation")
                        return
                
                # Check permissions before creating
                permissions = channel.permissions_for(channel.guild.me)
                if not permissions.send_messages or not permissions.embed_links:
                    self.logger.error(f"Missing permissions in channel {channel.id} for guild {guild_id}")
                    return
                
                # Clear old cached views since we're creating new messages
                for period in ['daily', 'weekly', 'monthly']:
                    cache_key = (guild_id, period)
                    if cache_key in self.view_cache:
                        del self.view_cache[cache_key]
                
                # Send header image
                header_embed = discord.Embed(color=EMBED_COLOR)
                header_embed.set_image(url=Images.VOICE_HEADER)
                await channel.send(embed=header_embed)
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.5)
                
                # Send monthly embed with Join the Vibe button
                monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
                monthly_view = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
                monthly_message = await channel.send(embed=monthly_embed, view=monthly_view)
                self.view_cache[(guild_id, 'monthly')] = monthly_view
                
                await asyncio.sleep(0.5)
                
                # Send weekly embed with Join the Vibe button
                weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
                weekly_view = VoiceLeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
                weekly_message = await channel.send(embed=weekly_embed, view=weekly_view)
                self.view_cache[(guild_id, 'weekly')] = weekly_view
                
                await asyncio.sleep(0.5)
                
                # Send daily embed with pagination buttons
                daily_embed = await self._build_period_embed(guild_id, 'daily', page=0)
                daily_view = VoiceLeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
                daily_message = await channel.send(embed=daily_embed, view=daily_view)
                self.view_cache[(guild_id, 'daily')] = daily_view
                
                # Save ALL message IDs for updates
                await self._save_leaderboard_messages(guild_id, channel.id, daily_message.id, weekly_message.id, monthly_message.id)
                self.logger.info(f"Created voice leaderboard messages for guild {guild_id}")
            except discord.Forbidden as e:
                self.logger.error(f"Permission denied creating leaderboard for guild {guild_id}: {e}")
            except discord.HTTPException as e:
                self.logger.error(f"HTTP error creating leaderboard for guild {guild_id}: {e}")
            except Exception as e:
                self.logger.error(f"Failed to create leaderboard for guild {guild_id}: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Track voice activity with improved race condition handling and memory management"""
        # Filter bots immediately
        if member.bot:
            return
        
        try:
            config = await self._get_guild_config(member.guild.id)
            if not config or not config.get('voice_enabled'):
                return
            
            guild_id = member.guild.id
            user_id = member.id
            session_key = (guild_id, user_id)
            current_time = datetime.utcnow()
            
            # Clean up old sessions periodically (memory leak prevention)
            if len(self.voice_sessions) > 1000:
                await self._cleanup_stale_sessions()
            
            # Determine if channels are AFK
            afk_channel_id = member.guild.afk_channel.id if member.guild.afk_channel else None
            before_is_afk = before.channel and afk_channel_id and before.channel.id == afk_channel_id
            after_is_afk = after.channel and afk_channel_id and after.channel.id == afk_channel_id
            
            async with self.voice_sessions_lock:
                # User joined a voice channel (not AFK)
                if before.channel is None and after.channel is not None and not after_is_afk:
                    self.voice_sessions[session_key] = current_time
                    self.logger.debug(f"Voice session started: {member.display_name} in guild {guild_id}")
                
                # User left a voice channel (not from AFK)
                elif before.channel is not None and after.channel is None and not before_is_afk:
                    if session_key in self.voice_sessions:
                        joined_at = self.voice_sessions.pop(session_key, None)
                        if joined_at and isinstance(joined_at, datetime):
                            minutes = (current_time - joined_at).total_seconds() / 60
                            if 0 < minutes < self.max_session_duration:  # Validate reasonable time
                                # Save immediately for user disconnects (more reliable than queuing)
                                await self._increment_voice_time(guild_id, user_id, minutes)
                                self.logger.debug(f"Voice session ended: {member.display_name} - {minutes:.1f} minutes")
                            elif minutes >= self.max_session_duration:
                                self.logger.warning(f"Session exceeded max duration for {member.display_name}: {minutes:.1f} minutes")
                                await self._increment_voice_time(guild_id, user_id, self.max_session_duration)
                
                # User moved between channels
                elif before.channel != after.channel and before.channel is not None and after.channel is not None:
                    # Moving to AFK from active channel
                    if not before_is_afk and after_is_afk:
                        if session_key in self.voice_sessions:
                            joined_at = self.voice_sessions.pop(session_key, None)
                            if joined_at and isinstance(joined_at, datetime):
                                minutes = (current_time - joined_at).total_seconds() / 60
                                if 0 < minutes < self.max_session_duration:
                                    # Save immediately when moving to AFK (more reliable)
                                    await self._increment_voice_time(guild_id, user_id, minutes)
                                    self.logger.debug(f"Moved to AFK: {member.display_name} - {minutes:.1f} minutes saved")
                    
                    # Moving from AFK to active channel
                    elif before_is_afk and not after_is_afk:
                        self.voice_sessions[session_key] = current_time
                        self.logger.debug(f"Moved from AFK: {member.display_name}")
                    
                    # Moving between active channels (keep session alive)
                    elif not before_is_afk and not after_is_afk:
                        # Validate existing session
                        if session_key not in self.voice_sessions:
                            self.voice_sessions[session_key] = current_time
                            self.logger.debug(f"Session missing, started new: {member.display_name}")
        
        except Exception as e:
            self.logger.error(f"Error in voice state update for {member.id}: {e}", exc_info=True)
    
    @tasks.loop(minutes=5)
    async def update_leaderboards(self):
        # FIX: Wrap entire task in try-except to prevent task from stopping on error
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                try:
                    msg_data = await self._get_leaderboard_message(guild_id)
                    vibe_channel_id = config.get('vibe_channel_id')
                    
                    # If no message data exists, try to create messages if channel is configured
                    if not msg_data:
                        voice_channel_id = config.get('voice_channel_id')
                        if voice_channel_id:
                            channel = guild.get_channel(voice_channel_id)
                            if channel:
                                self.logger.info(f"No leaderboard messages found for guild {guild_id}, creating...")
                                await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        continue
                    
                    channel = guild.get_channel(msg_data['channel_id'])
                    if not channel:
                        # Channel was deleted, clean up database reference
                        self.logger.warning(f"Voice leaderboard channel {msg_data['channel_id']} not found for guild {guild_id}, cleaning up")
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                        continue
                    
                    # Get message IDs (support both old and new format)
                    daily_id = msg_data.get('daily_message_id') or msg_data.get('message_id')
                    weekly_id = msg_data.get('weekly_message_id')
                    monthly_id = msg_data.get('monthly_message_id')
                    
                    self.logger.debug(f"Guild {guild_id} - Message IDs: daily={daily_id}, weekly={weekly_id}, monthly={monthly_id}")
                    
                    # Track which messages need updating
                    daily_updated = False
                    weekly_updated = False
                    monthly_updated = False
                    
                    # Track which message IDs to update in database
                    updated_message_ids = {}
                    
                    # Update all three messages
                    try:
                        # Update daily message
                        if daily_id:
                            try:
                                self.logger.debug(f"Fetching daily message {daily_id} for guild {guild_id}")
                                daily_message = await channel.fetch_message(daily_id)
                                await asyncio.sleep(0.2)  # Rate limit protection
                                self.logger.debug(f"Successfully fetched daily message {daily_id}")
                                if daily_message.author.id == self.bot.user.id:
                                    # Get or create cached view to preserve page state
                                    cache_key = (guild_id, 'daily')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = VoiceLeaderboardPaginator(self, guild_id, 'daily', page=0, vibe_channel_id=vibe_channel_id)
                                    daily_view = self.view_cache[cache_key]
                                    # Build embed with current page from cached view
                                    daily_embed = await self._build_period_embed(guild_id, 'daily', page=daily_view.page)
                                    await daily_message.edit(embed=daily_embed, view=daily_view)
                                    await asyncio.sleep(0.2)  # Rate limit protection
                                    self.logger.debug(f"Updated daily voice leaderboard for guild {guild_id}")
                                    daily_updated = True
                                    updated_message_ids['daily_message_id'] = daily_id
                                else:
                                    self.logger.warning(f"Daily message {daily_id} not owned by bot for guild {guild_id}")
                            except discord.NotFound:
                                self.logger.warning(f"Daily message {daily_id} not found for guild {guild_id}")
                            except discord.Forbidden as e:
                                self.logger.error(f"Permission denied for daily message {daily_id}: {e}")
                            except discord.HTTPException as e:
                                if e.status in [503, 502, 500]:
                                    # Discord API temporarily unavailable, skip this update
                                    self.logger.warning(f"Discord API unavailable (HTTP {e.status}) for guild {guild_id}, will retry next cycle")
                                else:
                                    self.logger.error(f"HTTP error updating daily message for guild {guild_id}: {e}")
                            except Exception as e:
                                self.logger.error(f"Error updating daily message for guild {guild_id}: {e}")
                        
                        # Update weekly message
                        if weekly_id:
                            try:
                                self.logger.debug(f"Fetching weekly message {weekly_id} for guild {guild_id}")
                                weekly_message = await channel.fetch_message(weekly_id)
                                await asyncio.sleep(0.2)  # Rate limit protection
                                self.logger.debug(f"Successfully fetched weekly message {weekly_id}")
                                if weekly_message.author.id == self.bot.user.id:
                                    # Get or create cached view
                                    cache_key = (guild_id, 'weekly')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = VoiceLeaderboardPaginator(self, guild_id, 'weekly', page=0, vibe_channel_id=vibe_channel_id)
                                    weekly_view = self.view_cache[cache_key]
                                    weekly_embed = await self._build_period_embed(guild_id, 'weekly', page=0)
                                    await weekly_message.edit(embed=weekly_embed, view=weekly_view)
                                    await asyncio.sleep(0.2)  # Rate limit protection
                                    self.logger.debug(f"Updated weekly voice leaderboard for guild {guild_id}")
                                    weekly_updated = True
                                    updated_message_ids['weekly_message_id'] = weekly_id
                                else:
                                    self.logger.warning(f"Weekly message {weekly_id} not owned by bot for guild {guild_id}")
                            except discord.NotFound:
                                self.logger.warning(f"Weekly message {weekly_id} not found for guild {guild_id}")
                            except discord.Forbidden as e:
                                self.logger.error(f"Permission denied for weekly message {weekly_id}: {e}")
                            except discord.HTTPException as e:
                                if e.status in [503, 502, 500]:
                                    self.logger.warning(f"Discord API unavailable (HTTP {e.status}) for guild {guild_id}, will retry next cycle")
                                else:
                                    self.logger.error(f"HTTP error updating weekly message for guild {guild_id}: {e}")
                            except Exception as e:
                                self.logger.error(f"Error updating weekly message for guild {guild_id}: {e}")
                        
                        # Update monthly message
                        if monthly_id:
                            try:
                                self.logger.debug(f"Fetching monthly message {monthly_id} for guild {guild_id}")
                                monthly_message = await channel.fetch_message(monthly_id)
                                await asyncio.sleep(0.2)  # Rate limit protection
                                self.logger.debug(f"Successfully fetched monthly message {monthly_id}")
                                if monthly_message.author.id == self.bot.user.id:
                                    # Get or create cached view
                                    cache_key = (guild_id, 'monthly')
                                    if cache_key not in self.view_cache:
                                        self.view_cache[cache_key] = VoiceLeaderboardPaginator(self, guild_id, 'monthly', page=0, vibe_channel_id=vibe_channel_id)
                                    monthly_view = self.view_cache[cache_key]
                                    monthly_embed = await self._build_period_embed(guild_id, 'monthly', page=0)
                                    await monthly_message.edit(embed=monthly_embed, view=monthly_view)
                                    await asyncio.sleep(0.2)  # Rate limit protection
                                    self.logger.debug(f"Updated monthly voice leaderboard for guild {guild_id}")
                                    monthly_updated = True
                                    updated_message_ids['monthly_message_id'] = monthly_id
                                else:
                                    self.logger.warning(f"Monthly message {monthly_id} not owned by bot for guild {guild_id}")
                            except discord.NotFound:
                                self.logger.warning(f"Monthly message {monthly_id} not found for guild {guild_id}")
                            except discord.Forbidden as e:
                                self.logger.error(f"Permission denied for monthly message {monthly_id}: {e}")
                            except discord.HTTPException as e:
                                if e.status in [503, 502, 500]:
                                    self.logger.warning(f"Discord API unavailable (HTTP {e.status}) for guild {guild_id}, will retry next cycle")
                                else:
                                    self.logger.error(f"HTTP error updating monthly message for guild {guild_id}: {e}")
                            except Exception as e:
                                self.logger.error(f"Error updating monthly message for guild {guild_id}: {e}")
                        
                        # Update database with successfully updated message IDs
                        if updated_message_ids:
                            await self.db.leaderboard_messages.update_one(
                                {'guild_id': guild_id, 'type': 'voice'},
                                {'$set': {**updated_message_ids, 'last_update': datetime.utcnow()}},
                                upsert=False
                            )
                        
                        # Only recreate if ALL messages are missing or failed
                        if not daily_id and not weekly_id and not monthly_id:
                            self.logger.info(f"No voice leaderboard message IDs found for guild {guild_id}, recreating all embeds")
                            await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                            await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        elif not daily_updated and not weekly_updated and not monthly_updated and (daily_id or weekly_id or monthly_id):
                            # All messages failed but IDs exist - likely all deleted
                            self.logger.warning(f"All voice leaderboard messages failed to update for guild {guild_id}, recreating all embeds")
                            await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                            await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                        else:
                            if daily_updated and weekly_updated and monthly_updated:
                                self.logger.info(f"Successfully updated all voice leaderboards for guild {guild_id}")
                            else:
                                self.logger.info(f"Partially updated voice leaderboards for guild {guild_id} (daily={daily_updated}, weekly={weekly_updated}, monthly={monthly_updated})")
                            
                    except discord.Forbidden as e:
                        self.logger.error(
                            f"Permission denied editing message for guild {guild_id}: {e}. "
                            f"Removing invalid reference and recreating."
                        )
                        await self.db.leaderboard_messages.delete_one({'guild_id': guild_id, 'type': 'voice'})
                        await self._create_full_leaderboard_message(channel, guild_id, vibe_channel_id)
                    except discord.HTTPException as e:
                        # Handle rate limits and other HTTP errors
                        if e.status == 429:  # Rate limited
                            self.logger.warning(f"Rate limited updating voice leaderboard for guild {guild_id}, will retry next cycle")
                        else:
                            self.logger.error(f"HTTP error updating voice leaderboard for guild {guild_id}: {e}")
                except Exception as e:
                    self.logger.error(f"Error updating voice leaderboard for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in update_leaderboards task: {e}", exc_info=True)
    
    @update_leaderboards.before_loop
    async def before_update_leaderboards(self):
        await self.bot.wait_until_ready()
        # Initialize voice sessions after bot is ready
        await self._initialize_voice_sessions()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes for zero chance of missing
    async def weekly_reset(self):
        """Check for weekly reset with missed reset detection
        
        NOTE: If Star of the Week system is configured, it handles weekly resets.
        This task only runs for guilds without Star system or as a backup.
        """
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                
                # Check if Star of the Week system is managing resets for this guild
                star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
                if star_config:
                    # Star system is configured - it will handle weekly resets
                    self.logger.debug(f"Star system manages weekly resets for guild {guild_id}, skipping")
                    continue
                
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    tz_name = self._validate_timezone(tz_name, guild_id)
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Multiple checks to NEVER miss weekly reset
                    last_reset_time = config.get('last_voice_weekly_reset')
                    should_reset = False
                    
                    if last_reset_time:
                        hours_since = (datetime.utcnow() - last_reset_time).total_seconds() / 3600
                        days_since = hours_since / 24
                        
                        # Check 1: Has it been at least 6.5 days?
                        if days_since >= 6.5:
                            if now.weekday() == 6 and now.hour >= 12:  # Sunday noon or later
                                should_reset = True
                                self.logger.info(f"Voice weekly reset for guild {guild_id}: {days_since:.1f} days since last")
                            elif now.weekday() == 0:  # Monday (missed Sunday)
                                should_reset = True
                                self.logger.warning(f"Missed Sunday voice reset for guild {guild_id}, doing it now")
                            elif days_since >= 7.0:  # Full week passed
                                should_reset = True
                                self.logger.warning(f"Full week passed for voice guild {guild_id}: {days_since:.1f} days")
                    else:
                        # Never reset before - do it now if enabled
                        if config.get('voice_enabled'):
                            should_reset = True
                            self.logger.info(f"First voice weekly reset for guild {guild_id}")
                    
                    if should_reset:
                        await self._reset_weekly_stats(guild_id)
                        # Persist reset time to database
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_voice_weekly_reset': datetime.utcnow()}}
                        )
                        self.last_weekly_reset[guild_id] = now
                
                except pytz.exceptions.UnknownTimeZoneError:
                    self.logger.error(f"Invalid timezone for guild {guild_id}: {tz_name}")
                except Exception as e:
                    self.logger.error(f"Error checking weekly reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in weekly_reset task: {e}", exc_info=True)
    
    @weekly_reset.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes
    async def daily_reset(self):
        """Check for daily reset (midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    tz_name = self._validate_timezone(tz_name, guild_id)
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    # Check for reset window (midnight to 1 AM)
                    if 0 <= now.hour < 1:
                        # Check if already reset today
                        last_reset = self.last_daily_reset.get(guild_id)
                        if last_reset and last_reset.date() == now.date():
                            continue  # Already reset today
                        
                        await self._reset_daily_stats(guild_id)
                        self.last_daily_reset[guild_id] = now
                except pytz.exceptions.UnknownTimeZoneError:
                    self.logger.error(f"Invalid timezone for guild {guild_id}: {tz_name}")
                except Exception as e:
                    self.logger.error(f"Error checking daily reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in daily_reset task: {e}", exc_info=True)
    
    @daily_reset.before_loop
    async def before_daily_reset(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)  # Check every 5 minutes
    async def monthly_reset(self):
        """Check for monthly reset (1st of month midnight guild time)"""
        try:
            cursor = self.db.guild_configs.find({'voice_enabled': True})
            configs = await cursor.to_list(length=1000)
            for config in configs:
                guild_id = config['guild_id']
                tz_name = config.get('timezone', 'UTC')
                try:
                    # Validate timezone
                    tz_name = self._validate_timezone(tz_name, guild_id)
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    # Check for monthly reset window (1st of month, midnight to 1 AM)
                    if now.day == 1 and 0 <= now.hour < 1:
                        # Check if already reset this month (use database as source of truth)
                        last_db_reset = config.get('last_voice_monthly_reset')
                        if last_db_reset:
                            last_reset_tz = last_db_reset.replace(tzinfo=pytz.UTC).astimezone(tz)
                            if last_reset_tz.month == now.month and last_reset_tz.year == now.year:
                                continue  # Already reset this month
                        
                        await self._reset_monthly_stats(guild_id)
                        self.last_monthly_reset[guild_id] = now
                        # Persist to database for crash recovery
                        await self.db.guild_configs.update_one(
                            {'guild_id': guild_id},
                            {'$set': {'last_voice_monthly_reset': datetime.utcnow()}}
                        )
                except Exception as e:
                    self.logger.error(f"Error checking monthly reset for guild {guild_id}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error in monthly_reset task: {e}", exc_info=True)
    
    @monthly_reset.before_loop
    async def before_monthly_reset(self):
        await self.bot.wait_until_ready()
    
    async def _cleanup_stale_sessions(self):
        """Remove stale sessions to prevent memory leaks (FIX #8: optimized with early exit)"""
        try:
            current_time = datetime.utcnow()
            stale_sessions = []
            
            # FIX #8: Use async lock and optimize iteration
            async with self.voice_sessions_lock:
                # Early exit if no sessions
                if not self.voice_sessions:
                    return
                
                # Build list of stale sessions
                for session_key, joined_at in self.voice_sessions.items():
                    if isinstance(joined_at, datetime):
                        duration = (current_time - joined_at).total_seconds() / 60
                        # Remove sessions older than max duration
                        if duration > self.max_session_duration:
                            stale_sessions.append((session_key, joined_at))
                
                # Remove stale sessions from dict
                for session_key, _ in stale_sessions:
                    self.voice_sessions.pop(session_key, None)
            
            # Process saves outside the lock
            if stale_sessions:
                self.logger.info(f"Cleaning up {len(stale_sessions)} stale sessions")
                for session_key, joined_at in stale_sessions:
                    guild_id, user_id = session_key
                    # Save the max duration
                    await self._increment_voice_time(guild_id, user_id, self.max_session_duration)
                    self.logger.warning(f"Cleaned up stale session for user {user_id} in guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up stale sessions: {e}")
    
    async def _process_save_queue(self):
        """Process queued voice time saves (FIX #1 & #2: Sum accumulated times, use lock)"""
        # FIX: Prevent concurrent processing
        if self.save_queue_processing:
            return
        
        self.save_queue_processing = True
        try:
            async with self.save_queue_lock:
                if not self.session_save_queue:
                    return
                
                # FIX: Hard limit protection against memory leak
                if len(self.session_save_queue) > self.MAX_SAVE_QUEUE_SIZE:
                    self.logger.error(f"Save queue exceeded limit: {len(self.session_save_queue)}, forcing clear")
                    self.session_save_queue.clear()
                    return
                
                try:
                    # Copy and clear queue
                    queue_copy = self.session_save_queue.copy()
                    self.session_save_queue.clear()
                except Exception as e:
                    self.logger.error(f"Error copying save queue: {e}")
                    return
            
            # Process outside the lock to avoid blocking
            try:
                # Process all queued saves, summing multiple entries per user
                for (guild_id, user_id), minutes_list in queue_copy.items():
                    total_minutes = sum(minutes_list)  # Sum all accumulated times
                    if total_minutes > 0:
                        await self._increment_voice_time(guild_id, user_id, total_minutes)
                        self.logger.debug(f"Processed queued save: {total_minutes:.2f} minutes ({len(minutes_list)} sessions) for user {user_id}")
            except Exception as e:
                self.logger.error(f"Error processing save queue: {e}")
        finally:
            self.save_queue_processing = False
    
    @tasks.loop(minutes=10)
    async def save_voice_sessions_periodically(self):
        """
        Periodically save active voice sessions to prevent data loss on crashes.
        Runs every 10 minutes to update ongoing sessions.
        """
        try:
            # Process any pending saves first
            await self._process_save_queue()
            
            if not self.voice_sessions:
                return
            
            async with self.voice_sessions_lock:
                saved_count = 0
                error_count = 0
                current_time = datetime.utcnow()
                sessions_to_update = []
                
                for (guild_id, user_id), joined_at in list(self.voice_sessions.items()):
                    try:
                        if not isinstance(joined_at, datetime):
                            self.logger.warning(f"Invalid session data for user {user_id}: {joined_at}")
                            continue
                        
                        # Calculate time since last save (or join)
                        minutes = (current_time - joined_at).total_seconds() / 60
                        
                        if minutes > 0 and minutes < self.max_session_duration:
                            sessions_to_update.append(((guild_id, user_id), minutes))
                        elif minutes >= self.max_session_duration:
                            # Cap at max duration and remove session
                            sessions_to_update.append(((guild_id, user_id), self.max_session_duration))
                            del self.voice_sessions[(guild_id, user_id)]
                            self.logger.warning(f"Capped long session at {self.max_session_duration} minutes for user {user_id}")
                    except Exception as e:
                        error_count += 1
                        self.logger.error(f"Error processing session for user {user_id}: {e}")
                
                # Save in batches
                for (guild_id, user_id), minutes in sessions_to_update:
                    await self._increment_voice_time(guild_id, user_id, minutes)
                    # Reset session start time to now (so we don't double-count)
                    if (guild_id, user_id) in self.voice_sessions:
                        self.voice_sessions[(guild_id, user_id)] = current_time
                    saved_count += 1
                
                if saved_count > 0 or error_count > 0:
                    self.logger.debug(f"Periodic save: {saved_count} saved, {error_count} errors, {len(self.voice_sessions)} active")
        
        except Exception as e:
            self.logger.error(f"Error in save_voice_sessions_periodically task: {e}", exc_info=True)
    
    @save_voice_sessions_periodically.before_loop
    async def before_save_voice_sessions_periodically(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=1)
    async def periodic_session_cleanup(self):
        """
        Periodic cleanup of stale voice sessions.
        Runs every hour to prevent memory leaks even in low-activity servers.
        """
        try:
            if len(self.voice_sessions) > 0:
                self.logger.debug(f"Running periodic session cleanup ({len(self.voice_sessions)} active sessions)")
                await self._cleanup_stale_sessions()
        except Exception as e:
            self.logger.error(f"Error in periodic session cleanup: {e}", exc_info=True)
    
    @periodic_session_cleanup.before_loop
    async def before_periodic_session_cleanup(self):
        await self.bot.wait_until_ready()
    
    async def _reset_daily_stats(self, guild_id: int):
        """Reset daily voice stats"""
        try:
            await self.db.user_stats.update_many(
                {'guild_id': guild_id},
                {'$set': {'voice_daily': 0}}
            )
            self.logger.info(f"Reset daily voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting daily stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_monthly_stats(self, guild_id: int):
        """Reset monthly voice stats and archive data"""
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_monthly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'monthly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_monthly': 0}})
            self.logger.info(f"Archived and reset monthly voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting monthly stats for guild {guild_id}: {e}", exc_info=True)
    
    async def _reset_weekly_stats(self, guild_id: int):
        try:
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_weekly': {'$gt': 0}})
            stats = await cursor.to_list(length=10000)
            if stats:
                archive_doc = {'guild_id': guild_id, 'type': 'voice', 'period': 'weekly', 'reset_date': datetime.utcnow(), 'stats': stats}
                await self.db.weekly_history.insert_one(archive_doc)
            await self.db.user_stats.update_many({'guild_id': guild_id}, {'$set': {'voice_weekly': 0}})
            self.logger.info(f"Archived and reset weekly voice stats for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error resetting weekly stats for guild {guild_id}: {e}", exc_info=True)
    
    @app_commands.command(name="voice-leaderboard-debug", description="[ADMIN] Debug voice leaderboard system")
    @app_commands.checks.has_permissions(administrator=True)
    async def debug_voice_leaderboard(self, interaction: discord.Interaction):
        """Diagnose voice leaderboard issues"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            guild_id = interaction.guild.id
            
            # Check configuration
            config = await self._get_guild_config(guild_id)
            if not config:
                await interaction.followup.send("❌ No configuration found! Use `/live-leaderboard-voice action:Setup` first.", ephemeral=True)
                return
            
            debug_info = "# 🔍 Voice Leaderboard Debug Info\n\n"
            
            # Configuration status
            debug_info += "## ⚙️ Configuration\n"
            debug_info += f"✅ Config exists\n"
            debug_info += f"**Enabled:** {'✅ Yes' if config.get('voice_enabled') else '❌ No'}\n"
            debug_info += f"**Timezone:** `{config.get('timezone', 'UTC')}`\n"
            
            voice_channel_id = config.get('voice_channel_id')
            if voice_channel_id:
                channel = interaction.guild.get_channel(voice_channel_id)
                if channel:
                    debug_info += f"**Channel:** {channel.mention} (ID: {voice_channel_id})\n"
                else:
                    debug_info += f"**Channel:** ❌ Not found (ID: {voice_channel_id})\n"
            else:
                debug_info += f"**Channel:** ❌ Not configured\n"
            
            # Task status
            debug_info += f"\n## 🔄 Background Tasks\n"
            debug_info += f"**Update Task:** {'✅ Running' if self.update_leaderboards.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Daily Reset:** {'✅ Running' if self.daily_reset.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Weekly Reset:** {'✅ Running' if self.weekly_reset.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Monthly Reset:** {'✅ Running' if self.monthly_reset.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Save Sessions:** {'✅ Running' if self.save_voice_sessions_periodically.is_running() else '❌ NOT RUNNING'}\n"
            debug_info += f"**Session Cleanup:** {'✅ Running' if self.periodic_session_cleanup.is_running() else '❌ NOT RUNNING'}\n"
            
            # Check message data
            msg_data = await self._get_leaderboard_message(guild_id)
            debug_info += f"\n## 📨 Leaderboard Messages\n"
            if msg_data:
                debug_info += f"**Channel ID:** {msg_data.get('channel_id')}\n"
                debug_info += f"**Daily Message ID:** {msg_data.get('daily_message_id', 'Not set')}\n"
                debug_info += f"**Weekly Message ID:** {msg_data.get('weekly_message_id', 'Not set')}\n"
                debug_info += f"**Monthly Message ID:** {msg_data.get('monthly_message_id', 'Not set')}\n"
            else:
                debug_info += f"❌ No message data found in database\n"
            
            # Check user stats
            stats_count = await self.db.user_stats.count_documents({'guild_id': guild_id})
            active_daily = await self.db.user_stats.count_documents({'guild_id': guild_id, 'voice_daily': {'$gt': 0}})
            active_weekly = await self.db.user_stats.count_documents({'guild_id': guild_id, 'voice_weekly': {'$gt': 0}})
            active_monthly = await self.db.user_stats.count_documents({'guild_id': guild_id, 'voice_monthly': {'$gt': 0}})
            
            debug_info += f"\n## 📊 User Statistics\n"
            debug_info += f"**Total Users Tracked:** {stats_count}\n"
            debug_info += f"**Active Today:** {active_daily}\n"
            debug_info += f"**Active This Week:** {active_weekly}\n"
            debug_info += f"**Active This Month:** {active_monthly}\n"
            
            # Check active voice sessions
            active_sessions = sum(1 for (gid, uid) in self.voice_sessions.keys() if gid == guild_id)
            debug_info += f"**Active Voice Sessions:** {active_sessions}\n"
            
            # Get top user for verification
            top_users = await self._get_top_users(guild_id, 'daily', limit=1)
            if top_users:
                top_user = top_users[0]
                member = interaction.guild.get_member(top_user['user_id'])
                username = member.display_name if member else f"User {top_user['user_id']}"
                minutes = top_user.get('voice_daily', 0)
                hours = int(minutes // 60)
                mins = int(minutes % 60)
                time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
                debug_info += f"\n**Top User Today:** {username} with {time_str}\n"
            
            # Check voice state listener
            debug_info += f"\n## 👂 Event Listeners\n"
            debug_info += f"**on_voice_state_update:** ✅ Registered\n"
            debug_info += f"**Bot User ID:** {self.bot.user.id}\n"
            
            # Database connection
            debug_info += f"\n## 💾 Database\n"
            if self.db is not None:
                try:
                    await self.db.command('ping')
                    debug_info += f"**Connection:** ✅ Active\n"
                except Exception as e:
                    debug_info += f"**Connection:** ❌ Error: {e}\n"
            else:
                debug_info += f"**Connection:** ❌ Database not initialized\n"
            
            # Recommendations
            debug_info += f"\n## 💡 Recommendations\n"
            if not config.get('voice_enabled'):
                debug_info += f"⚠️ Enable the leaderboard with `/live-leaderboard-voice action:Enable`\n"
            if not self.update_leaderboards.is_running():
                debug_info += f"⚠️ Update task not running - restart the bot\n"
            if not msg_data:
                debug_info += f"⚠️ No leaderboard messages - run `/live-leaderboard-voice action:Setup`\n"
            if active_daily == 0 and active_sessions == 0:
                debug_info += f"⚠️ No activity tracked today - join a voice channel to test\n"
            
            await interaction.followup.send(debug_info, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error during debug: {e}", ephemeral=True)
            self.logger.error(f"Voice debug command error: {e}", exc_info=True)
    
    @app_commands.command(name="live-leaderboard-voice", description="Setup or toggle live voice leaderboard")
    @app_commands.describe(
        action="Enable, disable, or setup the leaderboard",
        voice_channel="Channel for voice leaderboard (required for setup)",
        timezone="Timezone in IANA format (e.g., America/New_York)",
        vibe_channel="Channel users will be redirected to when clicking 'Join the Vibe' button"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Setup", value="setup")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_voice_leaderboard(
        self, 
        interaction: discord.Interaction, 
        action: app_commands.Choice[str],
        voice_channel: Optional[discord.TextChannel] = None,
        timezone: Optional[str] = "UTC",
        vibe_channel: Optional[discord.TextChannel] = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            config = await self._get_guild_config(interaction.guild.id)
            
            # Handle disable action
            if action.value == "disable":
                if not config or not config.get('voice_enabled'):
                    await interaction.followup.send("⚠️ Voice leaderboard is already disabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'voice_enabled': False}}
                )
                await interaction.followup.send(
                    "✅ **Voice leaderboard disabled!**\n"
                    "📊 Voice time tracking has been paused.\n"
                    "💡 Use `/live-leaderboard-voice action:Enable` to re-enable.",
                    ephemeral=True
                )
                return
            
            # Handle enable action
            if action.value == "enable":
                if not config:
                    await interaction.followup.send(
                        "⚠️ Voice leaderboard not configured yet!\n"
                        "💡 Use `/live-leaderboard-voice action:Setup` first.",
                        ephemeral=True
                    )
                    return
                
                if config.get('voice_enabled'):
                    await interaction.followup.send("⚠️ Voice leaderboard is already enabled!", ephemeral=True)
                    return
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': {'voice_enabled': True}}
                )
                
                channel_id = config.get('voice_channel_id')
                channel_mention = f"<#{channel_id}>" if channel_id else "Not set"
                
                await interaction.followup.send(
                    f"✅ **Voice leaderboard enabled!**\n"
                    f"📊 Channel: {channel_mention}\n"
                    f"🌍 Timezone: `{config.get('timezone', 'UTC')}`\n"
                    f"💡 Voice time tracking has resumed.",
                    ephemeral=True
                )
                return
            
            # Handle setup action
            if action.value == "setup":
                if not voice_channel:
                    await interaction.followup.send(
                        "❌ **Channel required for setup!**\n"
                        "Please provide a channel using the `voice_channel` parameter.",
                        ephemeral=True
                    )
                    return
                
                # Validate timezone
                try:
                    pytz.timezone(timezone)
                except pytz.exceptions.UnknownTimeZoneError:
                    await interaction.followup.send(
                        f"❌ Invalid timezone: `{timezone}`\n"
                        f"💡 Use IANA format (e.g., `America/New_York`, `Europe/London`, `Asia/Tokyo`)",
                        ephemeral=True
                    )
                    return
                
                await self._ensure_guild_config(interaction.guild.id)
                
                # Prepare update data
                update_data = {
                    'voice_enabled': True, 
                    'voice_channel_id': voice_channel.id, 
                    'timezone': timezone
                }
                
                # Add vibe_channel if provided
                if vibe_channel:
                    update_data['vibe_channel_id'] = vibe_channel.id
                
                await self.db.guild_configs.update_one(
                    {'guild_id': interaction.guild.id},
                    {'$set': update_data}
                )
                
                await self._create_full_leaderboard_message(voice_channel, interaction.guild.id, vibe_channel.id if vibe_channel else None)
                
                vibe_info = f"\n🎵 Vibe Channel: {vibe_channel.mention}" if vibe_channel else ""
                await interaction.followup.send(
                    f"✅ **Voice leaderboard setup complete!**\n"
                    f"📊 Channel: {voice_channel.mention}\n"
                    f"🌍 Timezone: `{timezone}`{vibe_info}\n"
                    f"🔄 Updates every 5 minutes\n"
                    f"💡 Use `/live-leaderboard-voice action:Disable` to pause tracking.",
                    ephemeral=True
                )
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceLeaderboardCog(bot))
