"""
Data Validation Module for Leaderboard System
==============================================
Validates and sanitizes all data before formatting to prevent display issues.

This module ensures:
- All counts are non-negative integers/floats
- Usernames are properly formatted and truncated
- Period values are valid
- Database results are sanitized
"""

from typing import List, Dict, Any, Optional
import logging


class LeaderboardDataValidator:
    """Validates and sanitizes leaderboard data"""
    
    logger = logging.getLogger('discord.bot.leaderboard.validator')
    
    @staticmethod
    def validate_user_stats(stats: List[Dict]) -> List[Dict]:
        """
        Validate and sanitize user stats from database.
        
        Args:
            stats: List of user stat dictionaries from MongoDB
            
        Returns:
            List of validated and sanitized user stats
            
        Ensures:
        - All required fields exist
        - All counts are non-negative
        - Invalid entries are filtered out
        """
        if not stats:
            return []
        
        validated_stats = []
        
        for stat in stats:
            try:
                # Validate required fields exist
                if not isinstance(stat, dict):
                    LeaderboardDataValidator.logger.warning(f"Invalid stat type: {type(stat)}")
                    continue
                
                if 'user_id' not in stat:
                    LeaderboardDataValidator.logger.warning("Stat missing user_id, skipping")
                    continue
                
                # Validate user_id is an integer
                try:
                    user_id = int(stat['user_id'])
                except (ValueError, TypeError):
                    LeaderboardDataValidator.logger.warning(f"Invalid user_id: {stat.get('user_id')}")
                    continue
                
                # Create validated stat with sanitized values
                validated_stat = {
                    'user_id': user_id,
                    'guild_id': int(stat.get('guild_id', 0)),
                }
                
                # Validate and sanitize all count fields
                for field in ['chat_daily', 'chat_weekly', 'chat_monthly', 'chat_alltime',
                             'voice_daily', 'voice_weekly', 'voice_monthly', 'voice_alltime']:
                    if field in stat:
                        if 'voice' in field:
                            # Voice fields are floats (minutes)
                            validated_stat[field] = LeaderboardDataValidator.validate_count(
                                stat[field], default=0.0, allow_float=True
                            )
                        else:
                            # Chat fields are integers
                            validated_stat[field] = LeaderboardDataValidator.validate_count(
                                stat[field], default=0, allow_float=False
                            )
                
                # Keep other fields that might be present
                for key, value in stat.items():
                    if key not in validated_stat:
                        validated_stat[key] = value
                
                validated_stats.append(validated_stat)
                
            except Exception as e:
                LeaderboardDataValidator.logger.error(f"Error validating stat: {e}", exc_info=True)
                continue
        
        return validated_stats
    
    @staticmethod
    def validate_count(count: Any, default: int | float = 0, allow_float: bool = False) -> int | float:
        """
        Validate message/time count is valid non-negative number.
        
        Args:
            count: Count value to validate
            default: Default value if validation fails
            allow_float: Whether to allow float values
            
        Returns:
            Validated count (non-negative integer or float)
            
        Examples:
            validate_count(5) -> 5
            validate_count(-1) -> 0
            validate_count(None) -> 0
            validate_count("invalid") -> 0
            validate_count(3.5, allow_float=True) -> 3.5
        """
        try:
            # Handle None
            if count is None:
                return default
            
            # Convert to appropriate type
            if allow_float:
                value = float(count)
            else:
                value = int(count)
            
            # Ensure non-negative
            if value < 0:
                LeaderboardDataValidator.logger.warning(f"Negative count detected: {value}, using {default}")
                return default
            
            # Sanity check for extremely large values (potential data corruption)
            max_value = 1_000_000_000 if not allow_float else 525600.0 * 365  # 1 year in minutes
            if value > max_value:
                LeaderboardDataValidator.logger.warning(f"Suspiciously large count: {value}, using {default}")
                return default
            
            return value
            
        except (ValueError, TypeError) as e:
            LeaderboardDataValidator.logger.warning(f"Invalid count value '{count}': {e}, using {default}")
            return default
        except Exception as e:
            LeaderboardDataValidator.logger.error(f"Unexpected error validating count '{count}': {e}", exc_info=True)
            return default
    
    @staticmethod
    def validate_username(username: str, max_length: int = 20, fallback: str = "Unknown User") -> str:
        """
        Validate and truncate username.
        
        Args:
            username: Username to validate
            max_length: Maximum length for username
            fallback: Fallback value if username is invalid
            
        Returns:
            Validated and truncated username
            
        Examples:
            validate_username("John") -> "John"
            validate_username("VeryLongUsername123456") -> "VeryLongUsername1234"
            validate_username("") -> "Unknown User"
            validate_username(None) -> "Unknown User"
        """
        try:
            # Handle None or empty
            if not username:
                return fallback
            
            # Convert to string if not already
            username = str(username)
            
            # Strip whitespace
            username = username.strip()
            
            # Check if empty after stripping
            if not username:
                return fallback
            
            # Truncate if too long
            if len(username) > max_length:
                username = username[:max_length]
            
            # Remove any null bytes or control characters
            username = ''.join(char for char in username if ord(char) >= 32)
            
            # Final check - if we removed everything, use fallback
            if not username:
                return fallback
            
            return username
            
        except Exception as e:
            LeaderboardDataValidator.logger.error(f"Error validating username '{username}': {e}", exc_info=True)
            return fallback
    
    @staticmethod
    def validate_period(period: str) -> str:
        """
        Validate period is one of: daily, weekly, monthly, alltime.
        
        Args:
            period: Period string to validate
            
        Returns:
            Validated period string (lowercase)
            
        Raises:
            ValueError: If period is invalid
            
        Examples:
            validate_period("daily") -> "daily"
            validate_period("WEEKLY") -> "weekly"
            validate_period("invalid") -> raises ValueError
        """
        valid_periods = {'daily', 'weekly', 'monthly', 'alltime'}
        
        if not period:
            raise ValueError("Period cannot be empty")
        
        # Convert to lowercase for case-insensitive comparison
        period_lower = str(period).lower().strip()
        
        if period_lower not in valid_periods:
            raise ValueError(f"Invalid period '{period}'. Must be one of: {', '.join(valid_periods)}")
        
        return period_lower
    
    @staticmethod
    def validate_guild_id(guild_id: Any) -> int:
        """
        Validate guild ID is a valid integer.
        
        Args:
            guild_id: Guild ID to validate
            
        Returns:
            Validated guild ID as integer
            
        Raises:
            ValueError: If guild_id is invalid
        """
        try:
            guild_id_int = int(guild_id)
            if guild_id_int <= 0:
                raise ValueError(f"Guild ID must be positive, got {guild_id_int}")
            return guild_id_int
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid guild_id '{guild_id}': {e}")
    
    @staticmethod
    def validate_user_id(user_id: Any) -> int:
        """
        Validate user ID is a valid integer.
        
        Args:
            user_id: User ID to validate
            
        Returns:
            Validated user ID as integer
            
        Raises:
            ValueError: If user_id is invalid
        """
        try:
            user_id_int = int(user_id)
            if user_id_int <= 0:
                raise ValueError(f"User ID must be positive, got {user_id_int}")
            return user_id_int
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid user_id '{user_id}': {e}")
