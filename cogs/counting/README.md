# Counting Cog

A production-ready counting game system for Discord servers with MongoDB integration.

## Features

- **Counting Game**: Members count sequentially in a designated channel
- **Validation**: Prevents same user counting twice in a row, validates number sequence
- **Reaction System**: Adds custom emoji reactions to correct counts
- **Statistics**: Tracks individual user contributions and server records
- **Auto-Reset**: Resets count when wrong number is posted
- **Rate Limiting**: Built-in protection against spam and rate limits
- **Banned Users**: Ability to ban users from participating
- **Logging**: Comprehensive logging system for moderation

## Commands

### `/count view`
View counting statistics and leaderboard
- Shows current count, next number, record
- Displays top contributors
- Shows banned users count

### `/count settings`
Configure counting system (Admin only)
- Set counting channel
- Set log channel
- Configure custom reaction emoji
- Reset count or jump to specific number
- Ban/unban users from counting

## Configuration

The cog uses MongoDB for data persistence and requires:
- `MONGO_URL` environment variable
- Administrator permissions for setup
- Manage Messages permission for the bot

## Database Structure

- **counting_data**: Stores guild configurations, counts, and user statistics
- **backups**: Automatic backups with TTL (30 days retention)

## Technical Features

- Async MongoDB with motor
- Connection pooling optimization
- Memory leak prevention
- Exponential backoff for rate limits
- Queue system for message deletions
- Automatic cache cleanup
