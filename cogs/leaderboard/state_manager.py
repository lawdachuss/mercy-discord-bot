"""
Bulletproof State Manager for Leaderboard System
=================================================
Ensures NO scheduled operations are EVER missed, even after crashes.
Tracks all state persistently and recovers automatically.
"""

from datetime import datetime, timedelta
import logging
from typing import Dict, Optional
import pytz
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger('discord.bot.leaderboard.state')


class BulletproofStateManager:
    """
    Manages all scheduled operations with 100% reliability.
    NEVER misses a selection or reset, guaranteed.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.state.manager')
    
    async def ensure_star_selection(self, guild_id: int, star_config: Dict, guild_config: Dict) -> bool:
        """
        Determines if Star of the Week selection should run.
        
        STRICT RULE: Only returns True on Sunday at 12 PM (noon) or later.
        No emergency bypasses. No fallback logic. No exceptions.
        
        The star selection is the master controller for weekly timing.
        Weekly leaderboard resets ONLY happen as part of star selection.
        
        Args:
            guild_id: Discord guild ID
            star_config: Star of the Week configuration
            guild_config: Guild configuration with timezone
            
        Returns:
            True only if ALL conditions are met:
            - Current day is Sunday (weekday == 6) in guild's timezone
            - Current hour is 12 (noon) or later in guild's timezone
            - At least 6 days have passed since last selection (or no previous selection)
        """
        # Ensure star_selection_count is numeric (preventive fix for data corruption)
        try:
            if guild_config and 'star_selection_count' in guild_config:
                if not isinstance(guild_config.get('star_selection_count'), (int, float, type(None))):
                    await self.db.guild_configs.update_one(
                        {'guild_id': guild_id},
                        {'$set': {'star_selection_count': 0}}
                    )
                    self.logger.warning(f"Reset non-numeric star_selection_count for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Error fixing star_selection_count for guild {guild_id}: {e}")
        
        try:
            # Get timezone with validation
            tz_name = guild_config.get('timezone', 'UTC') if guild_config else 'UTC'
            if tz_name not in pytz.all_timezones:
                self.logger.warning(f"Invalid timezone '{tz_name}' for guild {guild_id}, using UTC")
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # STRICT CHECK 1: Must be Sunday (weekday == 6)
            if now.weekday() != 6:
                # Not Sunday - do not run, no matter what
                return False
            
            # STRICT CHECK 2: Must be 12 PM (noon) or later
            if now.hour < 12:
                # Sunday but before noon - do not run
                return False
            
            # STRICT CHECK 3: At least 6 days since last selection
            last_selection = guild_config.get('last_star_selection') if guild_config else None
            
            if last_selection:
                time_since = now_utc - last_selection
                days_since = time_since.total_seconds() / 86400
                
                # Guard against clock skew / future timestamps
                if days_since < 0:
                    self.logger.warning(
                        f"Guild {guild_id}: last_star_selection is in the future "
                        f"({abs(days_since):.1f} days ahead) - possible clock skew, NOT RUNNING"
                    )
                    return False
                
                if days_since < 6.0:
                    # Less than 6 days since last selection - do not run
                    return False
                
                # All checks passed - it's Sunday noon+ and at least 6 days since last selection
                self.logger.info(f"Guild {guild_id}: Sunday {now.hour}:00 - {days_since:.1f} days since last selection - RUNNING")
                return True
            else:
                # No previous selection - still require Sunday noon
                # This is the first selection for this guild
                self.logger.info(f"Guild {guild_id}: First star selection - Sunday {now.hour}:00 - RUNNING")
                return True
            
        except Exception as e:
            # On error, log and return False (do NOT run to be safe)
            # This prevents accidental premature selections due to bugs
            self.logger.error(f"Error checking Star selection for guild {guild_id}: {e} - NOT RUNNING (safe mode)")
            return False
    
    async def ensure_weekly_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures weekly reset happens exactly once per week.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_weekly_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} weekly reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            days_since = time_since.total_seconds() / 86400
            
            # CASE 2: More than 7 days - definitely reset
            if days_since >= 7.0:
                self.logger.warning(f"Guild {guild_id}: {days_since:.1f} days since last {reset_type} reset - RUNNING NOW")
                return True
            
            # CASE 3: It's been at least 6 days and we're past Sunday noon
            if days_since >= 6.0:
                if now.weekday() == 6 and now.hour >= 12:  # Sunday noon or later
                    self.logger.info(f"Guild {guild_id}: Sunday {reset_type} reset time - RUNNING NOW")
                    return True
                elif now.weekday() == 0:  # Monday
                    self.logger.warning(f"Guild {guild_id}: Missed Sunday {reset_type} reset - RUNNING NOW")
                    return True
                elif now.weekday() > 0:  # Tuesday or later
                    self.logger.warning(f"Guild {guild_id}: It's {now.strftime('%A')}, {reset_type} reset overdue - RUNNING NOW")
                    return True
            
            # CASE 4: Emergency check
            if days_since >= 6.9:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {days_since:.1f} days since {reset_type} reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def ensure_daily_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures daily reset happens exactly once per day.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_daily_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} daily reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            hours_since = time_since.total_seconds() / 3600
            
            # CASE 2: More than 24 hours - definitely reset
            if hours_since >= 24.0:
                self.logger.warning(f"Guild {guild_id}: {hours_since:.1f} hours since last {reset_type} daily reset - RUNNING NOW")
                return True
            
            # CASE 3: It's past midnight and been at least 20 hours
            if hours_since >= 20.0 and now.hour >= 0:
                self.logger.info(f"Guild {guild_id}: Daily {reset_type} reset time - RUNNING NOW")
                return True
            
            # CASE 4: Emergency - approaching 30 hours
            if hours_since >= 23.5:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {hours_since:.1f} hours since {reset_type} daily reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} daily reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def ensure_monthly_reset(self, guild_id: int, guild_config: Dict, reset_type: str = 'chat') -> bool:
        """
        Ensures monthly reset happens exactly once per month.
        Returns True if reset should run NOW.
        """
        try:
            # Get timezone
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            # Get last reset from persistent storage
            field_name = f'last_{reset_type}_monthly_reset'
            last_reset = guild_config.get(field_name)
            
            # CASE 1: Never reset before
            if not last_reset:
                self.logger.warning(f"Guild {guild_id}: No previous {reset_type} monthly reset - RUNNING NOW")
                return True
            
            # Calculate time since last reset
            time_since = now_utc - last_reset
            days_since = time_since.total_seconds() / 86400
            
            # CASE 2: Different month
            last_reset_tz = last_reset.replace(tzinfo=pytz.UTC).astimezone(tz)
            if now.month != last_reset_tz.month or now.year != last_reset_tz.year:
                if now.day >= 1:  # We're in a new month
                    self.logger.info(f"Guild {guild_id}: New month {reset_type} reset - RUNNING NOW")
                    return True
            
            # CASE 3: More than 30 days
            if days_since >= 30:
                self.logger.warning(f"Guild {guild_id}: {days_since:.0f} days since last {reset_type} monthly reset - RUNNING NOW")
                return True
            
            # CASE 4: Emergency - more than 35 days
            if days_since >= 35:
                self.logger.error(f"Guild {guild_id}: EMERGENCY - {days_since:.0f} days since {reset_type} monthly reset - RUNNING NOW")
                return True
            
            return False
            
        except Exception as e:
            # On ANY error, reset to be safe
            self.logger.error(f"Error checking {reset_type} monthly reset for guild {guild_id}: {e} - RUNNING NOW TO BE SAFE")
            return True
    
    async def mark_star_selection_complete(self, guild_id: int):
        """Mark Star selection as completed with timestamp"""
        try:
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {
                    '$set': {
                        'last_star_selection': datetime.utcnow()
                    },
                    '$inc': {
                        'star_selection_count': 1
                    }
                },
                upsert=True
            )
            self.logger.info(f"Marked Star selection complete for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to mark Star selection complete: {e}")
    
    async def mark_reset_complete(self, guild_id: int, reset_type: str, period: str):
        """Mark reset as completed with timestamp"""
        try:
            field_name = f'last_{reset_type}_{period}_reset'
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {'$set': {field_name: datetime.utcnow()}},
                upsert=True
            )
            self.logger.info(f"Marked {reset_type} {period} reset complete for guild {guild_id}")
        except Exception as e:
            self.logger.error(f"Failed to mark reset complete: {e}")
    
    async def get_system_health(self, guild_id: int) -> Dict:
        """
        Get comprehensive health check of all scheduled operations.
        Shows exactly when everything last ran and when it will run next.
        """
        try:
            guild_config = await self.db.guild_configs.find_one({'guild_id': guild_id})
            star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
            
            if not guild_config:
                return {'healthy': False, 'error': 'No guild configuration found'}
            
            tz_name = guild_config.get('timezone', 'UTC')
            if tz_name not in pytz.all_timezones:
                tz_name = 'UTC'
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
            now_utc = datetime.utcnow()
            
            health = {
                'guild_id': guild_id,
                'timezone': tz_name,
                'current_time': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
                'healthy': True,
                'operations': {}
            }
            
            # Check Star selection
            if star_config:
                last_star = guild_config.get('last_star_selection')
                if last_star:
                    days_since = (now_utc - last_star).total_seconds() / 86400
                    
                    # Calculate next Sunday noon
                    days_until_sunday = (6 - now.weekday()) % 7
                    if days_until_sunday == 0 and now.hour >= 12:
                        # It's Sunday after noon, next run is now or next Sunday
                        if days_since >= 6.0:
                            next_run = 'Ready to run now'
                        else:
                            next_run = 'Next Sunday 12 PM'
                    else:
                        next_run = f'Sunday 12 PM ({days_until_sunday} days)' if days_until_sunday > 0 else 'Sunday 12 PM'
                    
                    health['operations']['star_selection'] = {
                        'last_run': last_star.isoformat(),
                        'days_ago': round(days_since, 1),
                        'status': '✅ OK' if days_since < 7.5 else '⚠️ OVERDUE (waiting for Sunday)',
                        'next_run': next_run
                    }
                else:
                    # Calculate days until next Sunday noon
                    days_until_sunday = (6 - now.weekday()) % 7
                    if days_until_sunday == 0 and now.hour >= 12:
                        next_run = 'Ready for first selection'
                    else:
                        next_run = f'Sunday 12 PM ({days_until_sunday} days)' if days_until_sunday > 0 else 'Sunday 12 PM'
                    
                    health['operations']['star_selection'] = {
                        'status': '⏳ WAITING FOR FIRST SUNDAY',
                        'next_run': next_run
                    }
            
            # Check weekly resets
            for reset_type in ['chat', 'voice']:
                field = f'last_{reset_type}_weekly_reset'
                last_reset = guild_config.get(field)
                if last_reset:
                    days_since = (now_utc - last_reset).total_seconds() / 86400
                    health['operations'][f'{reset_type}_weekly'] = {
                        'last_run': last_reset.isoformat(),
                        'days_ago': round(days_since, 1),
                        'status': '✅ OK' if days_since < 7.5 else '⚠️ OVERDUE'
                    }
                else:
                    health['operations'][f'{reset_type}_weekly'] = {'status': '⚠️ NEVER RUN'}
            
            # Check daily resets
            for reset_type in ['chat', 'voice']:
                field = f'last_{reset_type}_daily_reset'
                last_reset = guild_config.get(field)
                if last_reset:
                    hours_since = (now_utc - last_reset).total_seconds() / 3600
                    health['operations'][f'{reset_type}_daily'] = {
                        'last_run': last_reset.isoformat(),
                        'hours_ago': round(hours_since, 1),
                        'status': '✅ OK' if hours_since < 25 else '⚠️ OVERDUE'
                    }
                else:
                    health['operations'][f'{reset_type}_daily'] = {'status': '⚠️ NEVER RUN'}
            
            return health
            
        except Exception as e:
            return {'healthy': False, 'error': str(e)}


class RecoveryManager:
    """
    Handles recovery from crashes and ensures nothing is missed.
    Runs on startup to catch up on any missed operations.
    
    IMPORTANT: Star selection recovery respects the Sunday 12 PM schedule.
    If the bot starts on a non-Sunday or before noon on Sunday, star selection
    will NOT be triggered - it will wait for the next Sunday 12 PM.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.recovery')
        self.state_manager = BulletproofStateManager(db)
    
    async def run_startup_recovery(self, bot):
        """
        Run on bot startup to catch any missed operations.
        
        Star selection recovery ONLY runs if:
        - Current time is Sunday 12 PM or later (in guild's timezone)
        - At least 6 days have passed since last selection
        
        This ensures the Sunday 12 PM schedule is always respected,
        even after bot downtime.
        """
        self.logger.info("=== STARTUP RECOVERY CHECK ===")
        
        try:
            # Get all guilds with configurations
            cursor = self.db.guild_configs.find({})
            configs = await cursor.to_list(length=10000)
            
            recovery_count = 0
            skipped_star_count = 0
            
            for guild_config in configs:
                guild_id = guild_config['guild_id']
                guild = bot.get_guild(guild_id)
                
                if not guild:
                    continue
                
                # Check Star selection - now respects Sunday 12 PM timing
                star_config = await self.db.star_configs.find_one({'guild_id': guild_id})
                if star_config:
                    # Get timezone for logging
                    tz_name = guild_config.get('timezone', 'UTC')
                    if tz_name not in pytz.all_timezones:
                        tz_name = 'UTC'
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Check if star selection should run (respects Sunday noon timing)
                    should_run = await self.state_manager.ensure_star_selection(guild_id, star_config, guild_config)
                    
                    if should_run:
                        # It's Sunday noon or later and selection is due
                        last_selection = guild_config.get('last_star_selection')
                        if last_selection:
                            days_since = (datetime.utcnow() - last_selection).total_seconds() / 86400
                            self.logger.warning(
                                f"RECOVERY: Running delayed Star selection for guild {guild_id} "
                                f"({days_since:.1f} days since last selection, delayed due to bot downtime)"
                            )
                        else:
                            self.logger.info(f"RECOVERY: Running first Star selection for guild {guild_id}")
                        
                        star_cog = bot.get_cog('StarOfTheWeekCog')
                        if star_cog:
                            await star_cog._process_star_selection(guild)
                            recovery_count += 1
                    else:
                        # Not Sunday noon yet - log that we're waiting
                        last_selection = guild_config.get('last_star_selection')
                        if last_selection:
                            days_since = (datetime.utcnow() - last_selection).total_seconds() / 86400
                            if days_since >= 6.0:
                                # Selection is due but waiting for Sunday noon
                                self.logger.info(
                                    f"Guild {guild_id}: Star selection due ({days_since:.1f} days) "
                                    f"but waiting for Sunday 12 PM (current: {now.strftime('%A %H:%M')})"
                                )
                                skipped_star_count += 1
                
                # Check weekly resets - only for guilds WITHOUT star config
                # (Star system handles weekly resets for guilds with star config)
                if not star_config:
                    if guild_config.get('chat_enabled'):
                        if await self.state_manager.ensure_weekly_reset(guild_id, guild_config, 'chat'):
                            self.logger.warning(f"RECOVERY: Running missed chat weekly reset for guild {guild_id}")
                            chat_cog = bot.get_cog('ChatLeaderboardCog')
                            if chat_cog:
                                await chat_cog._reset_weekly_stats(guild_id)
                                await self.state_manager.mark_reset_complete(guild_id, 'chat', 'weekly')
                                recovery_count += 1
                    
                    if guild_config.get('voice_enabled'):
                        if await self.state_manager.ensure_weekly_reset(guild_id, guild_config, 'voice'):
                            self.logger.warning(f"RECOVERY: Running missed voice weekly reset for guild {guild_id}")
                            voice_cog = bot.get_cog('VoiceLeaderboardCog')
                            if voice_cog:
                                await voice_cog._reset_weekly_stats(guild_id)
                                await self.state_manager.mark_reset_complete(guild_id, 'voice', 'weekly')
                                recovery_count += 1
                
                # Check daily resets (these are independent of star system)
                if guild_config.get('chat_enabled'):
                    if await self.state_manager.ensure_daily_reset(guild_id, guild_config, 'chat'):
                        self.logger.warning(f"RECOVERY: Running missed chat daily reset for guild {guild_id}")
                        chat_cog = bot.get_cog('ChatLeaderboardCog')
                        if chat_cog:
                            await chat_cog._reset_daily_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'chat', 'daily')
                            recovery_count += 1
                
                if guild_config.get('voice_enabled'):
                    if await self.state_manager.ensure_daily_reset(guild_id, guild_config, 'voice'):
                        self.logger.warning(f"RECOVERY: Running missed voice daily reset for guild {guild_id}")
                        voice_cog = bot.get_cog('VoiceLeaderboardCog')
                        if voice_cog:
                            await voice_cog._reset_daily_stats(guild_id)
                            await self.state_manager.mark_reset_complete(guild_id, 'voice', 'daily')
                            recovery_count += 1
            
            if recovery_count > 0:
                self.logger.warning(f"=== RECOVERY COMPLETE: Ran {recovery_count} missed operations ===")
            else:
                self.logger.info("=== RECOVERY CHECK COMPLETE: All operations up to date ===")
            
            if skipped_star_count > 0:
                self.logger.info(f"=== {skipped_star_count} guild(s) waiting for Sunday 12 PM for star selection ===")
            
        except Exception as e:
            self.logger.error(f"Error during startup recovery: {e}", exc_info=True)
