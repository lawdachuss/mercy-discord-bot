# Giveaways Cog

A comprehensive giveaway system with reaction-based entry, fake participant filling, and advanced management features.

## Features

- **Reaction-Based Entry**: Users join by reacting with the giveaway emoji
- **Multiple Winners**: Support for selecting multiple winners
- **Fake Participants**: Fill giveaways with fake reactions for testing/appearance
- **Force Winners**: Manually select specific users as winners
- **Extend Duration**: Modify giveaway duration while active
- **Statistics**: Track giveaway history and participation
- **Pagination**: Browse entries with paginated views
- **Auto-End**: Automatic winner selection when giveaway ends

## Commands

### `/giveaway-edit`
Interactive management menu (Admin only)
- View statistics
- Fill with fake reactions
- Force specific winners
- Extend giveaway duration
- Cancel active giveaways

## Components

### Core Files
- `giveaway_core.py`: Main giveaway logic and database management
- `giveaway_admin.py`: Administrative commands and controls
- `config.py`: Configuration constants and settings

## Configuration

Requires:
- `MONGO_URL` environment variable
- Administrator permissions for management
- Manage Messages permission for the bot

## Database Collections

- **giveaways**: Active and ended giveaway data
- **participants**: User entries (real and fake)
- **fake_reactions**: Fake participant tracking
- **giveaway_stats**: Server-wide statistics
- **giveaway_history**: Historical tracking

## Technical Features

- Optimized for large servers (10k+ members)
- Connection pooling with retry logic
- Background tasks for auto-ending
- Persistent views for buttons
- Rate limit protection
- Comprehensive error handling
