"""
Utility functions for leaderboard system
=========================================
Shared utilities for validation, synchronization, and database operations.
"""

import hashlib
from typing import Optional, Union
import pytz
from datetime import datetime, timedelta
import logging
import asyncio
import discord

# Optional pymongo import for bulk operations
try:
    import pymongo
except ImportError:
    pymongo = None

logger = logging.getLogger('discord.bot.leaderboard.utils')


class ConfigValidator:
    """Validate configuration values"""
    
    @staticmethod
    def validate_timezone(timezone: str) -> str:
        """
        Validate and return a valid timezone.
        Falls back to UTC if invalid.
        Handles case-insensitive matching for common timezones.
        """
        if timezone in pytz.all_timezones:
            return timezone
        
        # Try case-insensitive match for common timezone errors
        timezone_lower = timezone.lower()
        for valid_tz in pytz.all_timezones:
            if valid_tz.lower() == timezone_lower:
                logger.info(f"Auto-corrected timezone '{timezone}' to '{valid_tz}'")
                return valid_tz
        
        logger.warning(f"Invalid timezone '{timezone}', using UTC")
        return 'UTC'
    
    @staticmethod
    def validate_weight(weight: float, name: str = "weight") -> float:
        """
        Validate scoring weight values.
        Must be non-negative and reasonable.
        """
        if weight < 0:
            logger.warning(f"Negative {name} ({weight}), using 0")
            return 0.0
        if weight > 1000:
            logger.warning(f"Excessive {name} ({weight}), capping at 1000")
            return 1000.0
        return weight
    
    @staticmethod
    def validate_limit(limit: int, max_limit: int = 100) -> int:
        """
        Validate and cap limit values.
        """
        if limit < 1:
            return 1
        if limit > max_limit:
            return max_limit
        return limit
    
    @staticmethod
    def validate_channel_id(channel_id: Optional[int]) -> Optional[int]:
        """
        Validate Discord channel ID.
        """
        if channel_id is None:
            return None
        if channel_id < 0:
            logger.warning(f"Invalid channel ID {channel_id}")
            return None
        return channel_id


class UserFormatter:
    """Format user information consistently across leaderboards"""
    
    @staticmethod
    def format_username(user_id: int, display_name: Optional[str] = None, max_length: int = 20) -> str:
        """
        Format username with consistent fallback for users who left.
        Uses a hash-based approach to avoid ID collisions.
        """
        if display_name:
            # Truncate long names
            if len(display_name) > max_length:
                return display_name[:max_length - 3] + "..."
            return display_name
        
        # Generate consistent hash for users who left
        user_id_str = str(user_id)
        # Use last 6 digits plus a hash character for uniqueness
        hash_char = chr(65 + (user_id % 26))  # A-Z based on ID
        username = f"User{hash_char}-{user_id_str[-6:]}"
        return username
    
    @staticmethod
    def get_user_hash(user_id: int) -> str:
        """
        Generate a short hash for a user ID.
        Used for anonymous references.
        """
        hash_obj = hashlib.md5(str(user_id).encode())
        return hash_obj.hexdigest()[:8]


class DatabaseTransactionManager:
    """
    Manage database transactions to prevent concurrent modification issues.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.transactions')
    
    async def atomic_update(self, collection_name: str, filter_dict: dict, 
                           update_dict: dict, upsert: bool = False) -> bool:
        """
        Perform an atomic update with retry logic.
        Returns True if successful.
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                collection = self.db[collection_name]
                result = await collection.update_one(
                    filter_dict,
                    update_dict,
                    upsert=upsert
                )
                return result.acknowledged
            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.warning(f"Retry {attempt + 1}/{max_retries} for atomic update: {e}")
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    self.logger.error(f"Failed atomic update after {max_retries} attempts: {e}")
                    return False
        return False
    
    async def bulk_update(self, collection_name: str, updates: list) -> int:
        """
        Perform bulk updates efficiently.
        Returns number of successful updates.
        """
        if not updates:
            return 0
        
        if pymongo is None:
            self.logger.warning("pymongo not available, falling back to individual updates")
            # Fallback to individual updates
            successful = 0
            collection = self.db[collection_name]
            for update in updates:
                try:
                    result = await collection.update_one(
                        update['filter'],
                        update['update'],
                        upsert=update.get('upsert', False)
                    )
                    if result.acknowledged:
                        successful += 1
                except Exception as e:
                    self.logger.error(f"Individual update failed: {e}")
            return successful
        
        successful = 0
        collection = self.db[collection_name]
        
        # Process in batches
        batch_size = 100
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            try:
                # Use bulk write for efficiency
                operations = []
                for update in batch:
                    operations.append(
                        pymongo.UpdateOne(
                            update['filter'],
                            update['update'],
                            upsert=update.get('upsert', False)
                        )
                    )
                
                result = await collection.bulk_write(operations, ordered=False)
                successful += result.modified_count + result.upserted_count
            except Exception as e:
                self.logger.error(f"Bulk update failed for batch: {e}")
        
        return successful


class TaskSynchronizer:
    """
    Coordinate task execution between different cogs.
    """
    
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger('discord.bot.leaderboard.sync')
    
    async def acquire_lock(self, guild_id: int, lock_type: str, 
                          timeout_seconds: int = 60) -> bool:
        """
        Acquire a distributed lock for a specific operation.
        Returns True if lock acquired.
        """
        lock_doc = {
            'guild_id': guild_id,
            'lock_type': lock_type,
            'acquired_at': datetime.utcnow(),
            'expires_at': datetime.utcnow().timestamp() + timeout_seconds
        }
        
        try:
            # Try to insert lock document
            await self.db.task_locks.insert_one(lock_doc)
            return True
        except:
            # Lock already exists, check if expired
            existing = await self.db.task_locks.find_one({
                'guild_id': guild_id,
                'lock_type': lock_type
            })
            
            if existing and existing['expires_at'] < datetime.utcnow().timestamp():
                # Lock expired, try to update it
                result = await self.db.task_locks.update_one(
                    {
                        'guild_id': guild_id,
                        'lock_type': lock_type,
                        'expires_at': {'$lt': datetime.utcnow().timestamp()}
                    },
                    {'$set': lock_doc}
                )
                return result.modified_count > 0
            
            return False
    
    async def release_lock(self, guild_id: int, lock_type: str):
        """
        Release a distributed lock.
        """
        try:
            await self.db.task_locks.delete_one({
                'guild_id': guild_id,
                'lock_type': lock_type
            })
        except Exception as e:
            self.logger.error(f"Failed to release lock {lock_type} for guild {guild_id}: {e}")
    
    async def check_recent_reset(self, guild_id: int, reset_type: str, 
                                hours_threshold: int = 6) -> bool:
        """
        Check if a reset was performed recently.
        Returns True if reset happened within threshold.
        """
        try:
            config = await self.db.guild_configs.find_one({'guild_id': guild_id})
            if not config:
                return False
            
            reset_field = f'last_{reset_type}_reset'
            last_reset = config.get(reset_field)
            
            if not last_reset:
                return False
            
            time_since = (datetime.utcnow() - last_reset).total_seconds() / 3600
            return time_since < hours_threshold
        except Exception as e:
            self.logger.error(f"Error checking recent reset: {e}")
            return False
    
    async def mark_reset_complete(self, guild_id: int, reset_type: str):
        """
        Mark a reset as completed.
        """
        try:
            await self.db.guild_configs.update_one(
                {'guild_id': guild_id},
                {'$set': {f'last_{reset_type}_reset': datetime.utcnow()}}
            )
        except Exception as e:
            self.logger.error(f"Failed to mark reset complete: {e}")



class SafePaginator:
    """
    Safe pagination handler with zero-division protection and edge case handling.
    """
    
    def __init__(self, data: list, page_size: int = 10):
        """
        Initialize paginator.
        
        Args:
            data: List of items to paginate
            page_size: Number of items per page
        """
        self.data = data if data else []
        # FIX #4: Ensure page_size is positive integer
        if not isinstance(page_size, int) or page_size < 1:
            logger.warning(f"Invalid page_size {page_size}, using default 10")
            page_size = 10
        self.page_size = max(1, abs(page_size))  # Ensure page_size is at least 1 and positive
    
    def get_max_pages(self) -> int:
        """
        Get maximum number of pages.
        
        Returns:
            Number of pages (0 if no data)
        """
        if not self.data or len(self.data) == 0:
            return 0
        
        # Calculate pages, ensuring we don't divide by zero
        return max(0, (len(self.data) - 1) // self.page_size)
    
    def get_page(self, page_num: int) -> list:
        """
        Get items for a specific page.
        
        Args:
            page_num: Page number (0-indexed)
            
        Returns:
            List of items for the page (empty list if invalid)
        """
        if not self.data or page_num < 0:
            return []
        
        start_idx = page_num * self.page_size
        end_idx = start_idx + self.page_size
        
        if start_idx >= len(self.data):
            return []
        
        return self.data[start_idx:end_idx]
    
    def has_next_page(self, current_page: int) -> bool:
        """
        Check if there's a next page.
        
        Args:
            current_page: Current page number (0-indexed)
            
        Returns:
            True if next page exists
        """
        if not self.data:
            return False
        return current_page < self.get_max_pages()
    
    def has_previous_page(self, current_page: int) -> bool:
        """
        Check if there's a previous page.
        
        Args:
            current_page: Current page number (0-indexed)
            
        Returns:
            True if previous page exists
        """
        return current_page > 0
    
    def get_total_items(self) -> int:
        """Get total number of items"""
        return len(self.data)


class UserDisplayFormatter:
    """
    Format user display names consistently with fallbacks for users who left.
    Enhanced with caching and better error handling.
    """
    
    # Class-level cache for username lookups (guild_id, user_id) -> (name, timestamp)
    _username_cache = {}
    _cache_ttl = 300  # 5 minutes cache
    
    @staticmethod
    async def get_display_name(guild, user_id: int, max_length: int = 20) -> str:
        """
        Get display name for a user with fallback for users who left.
        Enhanced with caching to reduce API calls.
        
        Args:
            guild: Discord guild object
            user_id: User ID
            max_length: Maximum length for display name
            
        Returns:
            Display name (truncated if necessary)
        """
        from datetime import datetime
        
        # Check cache first
        cache_key = (guild.id, user_id)
        if cache_key in UserDisplayFormatter._username_cache:
            cached_name, cached_time = UserDisplayFormatter._username_cache[cache_key]
            cache_age = (datetime.utcnow() - cached_time).total_seconds()
            if cache_age < UserDisplayFormatter._cache_ttl:
                # Cache hit - return cached name
                return UserDisplayFormatter.truncate_name(cached_name, max_length)
        
        # Try cache first (O(1) lookup)
        member = guild.get_member(user_id)
        
        if member:
            name = member.display_name
        else:
            # Try API fetch
            try:
                member = await guild.fetch_member(user_id)
                name = member.display_name
            except discord.NotFound:
                # User left server, use fallback
                name = UserDisplayFormatter.generate_fallback_name(user_id)
                logger.debug(f"User {user_id} not found in guild {guild.id}, using fallback name")
            except discord.HTTPException as e:
                # API error, use fallback
                logger.warning(f"Failed to fetch member {user_id}: {e}")
                name = UserDisplayFormatter.generate_fallback_name(user_id)
            except Exception as e:
                # Unexpected error, use fallback
                logger.error(f"Unexpected error fetching member {user_id}: {e}")
                name = UserDisplayFormatter.generate_fallback_name(user_id)
        
        # Validate and sanitize the name
        if not name or not isinstance(name, str):
            name = UserDisplayFormatter.generate_fallback_name(user_id)
        
        # Remove null bytes and control characters
        name = ''.join(char for char in name if ord(char) >= 32 or char in '\n\r\t')
        
        # If name is empty after sanitization, use fallback
        if not name.strip():
            name = UserDisplayFormatter.generate_fallback_name(user_id)
        
        # Update cache
        UserDisplayFormatter._username_cache[cache_key] = (name, datetime.utcnow())
        
        # Clean old cache entries periodically (keep cache size manageable)
        if len(UserDisplayFormatter._username_cache) > 1000:
            UserDisplayFormatter._clean_cache()
        
        # Truncate if necessary
        return UserDisplayFormatter.truncate_name(name, max_length)
    
    @staticmethod
    def _clean_cache():
        """Remove expired entries from cache"""
        from datetime import datetime
        current_time = datetime.utcnow()
        expired_keys = [
            key for key, (_, cached_time) in UserDisplayFormatter._username_cache.items()
            if (current_time - cached_time).total_seconds() > UserDisplayFormatter._cache_ttl
        ]
        for key in expired_keys:
            del UserDisplayFormatter._username_cache[key]
        logger.debug(f"Cleaned {len(expired_keys)} expired username cache entries")
    
    @staticmethod
    def generate_fallback_name(user_id: int) -> str:
        """
        Generate consistent hash-based fallback name for users who left.
        
        Args:
            user_id: User ID
            
        Returns:
            Fallback name in format "User{A-Z}-{last6digits}"
        """
        user_id_str = str(user_id)
        # Use last 6 digits plus a hash character for uniqueness
        hash_char = chr(65 + (user_id % 26))  # A-Z based on ID
        return f"User{hash_char}-{user_id_str[-6:]}"
    
    @staticmethod
    def truncate_name(name: str, max_length: int) -> str:
        """
        Truncate name if it exceeds max length (FIX #20: UTF-8 safe).
        
        Args:
            name: Display name
            max_length: Maximum length
            
        Returns:
            Truncated name with ellipsis if necessary
        """
        if len(name) <= max_length:
            return name
        
        # FIX #20: Safely truncate without breaking multi-byte characters
        # Try to encode and decode to ensure we don't break UTF-8
        try:
            truncated = name[:max_length - 3]
            # Verify it's valid UTF-8 by encoding/decoding
            truncated.encode('utf-8').decode('utf-8')
            return truncated + "..."
        except (UnicodeDecodeError, UnicodeEncodeError):
            # If we broke a character, try one character less
            try:
                truncated = name[:max_length - 4]
                truncated.encode('utf-8').decode('utf-8')
                return truncated + "..."
            except:
                # Fallback: use safe ASCII truncation
                safe_name = name.encode('ascii', errors='ignore').decode('ascii')
                return safe_name[:max_length - 3] + "..."


class ResetTimeCalculator:
    """
    Calculate reset times correctly across all edge cases.
    """
    
    @staticmethod
    def get_next_daily_reset(tz) -> datetime:
        """
        Get next daily reset time (midnight in guild timezone).
        
        Args:
            tz: pytz timezone object
            
        Returns:
            Next reset datetime
        """
        now = datetime.now(tz)
        next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return next_reset
    
    @staticmethod
    def get_next_weekly_reset(tz, reset_day: int = 6, reset_hour: int = 12) -> datetime:
        """
        Get next weekly reset time (Sunday 12 PM by default).
        
        Args:
            tz: pytz timezone object
            reset_day: Day of week (0=Monday, 6=Sunday)
            reset_hour: Hour of day (24-hour format)
            
        Returns:
            Next reset datetime
        """
        now = datetime.now(tz)
        
        # Calculate days until reset day
        days_until_reset = (reset_day - now.weekday()) % 7
        
        # Calculate the reset time for this week
        this_week_reset = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        
        # If it's the reset day and we're past the reset time, go to next week
        if days_until_reset == 0 and now >= this_week_reset:
            days_until_reset = 7
        
        next_reset = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_until_reset)
        return next_reset
    
    @staticmethod
    def get_next_monthly_reset(tz) -> datetime:
        """
        Get next monthly reset time (1st of next month at midnight).
        
        Args:
            tz: pytz timezone object
            
        Returns:
            Next reset datetime
        """
        now = datetime.now(tz)
        
        # Calculate next month's first day
        if now.month == 12:
            next_reset = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            next_reset = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        
        return next_reset
    
    @staticmethod
    def format_duration(minutes: float) -> str:
        """
        Format minutes into human-readable time string.
        
        Args:
            minutes: Duration in minutes
            
        Returns:
            Formatted string (e.g., "2h 30m", "45m", "3d 5h")
        """
        # Validate input
        if not isinstance(minutes, (int, float)):
            logger.error(f"Invalid type for minutes: {type(minutes)}")
            return "0m"
        
        if minutes < 0:
            logger.warning(f"Negative time value: {minutes}")
            return "0m"
        
        if minutes == 0:
            return "0m"
        
        # FIX #7: Prevent integer overflow with extremely large values
        MAX_MINUTES = 525600 * 10  # 10 years in minutes (reasonable max)
        if minutes > MAX_MINUTES:
            logger.warning(f"Extremely large time value detected: {minutes} minutes, capping at {MAX_MINUTES}")
            minutes = MAX_MINUTES
        
        # Round to avoid float display issues
        minutes = round(minutes)
        
        if minutes < 60:
            return f"{minutes}m"
        
        hours = minutes // 60
        mins = minutes % 60
        
        if hours >= 24:
            days = hours // 24
            hours = hours % 24
            if hours == 0:
                return f"{days}d"
            return f"{days}d {hours}h"
        
        if mins == 0:
            return f"{hours}h"
        return f"{hours}h {mins}m"



class ResilientDatabaseOps:
    """
    Add resilience to database operations with retry logic and error handling.
    """
    
    def __init__(self, db, logger_instance=None):
        """
        Initialize resilient database operations.
        
        Args:
            db: MongoDB database instance
            logger_instance: Logger instance (optional)
        """
        self.db = db
        self.logger = logger_instance or logger
    
    async def update_with_retry(self, collection_name: str, filter_dict: dict, 
                               update_dict: dict, upsert: bool = False, 
                               max_retries: int = 3) -> bool:
        """
        Perform database update with retry logic and exponential backoff.
        
        Args:
            collection_name: Name of the collection
            filter_dict: Filter for the update
            update_dict: Update operations
            upsert: Whether to insert if not found
            max_retries: Maximum number of retry attempts
            
        Returns:
            True if successful, False otherwise
        """
        collection = self.db[collection_name]
        
        for attempt in range(max_retries):
            try:
                result = await collection.update_one(
                    filter_dict,
                    update_dict,
                    upsert=upsert
                )
                
                if result.acknowledged:
                    return True
                else:
                    self.logger.warning(f"Update not acknowledged on attempt {attempt + 1}/{max_retries}")
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    # Exponential backoff: 0.5s, 1.5s, 3s
                    wait_time = 0.5 * (2 ** attempt)
                    self.logger.warning(
                        f"Database update failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"Database update failed after {max_retries} attempts. "
                        f"Collection: {collection_name}, Filter: {filter_dict}, Error: {e}",
                        exc_info=True
                    )
                    return False
        
        return False
    
    async def bulk_update_batched(self, collection_name: str, updates: list, 
                                  batch_size: int = 50) -> int:
        """
        Perform bulk updates in batches to avoid overwhelming the database.
        
        Args:
            collection_name: Name of the collection
            updates: List of update operations (each with 'filter' and 'update' keys)
            batch_size: Number of updates per batch
            
        Returns:
            Number of successful updates
        """
        if not updates:
            return 0
        
        collection = self.db[collection_name]
        successful = 0
        
        # Process in batches
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            
            try:
                if pymongo is not None:
                    # Use bulk write for efficiency
                    operations = []
                    for update in batch:
                        operations.append(
                            pymongo.UpdateOne(
                                update['filter'],
                                update['update'],
                                upsert=update.get('upsert', False)
                            )
                        )
                    
                    result = await collection.bulk_write(operations, ordered=False)
                    successful += result.modified_count + result.upserted_count
                else:
                    # Fallback to individual updates
                    for update in batch:
                        try:
                            result = await collection.update_one(
                                update['filter'],
                                update['update'],
                                upsert=update.get('upsert', False)
                            )
                            if result.acknowledged:
                                successful += 1
                        except Exception as e:
                            self.logger.error(f"Individual update failed: {e}")
                
                # Small delay between batches
                if i + batch_size < len(updates):
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                self.logger.error(f"Bulk update failed for batch {i//batch_size + 1}: {e}", exc_info=True)
        
        self.logger.info(f"Bulk update complete: {successful}/{len(updates)} successful")
        return successful
    
    async def find_with_timeout(self, collection_name: str, filter_dict: dict, 
                                timeout: int = 5) -> Optional[dict]:
        """
        Find document with timeout to prevent hanging.
        
        Args:
            collection_name: Name of the collection
            filter_dict: Filter for the query
            timeout: Timeout in seconds
            
        Returns:
            Document if found, None otherwise
        """
        collection = self.db[collection_name]
        
        try:
            # Use asyncio.wait_for to implement timeout
            result = await asyncio.wait_for(
                collection.find_one(filter_dict),
                timeout=timeout
            )
            return result
            
        except asyncio.TimeoutError:
            self.logger.error(
                f"Database query timed out after {timeout}s. "
                f"Collection: {collection_name}, Filter: {filter_dict}"
            )
            return None
            
        except Exception as e:
            self.logger.error(
                f"Database query failed. Collection: {collection_name}, "
                f"Filter: {filter_dict}, Error: {e}",
                exc_info=True
            )
            return None
    
    async def find_many_with_timeout(self, collection_name: str, filter_dict: dict, 
                                    limit: int = 100, timeout: int = 5) -> list:
        """
        Find multiple documents with timeout.
        
        Args:
            collection_name: Name of the collection
            filter_dict: Filter for the query
            limit: Maximum number of documents to return
            timeout: Timeout in seconds
            
        Returns:
            List of documents (empty list if error)
        """
        collection = self.db[collection_name]
        
        try:
            cursor = collection.find(filter_dict).limit(limit)
            result = await asyncio.wait_for(
                cursor.to_list(length=limit),
                timeout=timeout
            )
            return result
            
        except asyncio.TimeoutError:
            self.logger.error(
                f"Database query timed out after {timeout}s. "
                f"Collection: {collection_name}, Filter: {filter_dict}"
            )
            return []
            
        except Exception as e:
            self.logger.error(
                f"Database query failed. Collection: {collection_name}, "
                f"Filter: {filter_dict}, Error: {e}",
                exc_info=True
            )
            return []



class SafeInteractionHandler:
    """
    Handle Discord interactions safely with proper error handling.
    """
    
    @staticmethod
    async def safe_respond(interaction: discord.Interaction, content: str = None, 
                          embed: discord.Embed = None, view: discord.ui.View = None,
                          ephemeral: bool = False, **kwargs) -> bool:
        """
        Safely respond to an interaction with error handling.
        
        Args:
            interaction: Discord interaction
            content: Message content
            embed: Embed to send
            view: View with buttons
            ephemeral: Whether message should be ephemeral
            **kwargs: Additional arguments for response
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if already responded
            if interaction.response.is_done():
                logger.warning(f"Interaction {interaction.id} already responded to")
                return False
            
            # Build message parameters
            message_params = {
                "ephemeral": ephemeral,
                **kwargs
            }
            if content is not None:
                message_params["content"] = content
            if embed is not None:
                message_params["embed"] = embed
            if view is not None:
                message_params["view"] = view
            
            # Send response
            await interaction.response.send_message(**message_params)
            return True
            
        except discord.InteractionResponded:
            logger.warning(f"Interaction {interaction.id} already responded (InteractionResponded exception)")
            return False
            
        except discord.NotFound:
            logger.warning(f"Interaction {interaction.id} expired (NotFound exception)")
            return False
            
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                logger.warning(f"Rate limited responding to interaction {interaction.id}")
            else:
                logger.error(f"HTTP error responding to interaction {interaction.id}: {e}")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error responding to interaction {interaction.id}: {e}", exc_info=True)
            return False
    
    @staticmethod
    async def safe_edit(interaction: discord.Interaction, content: str = None,
                       embed: discord.Embed = None, view: discord.ui.View = None,
                       **kwargs) -> bool:
        """
        Safely edit an interaction response with error handling.
        
        Args:
            interaction: Discord interaction
            content: New message content
            embed: New embed
            view: New view with buttons
            **kwargs: Additional arguments for edit
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Build message parameters
            message_params = {**kwargs}
            if content is not None:
                message_params["content"] = content
            if embed is not None:
                message_params["embed"] = embed
            if view is not None:
                message_params["view"] = view
            
            # Check if already responded
            if interaction.response.is_done():
                # Use edit_message instead
                await interaction.response.edit_message(**message_params)
            else:
                # Defer first, then edit
                await interaction.response.defer()
                await interaction.edit_original_response(**message_params)
            return True
            
        except discord.InteractionResponded:
            # Try to edit the original response
            try:
                # Build message parameters
                message_params = {**kwargs}
                if content is not None:
                    message_params["content"] = content
                if embed is not None:
                    message_params["embed"] = embed
                if view is not None:
                    message_params["view"] = view
                
                await interaction.edit_original_response(**message_params)
                return True
            except Exception as e:
                logger.error(f"Failed to edit original response: {e}")
                return False
            
        except discord.NotFound:
            logger.warning(f"Interaction {interaction.id} expired (NotFound exception)")
            return False
            
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                logger.warning(f"Rate limited editing interaction {interaction.id}")
                # Retry once after delay
                try:
                    retry_after = float(e.response.headers.get('Retry-After', 1))
                    await asyncio.sleep(retry_after)
                    
                    # Build message parameters
                    message_params = {**kwargs}
                    if content is not None:
                        message_params["content"] = content
                    if embed is not None:
                        message_params["embed"] = embed
                    if view is not None:
                        message_params["view"] = view
                    
                    await interaction.edit_original_response(**message_params)
                    return True
                except Exception:
                    return False
            else:
                logger.error(f"HTTP error editing interaction {interaction.id}: {e}")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error editing interaction {interaction.id}: {e}", exc_info=True)
            return False
    
    @staticmethod
    async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False) -> bool:
        """
        Safely defer an interaction response.
        
        Args:
            interaction: Discord interaction
            ephemeral: Whether the deferred response should be ephemeral
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if already responded
            if interaction.response.is_done():
                logger.debug(f"Interaction {interaction.id} already responded, cannot defer")
                return False
            
            await interaction.response.defer(ephemeral=ephemeral)
            return True
            
        except discord.InteractionResponded:
            logger.warning(f"Interaction {interaction.id} already responded (cannot defer)")
            return False
            
        except discord.NotFound:
            logger.warning(f"Interaction {interaction.id} expired")
            return False
            
        except Exception as e:
            logger.error(f"Error deferring interaction {interaction.id}: {e}", exc_info=True)
            return False
    
    @staticmethod
    async def send_error_message(interaction: discord.Interaction, error_text: str) -> bool:
        """
        Send an ephemeral error message to the user.
        
        Args:
            interaction: Discord interaction
            error_text: Error message to display
            
        Returns:
            True if successful, False otherwise
        """
        return await SafeInteractionHandler.safe_respond(
            interaction,
            content=f"❌ {error_text}",
            ephemeral=True
        )
