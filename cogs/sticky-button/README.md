# Sticky Button System

A persistent message system that stays at the bottom of channels with interactive buttons that display custom embeds.

## Features

- **Sticky Messages**: Messages that automatically repost to stay at channel bottom
- **Interactive Buttons**: Up to 20 buttons per sticky message
- **Discohook Integration**: Import embeds using Discohook JSON format
- **Auto-Repost**: Reposts when new messages are sent (stays at bottom)
- **Periodic Check**: Ensures stickies remain at bottom every minute
- **Button Management**: Add, remove, and list buttons easily
- **Ephemeral Responses**: Button clicks show content only to the user
- **Rate Limiting**: Built-in protection against spam
- **Recovery System**: Automatically recovers sticky messages on bot restart

## Commands

### `/sticky-manage`
Opens interactive management menu (Admin only)

Available actions:
- **Setup / Edit sticky text**: Create or update the sticky message
- **Add Button**: Add a new button with Discohook JSON
- **Remove Button**: Remove a button by its number
- **List Buttons**: View all configured buttons
- **Remove sticky**: Delete sticky from channel

## Button Configuration

### Adding a Button
1. Use `/sticky-manage` and select "Add Button"
2. Provide:
   - **Emoji** (optional): Unicode or custom emoji
   - **Label** (optional): Text label for button
   - **Discohook JSON**: Paste JSON from discohook.org
3. At least emoji OR label is required

### Discohook JSON Format
```json
{
  "embeds": [
    {
      "title": "Example Title",
      "description": "Example description",
      "color": 3092790,
      "fields": [
        {
          "name": "Field Name",
          "value": "Field Value",
          "inline": false
        }
      ]
    }
  ]
}
```

## Requirements

### Bot Permissions
- Send Messages
- Manage Messages
- Read Message History
- Embed Links

### User Permissions
- Administrator (for management commands)

## Configuration

Requires:
- `MONGO_URL` environment variable
- Motor (async MongoDB driver)

## Database Structure

### Stickies Collection
```json
{
  "guild_id": 123456789,
  "channel_id": 987654321,
  "text": "Sticky message text",
  "buttons": [
    {
      "emoji": "💬",
      "label": "Info",
      "embed_data": [...]
    }
  ],
  "last_repost": "2024-01-01T00:00:00Z"
}
```

## Technical Features

- **Queue System**: Prevents race conditions with repost queue
- **Rate Limiting**: Per-channel rate limits (5 seconds)
- **Memory Management**: Automatic cleanup of old data
- **Persistent Views**: Buttons work after bot restart
- **Error Recovery**: Automatic recovery of sticky messages
- **Validation**: JSON validation and Discord limit checks
- **Duplicate Prevention**: Prevents duplicate view registration

## Limits

- Maximum 20 buttons per sticky (Discord limit: 25 components)
- Sticky text: 2000 characters max
- Button label: 80 characters max
- Embed title: 256 characters max
- Embed description: 4096 characters max
- Fields per embed: 25 max
- Embeds per button: 10 max (Discord limit)

## Behavior

- Sticky reposts when any message is sent in the channel
- Old sticky message is deleted before reposting
- Periodic check every 1 minute ensures sticky stays at bottom
- Button clicks are ephemeral (only visible to clicker)
- Supports multiple embeds per button
- Handles GIFs, images, and other media in embeds

## Notes

- Buttons can have duplicate emojis (uses index-based identification)
- Removing a button renumbers subsequent buttons
- Sticky messages persist across bot restarts
- Views are automatically re-registered on bot startup
