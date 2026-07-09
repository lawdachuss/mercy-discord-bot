"""
Star of the Week Cog
====================
Selects and announces the most active member each week based on combined chat + voice activity.

Features:
- Weekly selection every Sunday 12 PM (noon, guild timezone) - runs WITH weekly reset
- Combined scoring: chat messages + voice minutes
- Configurable weights per guild
- Auto role assignment/removal (removes from previous winner first)
- Automatic weekly leaderboard reset after selection
- DM winner with styled embed
- Optional public announcement
- History tracking in MongoDB
- Tie-breaker: voice minutes > chat messages
- Bot users are automatically excluded

Selection Process:
1. Calculate scores for all active members
2. Select winner with highest score
3. Remove Star role from previous winner (if any)
4. Assign Star role to new winner
5. Save winner to history
6. Notify winner via DM and optional announcement
7. Reset weekly stats for both chat and voice leaderboards
8. Archive old stats to database

Admin Commands (consolidated under /star):
- /star setup role:@role announce_channel:#channel weight_chat:1.0 weight_voice:2.0
- /star history limit:5
- /star preview

Author: Production-Ready Discord Bot
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
import asyncio
import os
from typing import Optional, Dict, List
import pytz
from dotenv import load_dotenv
import logging
from .state_manager import BulletproofStateManager, RecoveryManager
from .leaderboard_config import Emojis, Images
from .validators import DataValidator

load_dotenv()


class RoleAssignmentManager:
    """
    Manages Star of the Week role assignment with verification and error handling.
    """
    
    def __init__(self, bot, logger):
        self.bot = bot
        self.logger = logger
    
    async def verify_role_permissions(self, guild: discord.Guild, role_id: int) -> tuple[bool, str]:
        """
        Verify bot has permissions to assign the role.
        
        Args:
            guild: Discord guild
            role_id: Role ID to check
            
        Returns:
            (success, error_message)
        """
        # Check if role exists
        role = guild.get_role(role_id)
        if not role:
            return False, f"Role with ID {role_id} not found in guild"
        
        # Get bot member
        bot_member = guild.get_member(self.bot.user.id)
        if not bot_member:
            return False, "Bot member not found in guild"
        
        # Check if bot has Manage Roles permission
        if not bot_member.guild_permissions.manage_roles:
            return False, "Bot lacks 'Manage Roles' permission. Please grant this permission in Server Settings > Roles."
        
        # Check role hierarchy
        bot_top_role = bot_member.top_role
        if bot_top_role.position <= role.position:
            return False, (
                f"Role hierarchy error: Bot's highest role '{bot_top_role.name}' (position {bot_top_role.position}) "
                f"must be ABOVE the Star role '{role.name}' (position {role.position}). "
                f"Please move the bot's role above the Star role in Server Settings > Roles."
            )
        
        return True, ""
    
    async def assign_star_role(self, guild: discord.Guild, user_id: int, role_id: int, max_retries: int = 3) -> tuple[bool, str]:
        """
        Assign Star role to user with verification.
        
        Args:
            guild: Discord guild
            user_id: User ID to assign role to
            role_id: Role ID to assign
            max_retries: Maximum retry attempts
            
        Returns:
            (success, message)
        """
        # Verify permissions first
        can_assign, error_msg = await self.verify_role_permissions(guild, role_id)
        if not can_assign:
            self.logger.error(f"[STAR ROLE] Permission check failed: {error_msg}")
            return False, error_msg
        
        role = guild.get_role(role_id)
        
        # Get member (try cache first, then API)
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
                self.logger.info(f"[STAR ROLE] Fetched member {user_id} from API")
            except discord.NotFound:
                return False, f"User {user_id} not found in guild"
            except discord.HTTPException as e:
                return False, f"Failed to fetch user: {e}"
        
        # Check if member already has the role
        if role in member.roles:
            self.logger.info(f"[STAR ROLE] Member {member.display_name} already has role {role.name}")
            return True, "User already has the role"
        
        # Attempt to assign role with retry logic
        for attempt in range(max_retries):
            try:
                await member.add_roles(role, reason="Star of the Week winner")
                
                # Verify role was actually added
                await asyncio.sleep(0.5)
                updated_member = guild.get_member(user_id)
                if not updated_member:
                    try:
                        updated_member = await guild.fetch_member(user_id)
                    except:
                        # Assume success if we can't verify
                        self.logger.warning(f"[STAR ROLE] Could not verify role assignment for {member.display_name}")
                        return True, "Role assigned (verification skipped)"
                
                if role in updated_member.roles:
                    self.logger.info(f"[STAR ROLE] Successfully assigned role {role.name} to {member.display_name}")
                    return True, "Role assigned successfully"
                else:
                    if attempt < max_retries - 1:
                        self.logger.warning(f"[STAR ROLE] Role not found after assignment, retrying... (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(1)
                        continue
                    return False, "Role assignment could not be verified"
                    
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = float(e.response.headers.get('Retry-After', 1))
                    self.logger.warning(f"[STAR ROLE] Rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_after)
                elif e.status == 403:  # Forbidden
                    return False, f"Permission denied: {e}"
                else:
                    if attempt < max_retries - 1:
                        self.logger.warning(f"[STAR ROLE] HTTP error, retrying... (attempt {attempt + 1}/{max_retries}): {e}")
                        await asyncio.sleep(1)
                    else:
                        return False, f"HTTP error: {e}"
            except Exception as e:
                self.logger.error(f"[STAR ROLE] Unexpected error assigning role: {e}", exc_info=True)
                return False, f"Unexpected error: {e}"
        
        return False, "Failed after all retry attempts"
    
    async def remove_star_role(self, guild: discord.Guild, user_id: int, role_id: int, max_retries: int = 3) -> tuple[bool, str]:
        """
        Remove Star role from user.
        
        Args:
            guild: Discord guild
            user_id: User ID to remove role from
            role_id: Role ID to remove
            max_retries: Maximum retry attempts
            
        Returns:
            (success, message)
        """
        role = guild.get_role(role_id)
        if not role:
            return False, f"Role {role_id} not found"
        
        # Get member (try cache first, then API)
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                # User left the server, that's okay
                self.logger.info(f"[STAR ROLE] Previous winner {user_id} has left the server")
                return True, "User left server (role removal skipped)"
            except discord.HTTPException as e:
                return False, f"Failed to fetch user: {e}"
        
        # Check if member has the role
        if role not in member.roles:
            self.logger.info(f"[STAR ROLE] Member {member.display_name} doesn't have role {role.name}")
            return True, "User doesn't have the role"
        
        # Attempt to remove role with retry logic
        for attempt in range(max_retries):
            try:
                await member.remove_roles(role, reason="Star of the Week expired")
                
                # Verify role was actually removed
                await asyncio.sleep(0.5)
                updated_member = guild.get_member(user_id)
                if not updated_member:
                    try:
                        updated_member = await guild.fetch_member(user_id)
                    except:
                        # Assume success if we can't verify
                        return True, "Role removed (verification skipped)"
                
                if role not in updated_member.roles:
                    self.logger.info(f"[STAR ROLE] Successfully removed role {role.name} from {member.display_name}")
                    return True, "Role removed successfully"
                else:
                    if attempt < max_retries - 1:
                        self.logger.warning(f"[STAR ROLE] Role still present after removal, retrying... (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(1)
                        continue
                    return False, "Role removal could not be verified"
                    
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = float(e.response.headers.get('Retry-After', 1))
                    self.logger.warning(f"[STAR ROLE] Rate limited, waiting {retry_after}s")
                    await asyncio.sleep(retry_after)
                elif e.status == 403:  # Forbidden
                    return False, f"Permission denied: {e}"
                else:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        return False, f"HTTP error: {e}"
            except Exception as e:
                self.logger.error(f"[STAR ROLE] Unexpected error removing role: {e}", exc_info=True)
                return False, f"Unexpected error: {e}"
        
        return False, "Failed after all retry attempts"


class StarOfTheWeekCog(commands.Cog):
    """
    Star of the Week System
    Automatically selects and rewards the most active member weekly.
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.state_manager = None
        self.recovery_manager = None
        self.role_manager = RoleAssignmentManager(bot, logging.getLogger('discord.bot.star_of_the_week.roles'))
        self.logger = logging.getLogger('discord.bot.star_of_the_week')
    
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
        """Initialize MongoDB connection on cog load"""
        # Reuse bot's shared MongoDB connection
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.mongo_client = self.bot.mongo_client
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Star of the Week Cog: Reusing shared MongoDB connection")
        else:
            # Fallback: create new connection if shared one doesn't exist
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                raise ValueError("MONGO_URL not found in environment variables")
            
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            self.db = self.mongo_client['poison_bot']
            self.logger.info("Star of the Week Cog: MongoDB connected")
        
        # Create indexes for optimal performance
        await self._create_indexes()
        
        # Initialize state manager
        self.state_manager = BulletproofStateManager(self.db)
        self.recovery_manager = RecoveryManager(self.db)
        
        # Start task if bot is already ready (e.g., during cog reload)
        # Otherwise, on_ready event will start it
        if self.bot.is_ready():
            if not self.weekly_star_selection.is_running():
                self.weekly_star_selection.start()
                self.logger.info("Star of the Week task started (bot already ready)")
            # Run recovery check on startup
            await self.recovery_manager.run_startup_recovery(self.bot)
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Start tasks when bot is ready to avoid deadlock during cog loading"""
        if not self.weekly_star_selection.is_running():
            self.weekly_star_selection.start()
            self.logger.info("Star of the Week task started (on_ready event)")
        
        # Run recovery check to catch any missed selections
        if self.recovery_manager:
            await self.recovery_manager.run_startup_recovery(self.bot)
    
    async def cog_unload(self):
        """Cleanup on cog unload"""
        self.weekly_star_selection.cancel()
        # Don't close shared MongoDB connection - it's managed by the bot
        # Only close if we created our own connection
        if self.mongo_client is not None and not hasattr(self.bot, 'mongo_client'):
            self.mongo_client.close()
    
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Clean up data when bot is removed from a guild"""
        try:
            self.logger.info(f"Bot removed from guild {guild.name} ({guild.id}), cleaning up data")
            
            # Delete star config
            await self.db.star_configs.delete_one({'guild_id': guild.id})
            
            # Delete star history
            result = await self.db.star_history.delete_many({'guild_id': guild.id})
            self.logger.info(f"Deleted {result.deleted_count} star history records for guild {guild.id}")
            
            self.logger.info(f"Star system cleanup complete for guild {guild.id}")
        except Exception as e:
            self.logger.error(f"Error cleaning up star data for guild {guild.id}: {e}", exc_info=True)
    
    # ==================== DATABASE HELPERS ====================
    
    async def _create_indexes(self):
        """Create database indexes for star of the week collections"""
        try:
            # Star configs indexes
            await self.db.star_configs.create_index('guild_id', unique=True)
            
            # Star history indexes
            await self.db.star_history.create_index([('guild_id', 1), ('awarded_at', -1)])
            await self.db.star_history.create_index([('user_id', 1)])
            
            self.logger.info("Star of the Week: Database indexes created/verified")
        except Exception as e:
            self.logger.warning(f"Error creating indexes (may already exist): {e}")
    
    async def _get_guild_config(self, guild_id: int) -> Optional[Dict]:
        """Fetch guild configuration from MongoDB"""
        return await self.db.guild_configs.find_one({'guild_id': guild_id})
    
    async def _get_star_config(self, guild_id: int) -> Optional[Dict]:
        """Get Star of the Week configuration for a guild"""
        return await self.db.star_configs.find_one({'guild_id': guild_id})
    
    async def _save_star_config(self, guild_id: int, role_id: int, 
                               announce_channel_id: Optional[int],
                               weight_chat: float, weight_voice: float):
        """Save Star of the Week configuration"""
        await self.db.star_configs.update_one(
            {'guild_id': guild_id},
            {
                '$set': {
                    'role_id': role_id,
                    'announce_channel_id': announce_channel_id,
                    'weight_chat': weight_chat,
                    'weight_voice': weight_voice,
                    'last_update': datetime.utcnow()
                }
            },
            upsert=True
        )
    
    async def _get_previous_winner(self, guild_id: int) -> Optional[Dict]:
        """Get the most recent Star of the Week winner"""
        cursor = self.db.star_history.find(
            {'guild_id': guild_id}
        ).sort('awarded_at', -1).limit(1)
        
        results = await cursor.to_list(length=1)
        return results[0] if results else None
    
    async def _save_star_winner(self, guild_id: int, user_id: int, 
                               score: float, chat_count: int, voice_minutes: float):
        """Save Star of the Week winner to history"""
        await self.db.star_history.insert_one({
            'guild_id': guild_id,
            'user_id': user_id,
            'score': score,
            'chat_weekly': chat_count,
            'voice_weekly': voice_minutes,
            'awarded_at': datetime.utcnow()
        })
    
    async def _get_weekly_stats(self, guild_id: int) -> List[Dict]:
        """Get all users with weekly activity (excluding bots)"""
        # Get all stats with activity
        cursor = self.db.user_stats.find({
            'guild_id': guild_id,
            '$or': [
                {'chat_weekly': {'$gt': 0}},
                {'voice_weekly': {'$gt': 0}}
            ]
        })
        
        stats = await cursor.to_list(length=10000)
        
        # Filter out bots
        guild = self.bot.get_guild(guild_id)
        if guild:
            filtered_stats = []
            for stat in stats:
                # Try cache first, fall back to API if not found
                member = guild.get_member(stat['user_id'])
                if not member:
                    try:
                        member = await guild.fetch_member(stat['user_id'])
                    except discord.NotFound:
                        # User left the server
                        continue
                    except discord.HTTPException as e:
                        self.logger.warning(f"Failed to fetch member {stat['user_id']}: {e}")
                        continue
                
                if member and not member.bot:
                    filtered_stats.append(stat)
            return filtered_stats
        
        return stats
    
    # ==================== SCORING & SELECTION ====================
    
    def _calculate_score(self, chat_count: int, voice_minutes: float, 
                        weight_chat: float, weight_voice: float) -> float:
        """
        Calculate combined activity score with validation.
        
        Args:
            chat_count: Number of messages sent
            voice_minutes: Minutes spent in voice
            weight_chat: Weight multiplier for chat
            weight_voice: Weight multiplier for voice minutes
        
        Returns:
            Combined score
        """
        # FIX #17: Validate inputs to prevent negative scores and handle corrupted data
        chat_count = max(0, int(chat_count) if isinstance(chat_count, (int, float)) else 0)
        voice_minutes = max(0.0, float(voice_minutes) if isinstance(voice_minutes, (int, float)) else 0.0)
        
        # Validate weights (could be corrupted in database)
        if not isinstance(weight_chat, (int, float)) or weight_chat < 0:
            self.logger.warning(f"Invalid weight_chat: {weight_chat}, using 1.0")
            weight_chat = 1.0
        if not isinstance(weight_voice, (int, float)) or weight_voice < 0:
            self.logger.warning(f"Invalid weight_voice: {weight_voice}, using 2.0")
            weight_voice = 2.0
        
        weight_chat = max(0.0, min(1000.0, weight_chat))  # Cap at reasonable max
        weight_voice = max(0.0, min(1000.0, weight_voice))
        
        # Calculate score with float precision
        score = (chat_count * weight_chat) + (voice_minutes * weight_voice)
        return round(score, 2)
    
    async def _select_star_of_week(self, guild_id: int) -> Optional[Dict]:
        """
        Select Star of the Week based on combined activity.
        
        Returns:
            Dict with winner info: {user_id, score, chat_weekly, voice_weekly}
            None if no eligible users
        """
        # Get configuration
        star_config = await self._get_star_config(guild_id)
        if not star_config:
            self.logger.warning(f"No Star config for guild {guild_id}")
            return None
        
        weight_chat = star_config.get('weight_chat', 1.0)
        weight_voice = star_config.get('weight_voice', 2.0)
        
        # Get weekly stats
        stats = await self._get_weekly_stats(guild_id)
        
        if not stats:
            self.logger.info(f"No weekly activity for guild {guild_id}")
            return None
        
        # Calculate scores for all users
        scored_users = []
        for user_stat in stats:
            chat_count = user_stat.get('chat_weekly', 0)
            voice_minutes = user_stat.get('voice_weekly', 0)
            
            # Skip users with no activity
            if chat_count == 0 and voice_minutes == 0:
                continue
            
            score = self._calculate_score(chat_count, voice_minutes, weight_chat, weight_voice)
            
            scored_users.append({
                'user_id': user_stat['user_id'],
                'score': score,
                'chat_weekly': chat_count,
                'voice_weekly': voice_minutes
            })
        
        if not scored_users:
            return None
        
        # Sort by score (desc), then voice_minutes (desc), then chat (desc)
        scored_users.sort(
            key=lambda x: (x['score'], x['voice_weekly'], x['chat_weekly']),
            reverse=True
        )
        
        return scored_users[0]
    
    # ==================== ROLE MANAGEMENT ====================
    
    async def _reset_weekly_leaderboards(self, guild_id: int):
        """
        Reset weekly stats for both chat and voice leaderboards after Star selection.
        This archives the current week's data and resets counters to 0.
        
        Args:
            guild_id: Discord guild ID
        """
        try:
            self.logger.info(f"Resetting weekly leaderboards for guild {guild_id} after Star selection")
            
            # Archive and reset chat weekly stats
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'chat_weekly': {'$gt': 0}})
            chat_stats = await cursor.to_list(length=10000)
            if chat_stats:
                archive_doc = {
                    'guild_id': guild_id,
                    'type': 'chat',
                    'period': 'weekly',
                    'reset_date': datetime.utcnow(),
                    'reset_reason': 'star_of_the_week_selection',
                    'stats': chat_stats
                }
                await self.db.weekly_history.insert_one(archive_doc)
                self.logger.info(f"Archived {len(chat_stats)} chat weekly stats for guild {guild_id}")
            
            # Archive and reset voice weekly stats
            cursor = self.db.user_stats.find({'guild_id': guild_id, 'voice_weekly': {'$gt': 0}})
            voice_stats = await cursor.to_list(length=10000)
            if voice_stats:
                archive_doc = {
                    'guild_id': guild_id,
                    'type': 'voice',
                    'period': 'weekly',
                    'reset_date': datetime.utcnow(),
                    'reset_reason': 'star_of_the_week_selection',
                    'stats': voice_stats
                }
                await self.db.weekly_history.insert_one(archive_doc)
                self.logger.info(f"Archived {len(voice_stats)} voice weekly stats for guild {guild_id}")
            
            # Reset both chat_weekly and voice_weekly to 0
            result = await self.db.user_stats.update_many(
                {'guild_id': guild_id},
                {'$set': {'chat_weekly': 0, 'voice_weekly': 0}}
            )
            
            # Persist reset timestamps to database for coordination with other cogs
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {
                    '$set': {
                        'last_chat_weekly_reset': datetime.utcnow(),
                        'last_voice_weekly_reset': datetime.utcnow(),
                        'last_star_selection': datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            self.logger.info(f"Reset weekly stats for {result.modified_count} users in guild {guild_id}")
            
        except Exception as e:
            self.logger.error(f"Error resetting weekly leaderboards for guild {guild_id}: {e}", exc_info=True)
    
    async def _assign_star_role(self, guild: discord.Guild, user_id: int, role_id: int) -> bool:
        """
        Assign Star of the Week role to user and remove from previous winner.
        Uses RoleAssignmentManager for reliable role management.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.logger.info(f"[STAR ROLE] Starting role assignment for user {user_id} in guild {guild.name}")
            
            # Remove role from previous winner first
            previous_winner = await self._get_previous_winner(guild.id)
            if previous_winner:
                success, message = await self.role_manager.remove_star_role(guild, previous_winner['user_id'], role_id)
                if success:
                    self.logger.info(f"[STAR ROLE] {message}")
                else:
                    self.logger.warning(f"[STAR ROLE] Failed to remove from previous winner: {message}")
            
            # Assign role to new winner
            success, message = await self.role_manager.assign_star_role(guild, user_id, role_id)
            if success:
                self.logger.info(f"[STAR ROLE] {message}")
            else:
                self.logger.error(f"[STAR ROLE] Failed to assign role: {message}")
            
            return success
        
        except Exception as e:
            self.logger.error(f"Error assigning Star role in guild {guild.id}: {e}", exc_info=True)
            return False
    
    async def _add_role_with_retry(self, member: discord.Member, role: discord.Role, reason: str, max_retries: int = 3) -> bool:
        """Add role with retry logic for rate limits"""
        for attempt in range(max_retries):
            try:
                # Skip if member already has the role
                if role in member.roles:
                    self.logger.info(f"Member {member.display_name} already has role {role.name}")
                    return True
                
                await member.add_roles(role, reason=reason)
                
                # Verify the role was actually added by refetching the member
                await asyncio.sleep(0.5)  # Small delay to ensure Discord's cache updates
                updated_member = member.guild.get_member(member.id)
                if not updated_member:
                    # Fallback to API fetch
                    try:
                        updated_member = await member.guild.fetch_member(member.id)
                    except discord.HTTPException:
                        self.logger.warning(f"Could not verify role assignment for {member.display_name}")
                        return True  # Assume success if we can't verify
                
                if role in updated_member.roles:
                    self.logger.info(f"Successfully verified role {role.name} added to {member.display_name}")
                    return True
                else:
                    self.logger.warning(f"Role {role.name} not found on {member.display_name} after add_roles call (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    return False
                    
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = float(e.response.headers.get('Retry-After', 1))
                    self.logger.warning(f"Rate limited adding role, retrying after {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_after)
                elif e.status == 403:  # Forbidden
                    self.logger.error(f"Missing permissions to add role: {e}")
                    return False
                else:
                    self.logger.error(f"HTTP error adding role: {e}")
                    if attempt == max_retries - 1:
                        return False
                    await asyncio.sleep(1)
            except Exception as e:
                self.logger.error(f"Unexpected error adding role: {e}")
                return False
        return False
    
    async def _remove_role_with_retry(self, member: discord.Member, role: discord.Role, reason: str, max_retries: int = 3) -> bool:
        """Remove role with retry logic for rate limits"""
        for attempt in range(max_retries):
            try:
                # Skip if member doesn't have the role
                if role not in member.roles:
                    self.logger.info(f"Member {member.display_name} doesn't have role {role.name}, skipping removal")
                    return True
                
                await member.remove_roles(role, reason=reason)
                
                # Verify the role was actually removed by refetching the member
                await asyncio.sleep(0.5)  # Small delay to ensure Discord's cache updates
                updated_member = member.guild.get_member(member.id)
                if not updated_member:
                    # Fallback to API fetch
                    try:
                        updated_member = await member.guild.fetch_member(member.id)
                    except discord.HTTPException:
                        self.logger.warning(f"Could not verify role removal for {member.display_name}")
                        return True  # Assume success if we can't verify
                
                if role not in updated_member.roles:
                    self.logger.info(f"Successfully verified role {role.name} removed from {member.display_name}")
                    return True
                else:
                    self.logger.warning(f"Role {role.name} still on {member.display_name} after remove_roles call (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    return False
                    
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = float(e.response.headers.get('Retry-After', 1))
                    self.logger.warning(f"Rate limited removing role, retrying after {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_after)
                elif e.status == 403:  # Forbidden
                    self.logger.error(f"Missing permissions to remove role: {e}")
                    return False
                else:
                    self.logger.error(f"HTTP error removing role: {e}")
                    if attempt == max_retries - 1:
                        return False
                    await asyncio.sleep(1)
            except Exception as e:
                self.logger.error(f"Unexpected error removing role: {e}")
                return False
        return False
    
    # ==================== NOTIFICATIONS ====================
    
    def _create_winner_dm_embed(self, guild: discord.Guild, score: float, 
                                chat_count: int, voice_minutes: float) -> discord.Embed:
        """
        Create compact DM embed for the winner with custom emojis.
        
        Args:
            guild: Discord guild
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        
        Returns:
            Discord embed
        """
        # Format voice time
        if voice_minutes >= 60:
            voice_hours = int(voice_minutes // 60)
            voice_mins = int(voice_minutes % 60)
            voice_str = f"{voice_hours}h {voice_mins}m" if voice_mins > 0 else f"{voice_hours}h"
        else:
            voice_str = f"{int(voice_minutes)}m"
        
        # Create embed with custom emojis
        embed = discord.Embed(
            title=f"{Emojis.STAR} Star of the Week!",
            description=(
                f"Congrats! You're **{guild.name}'s** Star!\n\n"
                f"{Emojis.OGS_SYMBO} **Messages:** `{chat_count:,}`\n"
                f"{Emojis.OGS_SYMBO} **Voice:** `{voice_str}`\n"
                f"{Emojis.OGS_SYMBO} **Score:** `{score:.0f}`\n\n"
                f"{Emojis.HEARTSPARK} Keep being awesome!"
            ),
            color=0xFFD700,  # Gold color
            timestamp=datetime.utcnow()
        )
        
        # Only add thumbnail if guild has icon
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        embed.set_footer(text=guild.name, icon_url=Images.FOOTER_ICON)
        
        return embed
    
    def _create_announcement_embed(self, member: discord.Member, score: float,
                                   chat_count: int, voice_minutes: float) -> discord.Embed:
        """
        Create public announcement embed.
        
        Args:
            member: Winner member
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        
        Returns:
            Discord embed
        """
        # Format voice time
        voice_hours = int(voice_minutes // 60)
        voice_mins = int(voice_minutes % 60)
        voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
        
        embed = discord.Embed(
            title=f"{Emojis.STAR} Star of the Week Announcement",
            description=(
                f"{Emojis.HEARTSPARK} Congratulations to {member.mention} for being this week's **Star of the Week**!\n\n"
                f"**{Emojis.STARS} Weekly Activity:**\n"
                f"{Emojis.MOON} **Messages:** `{chat_count:,}`\n"
                f"{Emojis.MIC} **Voice Time:** `{voice_str}`\n"
                f"{Emojis.TROPHY} **Score:** `{score:.1f}`\n\n"
                f"Thank you for being such an active and valuable member of our community! {Emojis.CROW}"
            ),
            color=0xFFD700,
            timestamp=datetime.utcnow()
        )
        
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Keep being awesome!", icon_url=Images.FOOTER_ICON)
        
        return embed
    
    async def _notify_winner(self, guild: discord.Guild, user_id: int,
                            score: float, chat_count: int, voice_minutes: float):
        """
        Send DM to winner and optionally announce in channel.
        
        Args:
            guild: Discord guild
            user_id: Winner user ID
            score: Combined score
            chat_count: Weekly messages
            voice_minutes: Weekly voice minutes
        """
        member = guild.get_member(user_id)
        if not member:
            self.logger.error(f"Winner {user_id} not found in guild {guild.id}")
            return
        
        # Send DM to winner
        try:
            dm_embed = self._create_winner_dm_embed(guild, score, chat_count, voice_minutes)
            
            # Get vibe channel from guild config
            guild_config = await self._get_guild_config(guild.id)
            vibe_channel_id = guild_config.get('vibe_channel_id') if guild_config else None
            
            # Create button view if vibe channel exists
            if vibe_channel_id:
                vibe_channel = guild.get_channel(vibe_channel_id)
                if vibe_channel:
                    view = discord.ui.View()
                    # Use same emoji as leaderboard embeds
                    vibe_emoji = discord.PartialEmoji(name='original_Peek', id=1429151221939441776, animated=True)
                    button = discord.ui.Button(
                        style=discord.ButtonStyle.secondary,
                        label="Join the Vibe",
                        url=f"https://discord.com/channels/{guild.id}/{vibe_channel_id}",
                        emoji=vibe_emoji
                    )
                    view.add_item(button)
                    await member.send(embed=dm_embed, view=view)
                else:
                    await member.send(embed=dm_embed)
            else:
                await member.send(embed=dm_embed)
            
            self.logger.info(f"Sent Star DM to {member.display_name} in guild {guild.id}")
        except discord.Forbidden:
            self.logger.warning(f"Cannot DM {member.display_name} (DMs disabled) in guild {guild.id}")
        except Exception as e:
            self.logger.error(f"Error sending DM to winner in guild {guild.id}: {e}", exc_info=True)
        
        # Announce in channel if configured
        star_config = await self._get_star_config(guild.id)
        if star_config and star_config.get('announce_channel_id'):
            try:
                channel = guild.get_channel(star_config['announce_channel_id'])
                if channel:
                    announce_embed = self._create_announcement_embed(member, score, chat_count, voice_minutes)
                    await channel.send(embed=announce_embed)
                    self.logger.info(f"Announced Star in {channel.name} (guild {guild.id})")
                else:
                    # Channel was deleted - clear it from config
                    self.logger.warning(f"Announcement channel {star_config['announce_channel_id']} not found for guild {guild.id}, clearing from config")
                    await self.db.star_configs.update_one(
                        {'guild_id': guild.id},
                        {'$set': {'announce_channel_id': None}}
                    )
            except Exception as e:
                self.logger.error(f"Error announcing Star in guild {guild.id}: {e}", exc_info=True)
    
    # ==================== BACKGROUND TASKS ====================
    
    @tasks.loop(minutes=5)  # Check every 5 minutes for maximum reliability
    async def weekly_star_selection(self):
        """
        Check for weekly Star selection (Sunday 12 PM / noon guild time).
        Runs every 5 minutes to ensure selection happens at the right time.
        Selection happens at noon, then weekly leaderboards are reset immediately after.
        """
        try:
            # Get all guilds with Star config
            cursor = self.db.star_configs.find({})
            configs = await cursor.to_list(length=1000)
            
            if not configs:
                return
            
            for star_config in configs:
                guild_id = star_config['guild_id']
                guild = self.bot.get_guild(guild_id)
                
                if not guild:
                    self.logger.warning(f"Guild {guild_id} not found, skipping Star selection")
                    continue
                
                # Get guild timezone
                guild_config = await self._get_guild_config(guild_id)
                tz_name = guild_config.get('timezone', 'UTC') if guild_config else 'UTC'
                
                try:
                    # Validate timezone
                    tz_name = self._validate_timezone(tz_name, guild_id)
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Use the bulletproof state manager to check if selection should run
                    should_run_selection = await self.state_manager.ensure_star_selection(
                        guild_id, star_config, guild_config
                    )
                    
                    if should_run_selection:
                        # Select Star of the Week
                        self.logger.info(f"Triggering Star selection for guild {guild_id}")
                        success = await self._process_star_selection(guild)
                        # ALWAYS mark selection as complete to prevent infinite retries
                        # Even if it failed, we don't want to retry every 5 minutes
                        await self.state_manager.mark_star_selection_complete(guild_id)
                        if success:
                            self.logger.info(f"Star selection completed successfully for guild {guild_id}")
                        else:
                            self.logger.warning(f"Star selection failed or had no eligible users for guild {guild_id}, but marked as complete to prevent retries")
                
                except pytz.exceptions.UnknownTimeZoneError:
                    self.logger.error(f"Invalid timezone for guild {guild_id}: {tz_name}")
                except Exception as e:
                    self.logger.error(f"Error checking Star selection for guild {guild_id}: {e}", exc_info=True)
        
        except Exception as e:
            self.logger.error(f"Error in weekly_star_selection task: {e}", exc_info=True)
    
    @weekly_star_selection.before_loop
    async def before_weekly_star_selection(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()
    
    async def _process_star_selection(self, guild: discord.Guild):
        """
        Process Star of the Week selection for a guild.
        
        This process is transactional - if any critical step fails, the entire
        selection is aborted to prevent data loss.
        
        Args:
            guild: Discord guild
        """
        try:
            self.logger.info(f"Processing Star of the Week selection for {guild.name} (ID: {guild.id})")
            
            # Select winner
            winner = await self._select_star_of_week(guild.id)
            
            if not winner:
                self.logger.info(f"No eligible users for Star of the Week in {guild.name} (no activity this week)")
                return True  # Return True to mark as complete (not an error, just no activity)
            
            # Get Star config
            star_config = await self._get_star_config(guild.id)
            if not star_config:
                self.logger.error(f"No Star config found for guild {guild.id}")
                return False  # Explicitly return False instead of None
            
            # Step 1: Assign role (CRITICAL - must succeed)
            # This removes role from previous winner and assigns to new winner
            role_assigned = await self._assign_star_role(
                guild, 
                winner['user_id'], 
                star_config['role_id']
            )
            
            if not role_assigned:
                self.logger.error(
                    f"❌ CRITICAL: Failed to assign Star role in {guild.name}. "
                    f"Aborting star selection to preserve leaderboard data. "
                    f"Please check role permissions and configuration, then try manual selection."
                )
                return False  # STOP HERE - don't reset leaderboards if role wasn't assigned
            
            # Step 2: Save to history (CRITICAL - must succeed)
            try:
                await self._save_star_winner(
                    guild.id,
                    winner['user_id'],
                    winner['score'],
                    winner['chat_weekly'],
                    winner['voice_weekly']
                )
            except Exception as e:
                self.logger.error(
                    f"❌ CRITICAL: Failed to save star winner to history in {guild.name}: {e}. "
                    f"Attempting rollback of role assignment."
                )
                # Rollback: Remove role from winner since we couldn't save to history
                try:
                    member = guild.get_member(winner['user_id'])
                    role = guild.get_role(star_config['role_id'])
                    if member and role and role in member.roles:
                        await member.remove_roles(role, reason="Star selection failed - rollback")
                        self.logger.info(f"Rolled back role assignment for {member.display_name}")
                except Exception as rollback_error:
                    self.logger.error(f"Failed to rollback role assignment: {rollback_error}")
                return False  # Abort - don't reset leaderboards
            
            # Step 3: Notify winner (NON-CRITICAL - can fail without aborting)
            try:
                await self._notify_winner(
                    guild,
                    winner['user_id'],
                    winner['score'],
                    winner['chat_weekly'],
                    winner['voice_weekly']
                )
            except Exception as e:
                self.logger.warning(
                    f"⚠️ Failed to notify star winner in {guild.name}: {e}. "
                    f"Continuing with reset (notification is not critical)."
                )
            
            # Step 4: Reset weekly leaderboards (ONLY if all critical steps succeeded)
            await self._reset_weekly_leaderboards(guild.id)
            
            self.logger.info(
                f"✅ Star of the Week complete for {guild.name}: "
                f"User {winner['user_id']} with score {winner['score']:.1f}. "
                f"Weekly leaderboards have been reset."
            )
            return True  # Selection successful
        
        except Exception as e:
            self.logger.error(f"Error processing Star selection for {guild.name}: {e}", exc_info=True)
            return False  # Selection failed
    
    # ==================== CONSOLIDATED SLASH COMMAND ====================
    
    star_group = app_commands.Group(name="star", description="Star of the Week management")
    
    @star_group.command(name="setup", description="Configure Star of the Week system")
    @app_commands.describe(
        role="Role to assign to Star of the Week",
        announce_channel="Channel to announce winner (optional)",
        weight_chat="Score weight for chat messages (default: 1.0)",
        weight_voice="Score weight per voice minute (default: 2.0)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_star(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        announce_channel: Optional[discord.TextChannel] = None,
        weight_chat: Optional[float] = 1.0,
        weight_voice: Optional[float] = 2.0
    ):
        """
        Configure Star of the Week system.
        
        Admin only command to set up automatic weekly winner selection.
        
        Scoring formula: score = (messages × weight_chat) + (voice_minutes × weight_voice)
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Validate weights
            if weight_chat < 0 or weight_voice < 0:
                await interaction.followup.send(
                    "❌ Weights must be positive numbers!",
                    ephemeral=True
                )
                return
            
            # Save configuration
            await self._save_star_config(
                interaction.guild.id,
                role.id,
                announce_channel.id if announce_channel else None,
                weight_chat,
                weight_voice
            )
            
            # Build response
            response = (
                f"✅ **Star of the Week configured!**\n\n"
                f"🏆 **Role:** {role.mention}\n"
                f"📢 **Announce Channel:** {announce_channel.mention if announce_channel else 'None (DM only)'}\n"
                f"📊 **Scoring Weights:**\n"
                f"   • Chat messages: `{weight_chat}` point(s) each\n"
                f"   • Voice minutes: `{weight_voice}` point(s) each\n\n"
                f"🗓️ **Selection:** Every Sunday at 12 PM / noon (guild timezone)\n"
                f"💡 **Tip:** Higher weights = more impact on score"
            )
            
            await interaction.followup.send(response, ephemeral=True)
            self.logger.info(f"Star of the Week configured for guild {interaction.guild.id} by {interaction.user}")
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error configuring Star of the Week: {e}",
                ephemeral=True
            )
            self.logger.error(f"Setup error for guild {interaction.guild.id}: {e}", exc_info=True)
    
    @star_group.command(name="history", description="View past Star of the Week winners")
    @app_commands.describe(limit="Number of past winners to show (default: 5)")
    async def star_history(
        self,
        interaction: discord.Interaction,
        limit: Optional[int] = 5
    ):
        """
        Display Star of the Week history.
        
        Shows past winners with their scores and stats.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch history
            cursor = self.db.star_history.find(
                {'guild_id': interaction.guild.id}
            ).sort('awarded_at', -1).limit(min(limit, 20))
            
            history = await cursor.to_list(length=20)
            
            if not history:
                await interaction.followup.send(
                    "📜 No Star of the Week history yet!",
                    ephemeral=True
                )
                return
            
            # Build embed
            embed = discord.Embed(
                title="⭐ Star of the Week History",
                description=f"Past {len(history)} winner(s)",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )
            
            for idx, winner in enumerate(history, 1):
                member = interaction.guild.get_member(winner['user_id'])
                username = member.mention if member else f"<@{winner['user_id']}>"
                
                voice_hours = int(winner['voice_weekly'] // 60)
                voice_mins = int(winner['voice_weekly'] % 60)
                voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
                
                awarded_timestamp = int(winner['awarded_at'].timestamp())
                
                embed.add_field(
                    name=f"#{idx} • {username}",
                    value=(
                        f"🗓️ <t:{awarded_timestamp}:R>\n"
                        f"💬 {winner['chat_weekly']:,} messages\n"
                        f"🎤 {voice_str}\n"
                        f"🏆 Score: {winner['score']:.1f}"
                    ),
                    inline=True
                )
            
            embed.set_footer(
                text=f"{interaction.guild.name} • Star History",
                icon_url=interaction.guild.icon.url if interaction.guild.icon else None
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error fetching history: {e}",
                ephemeral=True
            )
    
    @star_group.command(name="diagnose", description="[ADMIN] Check Star of the Week system configuration and permissions")
    @app_commands.checks.has_permissions(administrator=True)
    async def star_diagnose(self, interaction: discord.Interaction):
        """
        Diagnose Star of the Week system configuration.
        
        Checks permissions, role hierarchy, and configuration.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            issues = []
            warnings = []
            info = []
            
            # Check configuration
            star_config = await self._get_star_config(interaction.guild.id)
            if not star_config:
                issues.append("❌ Star of the Week not configured! Use `/star setup` first.")
                await interaction.followup.send("\n".join(issues), ephemeral=True)
                return
            
            role_id = star_config.get('role_id')
            announce_channel_id = star_config.get('announce_channel_id')
            weight_chat = star_config.get('weight_chat', 1.0)
            weight_voice = star_config.get('weight_voice', 2.0)
            
            info.append(f"✅ Star system is configured")
            info.append(f"📊 Weights: Chat={weight_chat}, Voice={weight_voice}")
            
            # Check if task is running
            if self.weekly_star_selection.is_running():
                info.append("✅ Automatic selection task is running")
                
                # Calculate next selection time
                guild_config = await self._get_guild_config(interaction.guild.id)
                tz_name = guild_config.get('timezone', 'UTC') if guild_config else 'UTC'
                try:
                    # Validate timezone
                    tz_name = self._validate_timezone(tz_name, interaction.guild.id)
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    
                    # Find next Sunday 12 PM (noon)
                    days_until_sunday = (6 - now.weekday()) % 7
                    if days_until_sunday == 0 and now.hour >= 12:
                        days_until_sunday = 7  # Next week
                    
                    next_selection = now.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
                    next_selection_utc = next_selection.astimezone(pytz.UTC)
                    next_timestamp = int(next_selection_utc.timestamp())
                    
                    # Calculate time until selection
                    time_until = next_selection - now
                    hours_until = int(time_until.total_seconds() / 3600)
                    minutes_until = int((time_until.total_seconds() % 3600) / 60)
                    
                    info.append(f"📅 Next automatic selection: <t:{next_timestamp}:F> (<t:{next_timestamp}:R>)")
                    info.append(f"⏰ Time until selection: {hours_until}h {minutes_until}m")
                    info.append(f"🕐 Current server time: {now.strftime('%A %I:%M %p')} ({tz_name})")
                    if tz_name != 'UTC':
                        now_utc = datetime.now(pytz.UTC)
                        info.append(f"🌍 Current UTC time: {now_utc.strftime('%A %I:%M %p')} (UTC)")
                except Exception as e:
                    warnings.append(f"⚠️ Could not calculate next selection time: {e}")
            else:
                issues.append("❌ **CRITICAL:** Automatic selection task is NOT running!")
                issues.append("   This means automatic selections won't happen.")
                issues.append("   **Fix:** Restart the bot to start the task.")
            
            # Check role exists
            role = interaction.guild.get_role(role_id)
            if not role:
                issues.append(f"❌ Star role (ID: {role_id}) not found! It may have been deleted.")
            else:
                info.append(f"✅ Star role found: {role.mention} (Position: {role.position})")
                
                # Check bot permissions
                bot_member = interaction.guild.get_member(self.bot.user.id)
                if bot_member:
                    if not bot_member.guild_permissions.manage_roles:
                        issues.append("❌ Bot lacks 'Manage Roles' permission!")
                    else:
                        info.append("✅ Bot has 'Manage Roles' permission")
                    
                    # Check role hierarchy
                    bot_top_role = bot_member.top_role
                    if bot_top_role.position <= role.position:
                        issues.append(
                            f"❌ **ROLE HIERARCHY ERROR**\n"
                            f"   Bot's highest role: **{bot_top_role.name}** (Position: {bot_top_role.position})\n"
                            f"   Star role: **{role.name}** (Position: {role.position})\n"
                            f"   **Fix:** Move the bot's role ABOVE the Star role in Server Settings > Roles"
                        )
                    else:
                        info.append(f"✅ Role hierarchy is correct (Bot: {bot_top_role.position} > Star: {role.position})")
            
            # Check announcement channel
            if announce_channel_id:
                channel = interaction.guild.get_channel(announce_channel_id)
                if not channel:
                    warnings.append(f"⚠️ Announcement channel (ID: {announce_channel_id}) not found")
                else:
                    info.append(f"✅ Announcement channel: {channel.mention}")
            else:
                info.append("ℹ️ No announcement channel (DM only)")
            
            # Check for active users
            stats = await self._get_weekly_stats(interaction.guild.id)
            if stats:
                info.append(f"✅ {len(stats)} users with activity this week")
            else:
                warnings.append("⚠️ No users with activity this week")
            
            # Check previous winner
            previous_winner = await self._get_previous_winner(interaction.guild.id)
            if previous_winner:
                time_since = datetime.utcnow() - previous_winner['awarded_at']
                days = time_since.days
                hours = time_since.seconds // 3600
                info.append(f"ℹ️ Last selection: {days}d {hours}h ago")
                
                # Check if previous winner still has the role
                if role:
                    prev_member = interaction.guild.get_member(previous_winner['user_id'])
                    if prev_member:
                        if role in prev_member.roles:
                            info.append(f"✅ Previous winner {prev_member.mention} still has the role")
                        else:
                            warnings.append(f"⚠️ Previous winner {prev_member.mention} doesn't have the role anymore")
            else:
                info.append("ℹ️ No previous winner (first selection)")
            
            # Build response
            response = "# 🔍 Star of the Week Diagnostics\n\n"
            
            if issues:
                response += "## ❌ Critical Issues\n" + "\n".join(issues) + "\n\n"
            
            if warnings:
                response += "## ⚠️ Warnings\n" + "\n".join(warnings) + "\n\n"
            
            if info:
                response += "## ℹ️ System Status\n" + "\n".join(info) + "\n\n"
            
            if not issues:
                response += "\n✅ **System is ready for Star selection!**\n"
                response += "Use `/star force-select` to trigger selection manually."
            else:
                response += "\n❌ **Fix the issues above before running selection.**"
            
            await interaction.followup.send(response, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error during diagnostics: {e}",
                ephemeral=True
            )
            self.logger.error(f"Diagnose error: {e}", exc_info=True)
    
    @star_group.command(name="force-select", description="[ADMIN] Manually trigger Star of the Week selection now")
    @app_commands.checks.has_permissions(administrator=True)
    async def star_force_select(self, interaction: discord.Interaction):
        """
        Manually trigger Star of the Week selection immediately.
        
        Admin only command for testing or recovering from failed selections.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check if star system is configured
            star_config = await self._get_star_config(interaction.guild.id)
            if not star_config:
                await interaction.followup.send(
                    "❌ Star of the Week not configured! Use `/star setup` first.",
                    ephemeral=True
                )
                return
            
            # Send initial message
            await interaction.followup.send(
                "🔄 **Starting Star of the Week selection...**\n"
                "This may take a few seconds. Check logs for detailed progress.",
                ephemeral=True
            )
            
            # Trigger selection
            await self._process_star_selection(interaction.guild)
            
            # Check if it succeeded by looking at the most recent winner
            previous_winner = await self._get_previous_winner(interaction.guild.id)
            if previous_winner:
                time_since_last = datetime.utcnow() - previous_winner['awarded_at']
                if time_since_last < timedelta(minutes=5):
                    # Selection just happened
                    member = interaction.guild.get_member(previous_winner['user_id'])
                    username = member.mention if member else f"<@{previous_winner['user_id']}>"
                    
                    await interaction.followup.send(
                        f"✅ **Star of the Week selection complete!**\n\n"
                        f"🏆 Winner: {username}\n"
                        f"📊 Score: {previous_winner['score']:.1f}\n"
                        f"💬 Messages: {previous_winner['chat_weekly']:,}\n"
                        f"🎤 Voice: {previous_winner['voice_weekly']:.1f} minutes\n\n"
                        f"Check the logs for detailed information.",
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        "⚠️ **Selection may have failed.**\n"
                        "Check the bot logs for error messages. Common issues:\n"
                        "• Bot role is below the Star role in hierarchy\n"
                        "• Bot lacks 'Manage Roles' permission\n"
                        "• No users with activity this week",
                        ephemeral=True
                    )
            else:
                await interaction.followup.send(
                    "⚠️ **No winner selected.**\n"
                    "Possible reasons:\n"
                    "• No users with activity this week\n"
                    "• Role assignment failed (check logs)\n"
                    "• All users are bots",
                    ephemeral=True
                )
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ **Error during selection:** {e}\n"
                f"Check bot logs for details.",
                ephemeral=True
            )
            self.logger.error(f"Force select error: {e}", exc_info=True)
    
    @star_group.command(name="status", description="[ADMIN] Check automatic selection task status")
    @app_commands.checks.has_permissions(administrator=True)
    async def star_status(self, interaction: discord.Interaction):
        """
        Check the status of the automatic selection task and timing.
        
        Shows real-time information about when the next selection will happen.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check if star system is configured
            star_config = await self._get_star_config(interaction.guild.id)
            if not star_config:
                await interaction.followup.send(
                    "❌ Star of the Week not configured! Use `/star setup` first.",
                    ephemeral=True
                )
                return
            
            # Get timezone
            guild_config = await self._get_guild_config(interaction.guild.id)
            tz_name = guild_config.get('timezone', 'UTC') if guild_config else 'UTC'
            
            try:
                # Validate timezone
                tz_name = self._validate_timezone(tz_name, interaction.guild.id)
                tz = pytz.timezone(tz_name)
                now = datetime.now(tz)
                now_utc = datetime.utcnow()
            except pytz.exceptions.UnknownTimeZoneError:
                await interaction.followup.send(
                    f"❌ Invalid timezone: {tz_name}",
                    ephemeral=True
                )
                return
            
            # Build status message
            status = "# 🔍 Star Selection Task Status\n\n"
            
            # Task running status
            if self.weekly_star_selection.is_running():
                status += "✅ **Task Status:** Running\n"
                status += f"🔄 **Check Interval:** Every 5 minutes\n"
            else:
                status += "❌ **Task Status:** NOT RUNNING\n"
                status += "⚠️ **Action Required:** Restart the bot\n\n"
                await interaction.followup.send(status, ephemeral=True)
                return
            
            # Current time info
            status += f"\n## ⏰ Current Time\n"
            status += f"🌍 **Guild Timezone:** {tz_name}\n"
            status += f"🕐 **Current Time:** {now.strftime('%A, %B %d, %Y at %I:%M:%S %p')}\n"
            status += f"📅 **Weekday:** {now.strftime('%A')} (weekday={now.weekday()})\n"
            status += f"🕒 **Hour:** {now.hour}\n"
            status += f"⏱️ **UTC Time:** {now_utc.strftime('%I:%M:%S %p')}\n"
            
            # Selection trigger condition
            status += f"\n## 🎯 Selection Trigger\n"
            status += f"**Triggers when:** Sunday (weekday=6) at hour=12 (noon)\n"
            status += f"**Current state:** weekday={now.weekday()}, hour={now.hour}\n"
            
            if now.weekday() == 6 and now.hour == 12:
                status += f"✅ **RIGHT NOW is selection time!**\n"
                
                # Check if already selected
                previous_winner = await self._get_previous_winner(interaction.guild.id)
                if previous_winner:
                    time_since_last = datetime.utcnow() - previous_winner['awarded_at']
                    if time_since_last < timedelta(days=6):
                        status += f"⚠️ But already selected {time_since_last.days} days ago (skipping)\n"
                    else:
                        status += f"✅ Last selection was {time_since_last.days} days ago (will select)\n"
                else:
                    status += f"✅ No previous selection (will select)\n"
            else:
                # Calculate next selection
                days_until_sunday = (6 - now.weekday()) % 7
                if days_until_sunday == 0 and now.hour >= 12:
                    days_until_sunday = 7
                
                next_selection = now.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
                time_until = next_selection - now
                hours_until = int(time_until.total_seconds() / 3600)
                
                next_selection_utc = next_selection.astimezone(pytz.UTC)
                next_timestamp = int(next_selection_utc.timestamp())
                
                status += f"⏳ **Next selection:** <t:{next_timestamp}:F>\n"
                status += f"⏰ **Time until:** {hours_until} hours ({time_until.days} days)\n"
            
            # Previous selection info
            previous_winner = await self._get_previous_winner(interaction.guild.id)
            if previous_winner:
                last_timestamp = int(previous_winner['awarded_at'].timestamp())
                time_since = datetime.utcnow() - previous_winner['awarded_at']
                
                status += f"\n## 📜 Last Selection\n"
                status += f"🏆 **Winner:** <@{previous_winner['user_id']}>\n"
                status += f"📅 **When:** <t:{last_timestamp}:F> (<t:{last_timestamp}:R>)\n"
                status += f"⏱️ **Time since:** {time_since.days} days, {time_since.seconds // 3600} hours ago\n"
            else:
                status += f"\n## 📜 Last Selection\n"
                status += f"ℹ️ No previous selection (first time)\n"
            
            # Debug info
            status += f"\n## 🐛 Debug Info\n"
            status += f"**Task loop count:** {self.weekly_star_selection.current_loop}\n"
            status += f"**Task is being cancelled:** {self.weekly_star_selection.is_being_cancelled()}\n"
            status += f"**Task failed:** {self.weekly_star_selection.failed()}\n"
            
            await interaction.followup.send(status, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error checking status: {e}",
                ephemeral=True
            )
            self.logger.error(f"Status check error: {e}", exc_info=True)
    
    @star_group.command(name="preview", description="Preview current week's top candidates")
    async def star_preview(self, interaction: discord.Interaction):
        """
        Preview top 5 candidates for Star of the Week based on current weekly stats.
        
        Useful for checking who's leading before Sunday selection.
        """
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get Star config
            star_config = await self._get_star_config(interaction.guild.id)
            if not star_config:
                await interaction.followup.send(
                    "❌ Star of the Week not configured! Use `/star setup` first.",
                    ephemeral=True
                )
                return
            
            weight_chat = star_config.get('weight_chat', 1.0)
            weight_voice = star_config.get('weight_voice', 2.0)
            
            # Get weekly stats
            stats = await self._get_weekly_stats(interaction.guild.id)
            
            if not stats:
                await interaction.followup.send(
                    "📊 No activity this week yet!",
                    ephemeral=True
                )
                return
            
            # Calculate scores
            scored_users = []
            for user_stat in stats:
                chat_count = user_stat.get('chat_weekly', 0)
                voice_minutes = user_stat.get('voice_weekly', 0)
                
                if chat_count == 0 and voice_minutes == 0:
                    continue
                
                score = self._calculate_score(chat_count, voice_minutes, weight_chat, weight_voice)
                scored_users.append({
                    'user_id': user_stat['user_id'],
                    'score': score,
                    'chat_weekly': chat_count,
                    'voice_weekly': voice_minutes
                })
            
            # Sort and get top 5
            scored_users.sort(key=lambda x: (x['score'], x['voice_weekly'], x['chat_weekly']), reverse=True)
            top_5 = scored_users[:5]
            
            # Build embed
            embed = discord.Embed(
                title="🌟 Star of the Week Preview",
                description="Top 5 candidates based on current weekly activity",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )
            
            for idx, user in enumerate(top_5, 1):
                member = interaction.guild.get_member(user['user_id'])
                username = member.mention if member else f"<@{user['user_id']}>"
                
                voice_hours = int(user['voice_weekly'] // 60)
                voice_mins = int(user['voice_weekly'] % 60)
                voice_str = f"{voice_hours}h {voice_mins}m" if voice_hours > 0 else f"{voice_mins}m"
                
                medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][idx - 1]
                
                embed.add_field(
                    name=f"{medal} {username}",
                    value=(
                        f"💬 {user['chat_weekly']:,} messages\n"
                        f"🎤 {voice_str}\n"
                        f"🏆 Score: {user['score']:.1f}"
                    ),
                    inline=True
                )
            
            embed.set_footer(text=f"Weights: Chat={weight_chat}, Voice={weight_voice} per minute")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the cog"""
    await bot.add_cog(StarOfTheWeekCog(bot))
