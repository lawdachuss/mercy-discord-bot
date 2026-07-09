# Leaderboard Cog

A dual-tracking leaderboard system for both chat activity and voice channel participation.

## Features

- **Chat Tracking**: Monitor message activity across channels
- **Voice Tracking**: Track time spent in voice channels
- **Multiple Periods**: Daily, weekly, monthly, and all-time statistics
- **Live Updates**: Auto-updating leaderboard messages every 5 minutes
- **Pagination**: Browse through large leaderboards with buttons
- **Archives**: Historical data for past periods
- **Star of the Week**: Highlight top performers
- **Timezone Support**: Configurable timezone for resets
- **Blacklist**: Exclude specific channels from tracking

## Components

### Core Files
- `chat_leaderboard_cog.py`: Chat activity tracking
- `voice_leaderboard_cog.py`: Voice channel tracking
- `star_of_the_week_cog.py`: Weekly highlights
- `leaderboard_config.py`: Configuration and constants
- `utils.py`: Helper functions and formatters
- `validators.py`: Data validation
- `state_manager.py`: State persistence
- `memory_manager.py`: Memory optimization

## Commands

### Chat Leaderboard
- Setup and configuration commands
- View current standings
- Check personal statistics

### Voice Leaderboard
- Similar commands for voice tracking
- Time-based statistics
- Session tracking

## Configuration

Requires:
- `MONGO_URL` environment variable
- Manage Guild permissions for setup
- Read Message History for chat tracking
- View Channel for voice tracking

## Database Collections

- **guild_configs**: Server-specific settings
- **user_stats**: Individual user statistics
- **leaderboard_messages**: Tracked leaderboard message IDs
- **weekly_history**: Historical archives

## Technical Features

- Shared MongoDB connection pooling
- Automatic daily/weekly/monthly resets
- Cache system for performance
- Resilient database operations
- TTL indexes for auto-cleanup
- Compound indexes for fast queries
- Memory leak prevention
