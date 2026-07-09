# Skullboard

A reaction-based message highlighting system similar to starboard, but with skull emojis and dark theming.

## Features

- **Reaction Tracking**: Messages that reach threshold get posted to skullboard
- **Custom Emoji**: Configurable skull emoji (unicode or custom)
- **Threshold System**: Set minimum reactions required
- **Auto-Threading**: Optional thread creation for popular posts
- **Milestones**: Announcements at 10, 25, 50 reactions
- **Statistics**: Track top authors and reactors
- **Blacklists**: Exclude specific users or channels
- **Live Updates**: Real-time count updates on skullboard posts
- **Anti-Spam**: Prevents rapid reaction toggles
- **Media Support**: Full support for images, GIFs, videos, and attachments

## Commands

### `/skull-setup <channel> [emoji] [threshold]`
Initial skullboard setup (Admin only)
- `channel`: Channel for skullboard posts
- `emoji`: Custom emoji (default: 💀)
- `threshold`: Minimum reactions (default: 2)

### `/skull-config`
View current configuration

### `/skull-blacklist-user <user>`
Toggle user blacklist

### `/skull-blacklist-channel <channel>`
Toggle channel blacklist

### `/skull-stats [type]`
View leaderboard statistics
- `type`: "authors" or "reactors"

### `/skull-remove`
Remove skullboard configuration (Admin only)

### `/skull-style <style>`
Set embed style (detailed or compact)

### `/skull-autothread <enabled>`
Toggle automatic thread creation

### `/skull-milestones <enabled>`
Toggle milestone announcements

## Configuration

Requires:
- `MONGO_URL` environment variable
- Administrator permissions for setup
- Manage Messages permission
- Add Reactions permission
- Create Public Threads permission (if autothread enabled)

## Database Collections

- **configs**: Guild-specific configurations
- **message_maps**: Original message to skullboard post mapping
- **stats**: Author and reactor statistics
- **blacklists**: User and channel blacklists

## Technical Features

- Per-guild locks for race condition prevention
- Memory cache with TTL and size limits
- Anti-spam protection (3-second cooldown)
- Automatic cache cleanup
- Emoji validation and normalization
- Support for animated custom emojis
- Milestone system (10, 25, 50 reactions)
- Dark-themed embeds (#2f3136)
- Random footer quotes
- Emoji-only message filtering
- Full media attachment support

## Behavior

- Messages below threshold show "No longer qualifies" status
- Skullboard posts update in real-time as reactions change
- Bot automatically adds skull reaction to skullboard posts
- Emoji-only messages are ignored (but GIFs/media are allowed)
- Bot messages are always ignored
- Blacklisted users/channels are silently skipped
