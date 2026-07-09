# Quarantine System

A comprehensive quarantine/mute system with an integrated appeal system for Discord bots, optimized for servers of all sizes.

## Structure

```
cogs/quarantine/
├── __init__.py           # Package initialization
├── config.py            # Configuration and constants
├── quarantine_system.py  # Main quarantine/mute system
├── appeal_system.py      # Appeal system for punishments
└── README.md            # This file
```

## Features

### Quarantine System (`quarantine_system.py`)
- **Mute Management**: Comprehensive mute system with role-based permissions
- **Jail Channel**: Isolated channel for muted users
- **Case Tracking**: Each mute gets a unique case ID
- **Temporary Mutes**: Support for time-based mutes (e.g., 10m, 2h, 1d)
- **Auto-Unmute**: Background task to automatically unmute users when time expires
- **DM Notifications**: Automatic DM notifications to muted users
- **Jail History**: Track messages sent by muted users in jail
- **Permission Management**: Automatic channel permission overwrites

#### Commands
- `/setup-mute` - Initialize the mute system (creates roles, channels)
- `/check-muteperms` - Verify system configuration
- `/reset-muteconfig` - Reset system configuration
- `/reapply-mute-perms` - Reapply permission overwrites
- `!setmodrole <role>` - Set moderator role
- `!qmute <user> [duration] [reason]` - Mute a user
- `!qunmute <user>` - Unmute a user
- `!mutelist` - List all currently muted users
- `!clearmutes [days]` - Clean up old mute records
- `!jailhistory <user> [limit]` - View user's jail messages
- `!case <case_id>` - View details of a specific case

### Appeal System (`appeal_system.py`)
- **Modern UI**: Uses Discord modals and buttons for a smooth UX
- **Appeal Submission**: Users can appeal their punishments via modal
- **Cooldown System**: 24-hour cooldown between appeal submissions
- **Review Interface**: Interactive buttons for moderators to approve/deny
- **Status Tracking**: Track appeal status (pending, approved, denied, expired)
- **Auto-Expiration**: Appeals expire after 7 days if not reviewed
- **Notifications**: Automatic notifications to users and moderators
- **Integration**: Seamlessly integrates with quarantine system

#### Appeal Commands
- `/appeal <case_id>` - Submit an appeal for a case
- `/appeal-status [appeal_id]` - Check your appeal status
- `/appeal-list` - [MOD] List all pending appeals
- `/appeal-review <appeal_id>` - [MOD] Review a specific appeal

## Large Server Optimizations

### Dynamic Rate Limit Handling
- Adaptive sleep times based on server size
  - Small servers (<100 channels): 0.3s delay
  - Large servers (100-200 channels): 0.4s delay
  - Very large servers (>200 channels): 0.5s delay
- Smart rate limit handling with retry mechanism
- Random jitter to prevent thundering herd

### Performance Estimates
| Server Size | Channels | Estimated Time | Rate Limit Risk |
|-------------|----------|----------------|-----------------|
| Small       | <50      | 15-30 seconds  | Very Low        |
| Medium      | 50-100   | 30-60 seconds  | Low             |
| Large       | 100-200  | 1-2 minutes    | Low             |

### Error Recovery
- Exponential backoff for non-429 HTTP errors
- Graceful degradation with failed channel tracking
- Ability to retry failed channels using `/reapply-mute-perms`

## Configuration

Edit `config.py` to customize:

```python
APPEAL_COOLDOWN_HOURS = 24          # Hours between appeals
MAX_APPEAL_LENGTH = 1000            # Max characters in appeal
APPEAL_REVIEW_TIMEOUT_DAYS = 7      # Days before appeal expires
```

## Database Collections

### Quarantine System
- `guild_configs` - Server configurations
- `mutes` - Mute records
- `jail_messages` - Messages sent in jail (TTL: 7 days)
- `guild_counters` - Case ID counters
- `pending_dm_deletes` - Scheduled DM deletions

### Appeal System
- `appeals` - Appeal records
- `appeal_messages` - Appeal notification messages
- `appeal_counters` - Appeal ID counters

## Setup

1. Ensure MongoDB is configured in your `.env` file:
   ```
   MONGO_URL=mongodb://...
   ```

2. Run `/setup-mute` in your Discord server to initialize the system

3. Set a moderator role with `!setmodrole @ModRole`

4. The appeal system will automatically integrate with the quarantine system

## Permissions Required

- **Manage Roles** - To assign/remove muted role
- **Manage Channels** - To edit channel permission overwrites
- **Manage Messages** - For jail message management
- **Send Messages** - To send notifications
- **Embed Links** - For rich embeds