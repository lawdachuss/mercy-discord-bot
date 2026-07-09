"""
Safe Data Retrieval Module for Leaderboard System
==================================================
Wraps database queries with error handling, validation, and retry logic.

This module ensures:
- Database errors don't crash the bot
- Retry logic with exponential backoff
- Data validation before returning
- Proper logging of all errors
"""

from typing import List, Dict, Optional
import asyncio
import logging
from datetime import datetime
from .data_validator import LeaderboardDataValidator


class SafeLeaderboardData:
    """Safe database operations for leaderboard data"""
    
    def __init__(self, db, logger: logging.Logger):
        """
        Initialize safe data retrieval.
        
        Args:
            db: MongoDB database instance
            logger: Logger instance for error reporting
        """
        self.db = db
        self.logger = logger
        self.max_retries = 3
        self.base_backoff = 0.5  # seconds
    
    async def get_top_users_safe(self, guild_id: int, period: str, 
                                  limit: int = 100, stat_type: str = 'chat') -> List[Dict]:
        """
        Get top users with error handling and validation.
        
        Args:
            guild_id: Discord guild ID
            period: Time period (daily, weekly, monthly, alltime)
            limit: Maximum number of users to fetch
            stat_type: Type of stats ('chat' or 'voice')
            
        Returns:
            List of validated user stats, empty list on error
            
        Features:
        - Validates inputs before querying
        - Retries on database errors
        - Validates output data
        - Returns empty list on failure (never crashes)
        """
        try:
            # Validate inputs
            guild_id = LeaderboardDataValidator.validate_guild_id(guild_id)
            period = LeaderboardDataValidator.validate_period(period)
            
            if stat_type not in ['chat', 'voice']:
                self.logger.error(f"Invalid stat_type: {stat_type}")
                return []
            
            # Determine field name
            field = f"{stat_type}_{period}"
            
            # Clamp limit to reasonable range
            limit = max(1, min(limit, 1000))
            
            # Query with retry logic
            for attempt in range(self.max_retries):
                try:
                    cursor = self.db.user_stats.find(
                        {'guild_id': guild_id, field: {'$gt': 0}}
                    ).sort(field, -1).limit(limit)
                    
                    stats = await cursor.to_list(length=limit)
                    
                    # Validate results
                    validated_stats = LeaderboardDataValidator.validate_user_stats(stats)
                    
                    self.logger.debug(
                        f"Retrieved {len(validated_stats)} users for guild {guild_id}, "
                        f"period {period}, type {stat_type}"
                    )
                    
                    return validated_stats
                    
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        backoff = self.base_backoff * (2 ** attempt)
                        self.logger.warning(
                            f"Database query failed (attempt {attempt + 1}/{self.max_retries}): {e}. "
                            f"Retrying in {backoff}s..."
                        )
                        await asyncio.sleep(backoff)
                    else:
                        self.logger.error(
                            f"Failed to get top users after {self.max_retries} attempts: {e}",
                            exc_info=True
                        )
                        return []
            
            return []
            
        except ValueError as e:
            self.logger.error(f"Validation error in get_top_users_safe: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error in get_top_users_safe: {e}", exc_info=True)
            return []
    
    async def get_total_count_safe(self, guild_id: int, period: str, 
                                    stat_type: str = 'chat') -> int | float:
        """
        Get total count (messages or minutes) with validation.
        
        Args:
            guild_id: Discord guild ID
            period: Time period (daily, weekly, monthly, alltime)
            stat_type: Type of stats ('chat' or 'voice')
            
        Returns:
            Total count for the period, 0 on error
            
        Features:
        - Validates inputs
        - Aggregates safely
        - Returns 0 on any error
        """
        try:
            # Validate inputs
            guild_id = LeaderboardDataValidator.validate_guild_id(guild_id)
            period = LeaderboardDataValidator.validate_period(period)
            
            if stat_type not in ['chat', 'voice']:
                self.logger.error(f"Invalid stat_type: {stat_type}")
                return 0
            
            # Determine field name
            field = f"{stat_type}_{period}"
            
            # Query with retry logic
            for attempt in range(self.max_retries):
                try:
                    # Use aggregation pipeline for efficient sum
                    pipeline = [
                        {'$match': {'guild_id': guild_id, field: {'$gt': 0}}},
                        {'$group': {'_id': None, 'total': {'$sum': f'${field}'}}}
                    ]
                    
                    cursor = self.db.user_stats.aggregate(pipeline)
                    results = await cursor.to_list(length=1)
                    
                    if results and 'total' in results[0]:
                        total = results[0]['total']
                        # Validate the total
                        allow_float = (stat_type == 'voice')
                        validated_total = LeaderboardDataValidator.validate_count(
                            total, default=0, allow_float=allow_float
                        )
                        return validated_total
                    
                    return 0
                    
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        backoff = self.base_backoff * (2 ** attempt)
                        self.logger.warning(
                            f"Total count query failed (attempt {attempt + 1}/{self.max_retries}): {e}. "
                            f"Retrying in {backoff}s..."
                        )
                        await asyncio.sleep(backoff)
                    else:
                        self.logger.error(
                            f"Failed to get total count after {self.max_retries} attempts: {e}",
                            exc_info=True
                        )
                        return 0
            
            return 0
            
        except ValueError as e:
            self.logger.error(f"Validation error in get_total_count_safe: {e}")
            return 0
        except Exception as e:
            self.logger.error(f"Unexpected error in get_total_count_safe: {e}", exc_info=True)
            return 0
    
    async def get_last_month_winner_safe(self, guild_id: int, 
                                         stat_type: str = 'chat') -> Optional[Dict]:
        """
        Get last month winner with error handling.
        
        Args:
            guild_id: Discord guild ID
            stat_type: Type of stats ('chat' or 'voice')
            
        Returns:
            Dict with winner info or None if not found/error
            Format: {'user_id': int, 'count': int/float, 'month': datetime}
            
        Features:
        - Validates inputs
        - Handles missing data gracefully
        - Returns None on any error
        """
        try:
            # Validate inputs
            guild_id = LeaderboardDataValidator.validate_guild_id(guild_id)
            
            if stat_type not in ['chat', 'voice']:
                self.logger.error(f"Invalid stat_type: {stat_type}")
                return None
            
            # Query with retry logic
            for attempt in range(self.max_retries):
                try:
                    # Find most recent monthly archive
                    cursor = self.db.weekly_history.find({
                        'guild_id': guild_id,
                        'type': stat_type,
                        'period': 'monthly'
                    }).sort('reset_date', -1).limit(1)
                    
                    archives = await cursor.to_list(length=1)
                    
                    if not archives:
                        self.logger.debug(f"No monthly archive found for guild {guild_id}, type {stat_type}")
                        return None
                    
                    archive = archives[0]
                    stats = archive.get('stats', [])
                    
                    if not stats:
                        self.logger.debug(f"Empty stats in archive for guild {guild_id}")
                        return None
                    
                    # Validate stats
                    validated_stats = LeaderboardDataValidator.validate_user_stats(stats)
                    
                    if not validated_stats:
                        self.logger.debug(f"No valid stats after validation for guild {guild_id}")
                        return None
                    
                    # Find user with highest count
                    field = f"{stat_type}_monthly"
                    top_user = max(validated_stats, key=lambda x: x.get(field, 0))
                    
                    count = top_user.get(field, 0)
                    
                    if count <= 0:
                        self.logger.debug(f"Top user has zero count for guild {guild_id}")
                        return None
                    
                    # Validate count
                    allow_float = (stat_type == 'voice')
                    validated_count = LeaderboardDataValidator.validate_count(
                        count, default=0, allow_float=allow_float
                    )
                    
                    if validated_count <= 0:
                        return None
                    
                    # Build result
                    result = {
                        'user_id': top_user['user_id'],
                        'count' if stat_type == 'chat' else 'minutes': validated_count,
                        'month': archive.get('reset_date', datetime.utcnow())
                    }
                    
                    return result
                    
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        backoff = self.base_backoff * (2 ** attempt)
                        self.logger.warning(
                            f"Last month winner query failed (attempt {attempt + 1}/{self.max_retries}): {e}. "
                            f"Retrying in {backoff}s..."
                        )
                        await asyncio.sleep(backoff)
                    else:
                        self.logger.error(
                            f"Failed to get last month winner after {self.max_retries} attempts: {e}",
                            exc_info=True
                        )
                        return None
            
            return None
            
        except ValueError as e:
            self.logger.error(f"Validation error in get_last_month_winner_safe: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error in get_last_month_winner_safe: {e}", exc_info=True)
            return None
    
    async def get_user_stat_safe(self, guild_id: int, user_id: int) -> Optional[Dict]:
        """
        Get a single user's stats with error handling.
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            
        Returns:
            Dict with user stats or None if not found/error
            
        Features:
        - Validates inputs
        - Handles missing users gracefully
        - Returns None on any error
        """
        try:
            # Validate inputs
            guild_id = LeaderboardDataValidator.validate_guild_id(guild_id)
            user_id = LeaderboardDataValidator.validate_user_id(user_id)
            
            # Query with retry logic
            for attempt in range(self.max_retries):
                try:
                    stat = await self.db.user_stats.find_one({
                        'guild_id': guild_id,
                        'user_id': user_id
                    })
                    
                    if not stat:
                        return None
                    
                    # Validate result
                    validated_stats = LeaderboardDataValidator.validate_user_stats([stat])
                    
                    if not validated_stats:
                        return None
                    
                    return validated_stats[0]
                    
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        backoff = self.base_backoff * (2 ** attempt)
                        self.logger.warning(
                            f"User stat query failed (attempt {attempt + 1}/{self.max_retries}): {e}. "
                            f"Retrying in {backoff}s..."
                        )
                        await asyncio.sleep(backoff)
                    else:
                        self.logger.error(
                            f"Failed to get user stat after {self.max_retries} attempts: {e}",
                            exc_info=True
                        )
                        return None
            
            return None
            
        except ValueError as e:
            self.logger.error(f"Validation error in get_user_stat_safe: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error in get_user_stat_safe: {e}", exc_info=True)
            return None
