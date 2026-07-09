# Purge Cog

Bulk message deletion functionality with advanced filtering options.

## Features

- **Bulk Delete**: Remove up to 100 messages at once
- **Smart Filtering**: Delete by bots, specific users, or your own messages
- **Age Handling**: Automatically skips messages older than 14 days (Discord limitation)
- **Permission Checks**: Validates both user and bot permissions
- **Auto-Cleanup**: Feedback messages auto-delete after 3 seconds
- **Error Handling**: Comprehensive error messages and logging

## Commands

### `.purge <count>`
Delete specified number of messages
- Example: `.purge 50`

### `.purge <count> bots`
Delete only bot messages
- Example: `.purge 20 bots`

### `.purge <count> me`
Delete only your own messages
- Example: `.purge 10 me`

### `.purge <count> @user`
Delete messages from specific user
- Example: `.purge 30 @username`

## Requirements

### User Permissions
- Administrator permission required

### Bot Permissions
- Manage Messages
- Read Message History

## Technical Details

- Maximum 100 messages per command
- Respects Discord's 14-day bulk delete limitation
- Rate limit protection
- Async message fetching with buffer
- Automatic feedback cleanup

## Usage Notes

- Command message is deleted immediately
- Feedback messages auto-delete after 3 seconds
- Skipped old messages are reported in feedback
- Cannot delete messages older than 14 days
