"""
Leaderboard System Initialization
==================================
Ensures bulletproof operation with automatic recovery.
"""

import logging
from .state_manager import RecoveryManager

logger = logging.getLogger('discord.bot.leaderboard')

async def setup(bot):
    """
    Setup all leaderboard cogs with recovery system.
    This ensures nothing is ever missed.
    """
    # Import and load all cogs
    from .chat_leaderboard_cog import ChatLeaderboardCog
    from .voice_leaderboard_cog import VoiceLeaderboardCog
    from .star_of_the_week_cog import StarOfTheWeekCog
    from .debug_helper import DebugCommands
    
    # Load cogs
    await bot.add_cog(ChatLeaderboardCog(bot))
    logger.info("✅ Chat leaderboard cog loaded")
    
    await bot.add_cog(VoiceLeaderboardCog(bot))
    logger.info("✅ Voice leaderboard cog loaded")
    
    await bot.add_cog(StarOfTheWeekCog(bot))
    logger.info("✅ Star of the Week cog loaded")
    
    await bot.add_cog(DebugCommands(bot))
    logger.info("✅ Debug commands loaded")
    
    # Note: Recovery check runs automatically in StarOfTheWeekCog.on_ready()
    # Each cog has its own on_ready listener to start tasks independently
    # This prevents event handler conflicts and ensures proper initialization
    
    logger.info("🛡️ BULLETPROOF LEADERBOARD SYSTEM INITIALIZED")
    logger.info("📊 Checks run every 5 minutes")
    logger.info("♻️ Automatic recovery enabled")
    logger.info("💾 Persistent state tracking active")
