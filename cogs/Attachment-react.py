import discord
from discord import app_commands
from discord.ext import commands
import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import re
from typing import Optional, List, Dict
from collections import defaultdict

# Simple in-memory rate limit (per channel) -- consider Redis or external store for larger scale
message_timestamps = defaultdict(list)
RATE_LIMIT = 5  # max 5 reactions per 10 seconds per channel
RATE_LIMIT_INTERVAL = 10

class AutoReact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_client = AsyncIOMotorClient(os.getenv('MONGO_URL'))
        self.db = self.mongo_client['discord_bot']
        self.collection = self.db['autoreact_config']

    async def get_server_config(self, guild_id: int):
        """Get auto-react configuration for a server"""
        default_config = {'guild_id': guild_id, 'channels': {}}
        try:
            config = await self.collection.find_one({'guild_id': guild_id})
            return config if config else default_config
        except Exception:
            return default_config

    async def update_server_config(self, guild_id: int, config: dict):
        """Update auto-react configuration for a server"""
        try:
            await self.collection.update_one(
                {'guild_id': guild_id},
                {'$set': config},
                upsert=True
            )
        except Exception as e:
            raise Exception(f"Failed to update configuration: {e}")

    @staticmethod
    def parse_emojis(emoji_string: str) -> List[str]:
        """
        Parse a space-separated emoji string to detect Unicode and custom emojis (including animated)
        Supports both static <:name:id> and animated <a:name:id> Discord emojis
        """
        emojis = []
        # Match custom Discord emojis (static and animated)
        custom_emoji_pattern = r'<a?:\w+:\d+>'
        custom_emojis = re.findall(custom_emoji_pattern, emoji_string)
        emojis.extend(custom_emojis)

        # Remove custom emojis from string to process Unicode emojis
        temp_string = re.sub(custom_emoji_pattern, '', emoji_string)

        # Add Unicode emojis (split by spaces)
        unicode_emojis = [e.strip() for e in temp_string.split() if e.strip()]
        emojis.extend(unicode_emojis)

        return emojis

    def is_rate_limited(self, channel_id: int) -> bool:
        """Check if channel has exceeded rate limit for reactions"""
        now = asyncio.get_event_loop().time()
        timestamps = message_timestamps[channel_id]

        # Remove timestamps older than interval
        message_timestamps[channel_id] = [ts for ts in timestamps if now - ts < RATE_LIMIT_INTERVAL]

        if len(message_timestamps[channel_id]) >= RATE_LIMIT:
            return True

        message_timestamps[channel_id].append(now)
        return False

    @app_commands.command(name="autoreact", description="Manage auto-react system for channels")
    @app_commands.describe(
        action="Choose an action to perform",
        channel="Select a channel to configure",
        emojis="Emojis to react with (supports Unicode and custom/animated emojis)",
        message_type="Type of messages to react to"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="➕ Setup/Update Channel", value="setup"),
        app_commands.Choice(name="❌ Disable Channel", value="disable"),
        app_commands.Choice(name="📋 View Configuration", value="view"),
        app_commands.Choice(name="🗑️ Clear All Configuration", value="clear_all")
    ])
    @app_commands.choices(message_type=[
        app_commands.Choice(name="All Messages with Attachments", value="attachments"),
        app_commands.Choice(name="Messages with Only Attachments (no text)", value="attachments_only"),
        app_commands.Choice(name="All Messages with Text", value="text"),
        app_commands.Choice(name="Messages with Only Text (no attachments)", value="text_only"),
        app_commands.Choice(name="All Messages (text and/or attachments)", value="all")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def autoreact(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
        emojis: Optional[str] = None,
        message_type: Optional[app_commands.Choice[str]] = None
    ):
        """Main auto-react command with dropdown menus"""

        # Defer response to avoid timeout on database operations
        await interaction.response.defer(ephemeral=True, thinking=False)

        # VIEW CONFIGURATION
        if action.value == "view":
            config = await self.get_server_config(interaction.guild.id)

            if not config.get('channels'):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="📋 Auto-React Configuration",
                        description="No channels are currently configured for auto-react.",
                        color=discord.Color.orange()
                    ),
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="📋 Auto-React Configuration",
                description=f"Active auto-react channels in **{interaction.guild.name}**",
                color=discord.Color.blue()
            )

            for channel_id, channel_config in config['channels'].items():
                ch = interaction.guild.get_channel(int(channel_id))
                channel_name = ch.mention if ch else f"Unknown Channel ({channel_id})"

                emoji_display = ' '.join(channel_config.get('emojis', []))
                msg_type = channel_config.get('message_type', 'attachments')

                # Format message type display
                type_display = {
                    'attachments': 'All messages with attachments',
                    'attachments_only': 'Only attachments (no text)',
                    'text': 'All messages with text',
                    'text_only': 'Only text (no attachments)',
                    'all': 'All messages'
                }.get(msg_type, msg_type)

                embed.add_field(
                    name=f"{channel_name}",
                    value=f"**Emojis:** {emoji_display}\n**Type:** {type_display}",
                    inline=False
                )

            embed.set_footer(text=f"Total: {len(config['channels'])} channel(s)")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # CLEAR ALL CONFIGURATION
        if action.value == "clear_all":
            config = await self.get_server_config(interaction.guild.id)

            if not config.get('channels'):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ No Configuration",
                        description="There are no channels configured to clear.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Clear all channels
            config['channels'] = {}
            await self.update_server_config(interaction.guild.id, config)

            await interaction.followup.send(
                embed=discord.Embed(
                    title="✅ Configuration Cleared",
                    description="All auto-react configurations have been removed from this server.",
                    color=discord.Color.green()
                ),
                ephemeral=True
            )
            return

        # SETUP CHANNEL
        if action.value == "setup":
            if not channel:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Missing Channel",
                        description="Please select a channel to setup auto-react.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            if not emojis:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Missing Emojis",
                        description="Please provide emojis to react with.\n\n**Example:** `👍 ❤️ 🔥` or custom emojis, including `<a:animated:ID>`",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            if not message_type:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Missing Message Type",
                        description="Please select the type of messages to react to.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Parse emojis
            parsed_emojis = self.parse_emojis(emojis)

            if not parsed_emojis:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid Emojis",
                        description="Could not parse any valid emojis. Please provide valid Unicode or custom (static/animated) Discord emojis.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Get current config
            config = await self.get_server_config(interaction.guild.id)

            # Update channel configuration
            if 'channels' not in config:
                config['channels'] = {}

            config['channels'][str(channel.id)] = {
                'emojis': parsed_emojis,
                'message_type': message_type.value
            }

            await self.update_server_config(interaction.guild.id, config)

            # Format message type display
            type_display = {
                'attachments': 'All messages with attachments',
                'attachments_only': 'Only attachments (no text)',
                'text': 'All messages with text',
                'text_only': 'Only text (no attachments)',
                'all': 'All messages'
            }.get(message_type.value, message_type.value)

            embed = discord.Embed(
                title="✅ Auto-React Setup Complete",
                description=f"Auto-react has been configured for {channel.mention}",
                color=discord.Color.green()
            )
            embed.add_field(name="Emojis", value=' '.join(parsed_emojis), inline=False)
            embed.add_field(name="Message Type", value=type_display, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # DISABLE CHANNEL
        if action.value == "disable":
            if not channel:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Missing Channel",
                        description="Please select a channel to disable auto-react.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            config = await self.get_server_config(interaction.guild.id)

            if str(channel.id) not in config.get('channels', {}):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Channel Not Configured",
                        description=f"{channel.mention} is not currently configured for auto-react.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Remove channel from config
            del config['channels'][str(channel.id)]
            await self.update_server_config(interaction.guild.id, config)

            await interaction.followup.send(
                embed=discord.Embed(
                    title="✅ Auto-React Disabled",
                    description=f"Auto-react has been disabled for {channel.mention}",
                    color=discord.Color.green()
                ),
                ephemeral=True
            )
            return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for messages and auto-react based on configuration"""

        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return

        # Get server configuration (already has timeout protection via safe_find_one)
        config = await self.get_server_config(message.guild.id)
        
        # If config is None (timeout/error), skip this message
        if not config:
            return

        # Check if this channel is configured
        channel_config = config.get('channels', {}).get(str(message.channel.id))

        if not channel_config:
            return

        # Check rate limit
        if self.is_rate_limited(message.channel.id):
            return  # Too many reactions in short period

        emojis = channel_config.get('emojis', [])
        message_type = channel_config.get('message_type', 'attachments')

        # Determine if we should react based on message type
        should_react = False
        has_attachments = any([a for a in message.attachments if hasattr(a, 'url')])
        has_text = bool(message.content.strip())

        if message_type == 'attachments':
            should_react = has_attachments
        elif message_type == 'attachments_only':
            should_react = has_attachments and not has_text
        elif message_type == 'text':
            should_react = has_text
        elif message_type == 'text_only':
            should_react = has_text and not has_attachments
        elif message_type == 'all':
            should_react = True

        if not should_react:
            return

        # React with all configured emojis (with error handling)
        for emoji in emojis:
            try:
                await message.add_reaction(emoji)
            except discord.Forbidden:
                # Missing permissions; break loop to avoid further errors
                print(f"[AutoReact] Missing permissions to react in {message.channel}. Skipping.")
                break
            except discord.NotFound:
                # Emoji or message deleted
                continue
            except discord.HTTPException:
                # Spam or unknown error
                continue
            except Exception as e:
                # Catch any other unexpected errors
                print(f"[AutoReact] Error adding reaction: {e}")
                continue

    @autoreact.error
    async def autoreact_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Error handler for autoreact command"""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Missing Permissions",
                    description="You need **Administrator** permission to use this command.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description=f"An error occurred: {str(error)}",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(AutoReact(bot))
