"""
Safe Formatting Module for Leaderboard System
==============================================
Ensures consistent, safe formatting of all display values.

This module provides:
- Number formatting with comma separators
- Time formatting (minutes to human-readable)
- Rank formatting with zero-padding
- Period display formatting
- Discord markdown escaping
"""

from typing import Any
import logging
from datetime import datetime
import pytz


class SafeFormatter:
    """Safe formatting utilities for leaderboard display"""
    
    logger = logging.getLogger('discord.bot.leaderboard.formatter')
    
    @staticmethod
    def format_count(count: Any) -> str:
        """
        Format count with comma separators.
        
        Args:
            count: Count value to format (int or float)
            
        Returns:
            Formatted string with comma separators
            
        Examples:
            format_count(1234) -> "1,234"
            format_count(1000000) -> "1,000,000"
            format_count(0) -> "0"
            format_count(-5) -> "0"
            format_count(None) -> "0"
        """
        try:
            # Convert to int
            if count is None:
                count = 0
            
            count_int = int(count)
            
            # Ensure non-negative
            if count_int < 0:
                SafeFormatter.logger.warning(f"Negative count in format_count: {count_int}")
                count_int = 0
            
            # Format with comma separators
            return f"{count_int:,}"
            
        except (ValueError, TypeError) as e:
            SafeFormatter.logger.warning(f"Error formatting count '{count}': {e}")
            return "0"
        except Exception as e:
            SafeFormatter.logger.error(f"Unexpected error formatting count '{count}': {e}", exc_info=True)
            return "0"
    
    @staticmethod
    def format_voice_time(minutes: Any) -> str:
        """
        Format minutes as human-readable time.
        
        Args:
            minutes: Time in minutes (float or int)
            
        Returns:
            Formatted time string
            
        Examples:
            format_voice_time(0) -> "0m"
            format_voice_time(45) -> "45m"
            format_voice_time(90) -> "1h 30m"
            format_voice_time(120) -> "2 hours"
            format_voice_time(1440) -> "24 hours"
            format_voice_time(165.5) -> "2h 45m"
        """
        try:
            # Convert to float
            if minutes is None:
                minutes = 0.0
            
            minutes_float = float(minutes)
            
            # Ensure non-negative
            if minutes_float < 0:
                SafeFormatter.logger.warning(f"Negative minutes in format_voice_time: {minutes_float}")
                minutes_float = 0.0
            
            # Round to nearest minute
            minutes_int = round(minutes_float)
            
            # Handle zero
            if minutes_int == 0:
                return "0m"
            
            # Calculate hours and remaining minutes
            hours = minutes_int // 60
            remaining_minutes = minutes_int % 60
            
            # Format based on duration
            if hours == 0:
                # Less than 1 hour - show minutes only
                return f"{remaining_minutes}m"
            elif hours == 1 and remaining_minutes == 0:
                # Exactly 1 hour
                return "1 hour"
            elif remaining_minutes == 0:
                # Whole hours
                return f"{hours} hours"
            elif hours < 10:
                # Less than 10 hours - show hours and minutes
                return f"{hours}h {remaining_minutes}m"
            else:
                # 10+ hours - show hours only for brevity
                return f"{hours} hours"
            
        except (ValueError, TypeError) as e:
            SafeFormatter.logger.warning(f"Error formatting voice time '{minutes}': {e}")
            return "0m"
        except Exception as e:
            SafeFormatter.logger.error(f"Unexpected error formatting voice time '{minutes}': {e}", exc_info=True)
            return "0m"
    
    @staticmethod
    def format_rank(rank: Any) -> str:
        """
        Format rank with zero-padding (2 digits).
        
        Args:
            rank: Rank number to format
            
        Returns:
            Zero-padded rank string
            
        Examples:
            format_rank(1) -> "01"
            format_rank(5) -> "05"
            format_rank(10) -> "10"
            format_rank(99) -> "99"
            format_rank(100) -> "100"
        """
        try:
            # Convert to int
            if rank is None:
                rank = 0
            
            rank_int = int(rank)
            
            # Ensure positive
            if rank_int < 1:
                SafeFormatter.logger.warning(f"Invalid rank in format_rank: {rank_int}")
                rank_int = 1
            
            # Format with zero-padding (minimum 2 digits)
            return f"{rank_int:02d}"
            
        except (ValueError, TypeError) as e:
            SafeFormatter.logger.warning(f"Error formatting rank '{rank}': {e}")
            return "01"
        except Exception as e:
            SafeFormatter.logger.error(f"Unexpected error formatting rank '{rank}': {e}", exc_info=True)
            return "01"
    
    @staticmethod
    def format_period_display(period: str, timezone: str = 'UTC') -> str:
        """
        Format period display string.
        
        Args:
            period: Period type (daily, weekly, monthly)
            timezone: Timezone for date formatting
            
        Returns:
            Formatted period display string
            
        Examples:
            format_period_display('daily') -> "Today"
            format_period_display('weekly') -> "This Week"
            format_period_display('monthly', 'UTC') -> "December 2025"
        """
        try:
            period_lower = str(period).lower().strip()
            
            if period_lower == 'daily':
                return "Today"
            elif period_lower == 'weekly':
                return "This Week"
            elif period_lower == 'monthly':
                # Get current month and year in specified timezone
                try:
                    tz = pytz.timezone(timezone)
                    now = datetime.now(tz)
                    return now.strftime('%B %Y')
                except Exception as e:
                    SafeFormatter.logger.warning(f"Error with timezone '{timezone}': {e}, using UTC")
                    now = datetime.utcnow()
                    return now.strftime('%B %Y')
            elif period_lower == 'alltime':
                return "All Time"
            else:
                SafeFormatter.logger.warning(f"Unknown period '{period}'")
                return "Unknown"
            
        except Exception as e:
            SafeFormatter.logger.error(f"Error formatting period display '{period}': {e}", exc_info=True)
            return "Unknown"
    
    @staticmethod
    def escape_markdown(text: str) -> str:
        """
        Escape Discord markdown special characters.
        
        Args:
            text: Text to escape
            
        Returns:
            Escaped text safe for Discord
            
        Examples:
            escape_markdown("test") -> "test"
            escape_markdown("test*bold*") -> "test\\*bold\\*"
            escape_markdown("test_italic_") -> "test\\_italic\\_"
            
        Note:
            Escapes: * _ ~ ` | > # @
        """
        try:
            if not text:
                return ""
            
            text = str(text)
            
            # Characters that need escaping in Discord markdown
            # Note: We're being conservative and escaping more than strictly necessary
            # to ensure safety across all contexts
            escape_chars = {
                '*': '\\*',   # Bold/italic
                '_': '\\_',   # Italic/underline
                '~': '\\~',   # Strikethrough
                '`': '\\`',   # Code
                '|': '\\|',   # Spoiler
                '>': '\\>',   # Quote
                '#': '\\#',   # Header
                '@': '\\@',   # Mention (prevent accidental mentions)
            }
            
            # Replace each special character
            for char, escaped in escape_chars.items():
                text = text.replace(char, escaped)
            
            return text
            
        except Exception as e:
            SafeFormatter.logger.error(f"Error escaping markdown for '{text}': {e}", exc_info=True)
            # Return original text if escaping fails
            return str(text) if text else ""
    
    @staticmethod
    def format_timestamp(timestamp: Any) -> str:
        """
        Format Unix timestamp for Discord.
        
        Args:
            timestamp: Unix timestamp (int or float)
            
        Returns:
            Formatted Discord timestamp string
            
        Examples:
            format_timestamp(1234567890) -> "1234567890"
            format_timestamp(None) -> "0"
        """
        try:
            if timestamp is None:
                return "0"
            
            timestamp_int = int(timestamp)
            
            # Ensure positive
            if timestamp_int < 0:
                SafeFormatter.logger.warning(f"Negative timestamp: {timestamp_int}")
                return "0"
            
            return str(timestamp_int)
            
        except (ValueError, TypeError) as e:
            SafeFormatter.logger.warning(f"Error formatting timestamp '{timestamp}': {e}")
            return "0"
        except Exception as e:
            SafeFormatter.logger.error(f"Unexpected error formatting timestamp '{timestamp}': {e}", exc_info=True)
            return "0"
    
    @staticmethod
    def sanitize_for_embed(text: str, max_length: int = 4096) -> str:
        """
        Sanitize text for Discord embed (remove null bytes, limit length).
        
        Args:
            text: Text to sanitize
            max_length: Maximum allowed length
            
        Returns:
            Sanitized text safe for Discord embeds
        """
        try:
            if not text:
                return ""
            
            text = str(text)
            
            # Remove null bytes and other problematic characters
            text = text.replace('\x00', '')
            text = ''.join(char for char in text if ord(char) >= 32 or char in '\n\r\t')
            
            # Limit length
            if len(text) > max_length:
                text = text[:max_length - 3] + "..."
                SafeFormatter.logger.warning(f"Text truncated to {max_length} characters")
            
            return text
            
        except Exception as e:
            SafeFormatter.logger.error(f"Error sanitizing text: {e}", exc_info=True)
            return ""
    
    @staticmethod
    def format_percentage(value: float, total: float, decimals: int = 1) -> str:
        """
        Format percentage safely.
        
        Args:
            value: Numerator value
            total: Denominator value
            decimals: Number of decimal places
            
        Returns:
            Formatted percentage string
            
        Examples:
            format_percentage(25, 100) -> "25.0%"
            format_percentage(1, 3) -> "33.3%"
            format_percentage(0, 0) -> "0.0%"
        """
        try:
            if total == 0:
                return f"0.{'0' * decimals}%"
            
            percentage = (value / total) * 100
            return f"{percentage:.{decimals}f}%"
            
        except Exception as e:
            SafeFormatter.logger.error(f"Error formatting percentage: {e}", exc_info=True)
            return f"0.{'0' * decimals}%"
