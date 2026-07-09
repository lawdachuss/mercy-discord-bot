"""
Data Validation Module
======================
Centralized validation for all leaderboard input data.
"""

import logging
from typing import Tuple
import pytz

logger = logging.getLogger('discord.bot.leaderboard.validators')


class DataValidator:
    """Validate all input data with clear error messages"""
    
    @staticmethod
    def validate_voice_minutes(minutes: float) -> Tuple[bool, float, str]:
        """
        Validate voice time in minutes.
        
        Args:
            minutes: Voice time in minutes
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        # Check type
        if not isinstance(minutes, (int, float)):
            return False, 0.0, f"Invalid type: expected number, got {type(minutes).__name__}"
        
        # Check if negative
        if minutes < 0:
            logger.warning(f"Negative voice minutes detected: {minutes}, clamping to 0")
            return True, 0.0, ""
        
        # Check if exceeds maximum (7 days = 10,080 minutes)
        MAX_MINUTES = 10080
        if minutes > MAX_MINUTES:
            logger.warning(f"Voice minutes {minutes} exceeds maximum {MAX_MINUTES}, capping")
            return True, float(MAX_MINUTES), f"Capped at {MAX_MINUTES} minutes (7 days)"
        
        return True, float(minutes), ""
    
    @staticmethod
    def validate_chat_count(count: int) -> Tuple[bool, int, str]:
        """
        Validate chat message count.
        
        Args:
            count: Number of messages
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        # Check type
        if not isinstance(count, int):
            try:
                count = int(count)
            except (ValueError, TypeError):
                return False, 0, f"Invalid type: expected integer, got {type(count).__name__}"
        
        # Check if negative
        if count < 0:
            logger.warning(f"Negative chat count detected: {count}, clamping to 0")
            return True, 0, ""
        
        # Sanity check for extremely high values (possible data corruption)
        MAX_COUNT = 100000  # 100k messages per period seems reasonable max
        if count > MAX_COUNT:
            logger.warning(f"Chat count {count} exceeds sanity limit {MAX_COUNT}")
            return True, MAX_COUNT, f"Capped at {MAX_COUNT} messages"
        
        return True, count, ""
    
    @staticmethod
    def validate_timezone(tz_name: str) -> Tuple[bool, str, str]:
        """
        Validate timezone name.
        
        Args:
            tz_name: Timezone name (e.g., 'America/New_York')
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        if not isinstance(tz_name, str):
            return False, 'UTC', f"Invalid type: expected string, got {type(tz_name).__name__}"
        
        if tz_name not in pytz.all_timezones:
            # Try case-insensitive match for common timezone errors
            tz_lower = tz_name.lower()
            for valid_tz in pytz.all_timezones:
                if valid_tz.lower() == tz_lower:
                    logger.info(f"Auto-corrected timezone '{tz_name}' to '{valid_tz}'")
                    return True, valid_tz, ""
            
            logger.warning(f"Invalid timezone '{tz_name}', falling back to UTC")
            return True, 'UTC', f"Invalid timezone '{tz_name}', using UTC"
        
        return True, tz_name, ""
    
    @staticmethod
    def validate_weight(weight: float, name: str = "weight") -> Tuple[bool, float, str]:
        """
        Validate scoring weight values.
        
        Args:
            weight: Weight multiplier
            name: Name of the weight for error messages
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        # Check type
        if not isinstance(weight, (int, float)):
            try:
                weight = float(weight)
            except (ValueError, TypeError):
                return False, 0.0, f"Invalid type for {name}: expected number, got {type(weight).__name__}"
        
        # Check if negative
        if weight < 0:
            logger.warning(f"Negative {name} ({weight}), clamping to 0")
            return True, 0.0, f"Negative {name} not allowed, using 0"
        
        # Check if excessive
        MAX_WEIGHT = 1000
        if weight > MAX_WEIGHT:
            logger.warning(f"Excessive {name} ({weight}), capping at {MAX_WEIGHT}")
            return True, float(MAX_WEIGHT), f"{name} capped at {MAX_WEIGHT}"
        
        return True, float(weight), ""
    
    @staticmethod
    def validate_channel_id(channel_id: int) -> Tuple[bool, int, str]:
        """
        Validate Discord channel ID.
        
        Args:
            channel_id: Discord channel ID
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        if channel_id is None:
            return True, None, ""
        
        # Check type
        if not isinstance(channel_id, int):
            try:
                channel_id = int(channel_id)
            except (ValueError, TypeError):
                return False, None, f"Invalid type: expected integer, got {type(channel_id).__name__}"
        
        # Check if positive
        if channel_id <= 0:
            return False, None, f"Invalid channel ID: must be positive, got {channel_id}"
        
        return True, channel_id, ""
    
    @staticmethod
    def validate_user_id(user_id: int) -> Tuple[bool, int, str]:
        """
        Validate Discord user ID.
        
        Args:
            user_id: Discord user ID
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        # Check type
        if not isinstance(user_id, int):
            try:
                user_id = int(user_id)
            except (ValueError, TypeError):
                return False, None, f"Invalid type: expected integer, got {type(user_id).__name__}"
        
        # Check if positive
        if user_id <= 0:
            return False, None, f"Invalid user ID: must be positive, got {user_id}"
        
        return True, user_id, ""
    
    @staticmethod
    def validate_guild_id(guild_id: int) -> Tuple[bool, int, str]:
        """
        Validate Discord guild ID.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            (is_valid, sanitized_value, error_message)
        """
        # Check type
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return False, None, f"Invalid type: expected integer, got {type(guild_id).__name__}"
        
        # Check if positive
        if guild_id <= 0:
            return False, None, f"Invalid guild ID: must be positive, got {guild_id}"
        
        return True, guild_id, ""
