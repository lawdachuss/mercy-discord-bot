# Discord Bot - Mercy

A feature-rich Discord bot built with discord.py, featuring multiple cogs for server management, entertainment, and community engagement.

## Features

- **Leaderboards**: Chat and voice activity tracking with weekly star recognition
- **Giveaways**: Advanced giveaway system with role requirements
- **Moderation**: Ban, purge, quarantine, and other moderation tools
- **Utility**: AFK status, snipe, sticky messages, thread management
- **Reminders**: interactive admin dashboard for one-off and recurring scheduled reminders with timezone support and role pings
- **Fun**: Media commands, autoresponder, confession system
- **Voice**: Voice channel management and matchmaking
- **Counting & Skullboard**: Community engagement features

## Setup

### Prerequisites

- Python 3.8+
- MongoDB database
- Discord Bot Token

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd mercy
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with the following variables:
```env
DISCORD_TOKEN=your_discord_bot_token
WEBHOOK_URL=your_webhook_url
MONGO_URL=your_mongodb_connection_string
OWNER_IDS=your_discord_user_id
AUTO_SYNC_COMMANDS=true
```

4. Run the bot:
```bash
python main.py
```

## Bot Commands

### Slash Commands

#### Reminders (`/reminder` · Manage Server required)

- `/reminder dashboard` - Interactive dashboard: create, manage, pause and resume reminders
- `/reminder create` - Six-step wizard (details, frequency, timing, timezone, channel, role)
- `/reminder manage` - Pick a reminder to edit, reschedule, preview, test, pause or delete
- `/reminder list` - Paginated listing of every reminder in the server
- `/reminder preview <reminder>` - Show the exact message without posting it
- `/reminder pause|resume|delete <reminder>` - Per-reminder actions with confirmation

Reminders are stored in MongoDB, survive restarts, support one-off and recurring
schedules (interval, daily, weekly, monthly) with IANA timezones, and ping an
optional role. See [cogs/reminders/README.md](cogs/reminders/README.md).

### Prefix Commands (`.`)

- `.ping` - Check bot latency and database status
- `.sync` - Manually sync slash commands (owner only)
- `.reload [cog]` - Reload specific cog or all cogs (owner only)
- `.cogs` - List all loaded cogs (owner only)
- `.syncstatus` - Check command sync status (owner only)
- `.listcommands` - List all registered slash commands (owner only)
- `.clearglobal` - Clear all global slash commands (owner only)

## Project Structure

```
mercy/
├── main.py              # Bot entry point
├── cogs/                # Feature modules
│   ├── leaderboard/     # Activity tracking
│   ├── giveaways/       # Giveaway system
│   ├── quarantine/      # Moderation system
│   ├── purge/           # Message purging
│   ├── counting/        # Counting game
│   ├── skullboard/      # Message highlighting
│   ├── reminders/       # Scheduled reminders + interactive dashboard
│   └── ...              # Other features
├── logs/                # Bot logs
└── database/            # Local database files
```

## Tests

```bash
python -m pytest tests -q
```

## Contributing

This is a private project. Contact the owner for contribution guidelines.

## License

Private - All Rights Reserved
