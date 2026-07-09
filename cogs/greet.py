import discord
import logging
import random
from discord.ext import commands
from discord import app_commands
from typing import Optional, List
from datetime import datetime, timedelta

# Setup logging
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class GreetingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.default_greetings = [
            "<a:sukoon_Watermelon:1325703602379161676> Welcome, {user}, to {server}! A new journey begins—make yourself at home.",
            "<a:sukoon_white_bo:1335856241011855430> Hey {user}, you've arrived at {server}! Breathe in the good vibes and enjoy your stay.",
            "<a:sukoon_bandaid:1323990361647087729> {user}, welcome to {server}! A space to connect, share, and grow together.",
            "<:sukoon_btfl:1335856043477041204> Welcome aboard, {user}! {server} is a place of warmth and camaraderie—glad you're here.",
            "<a:sukoon_butterfly:1323990263609298967> Hi {user}, you've found your way to {server}. Let's create wonderful memories together!",
            "<a:sukoon_:1335855101897609226> {user}, welcome to {server}! You bring new energy to our growing community of {member_count}.",
            "<a:sukoon_rabbi:1335855768301473812> Glad to have you here, {user}! {server} just got brighter with your presence.",
            "<a:sukoon_yflower:1323990499660664883> A warm welcome to you, {user}! {server} is now {member_count} strong—let's make it unforgettable.",
            "<:sukoon_starr:1335855541335097408> Hello {user}, welcome to {server}! Let the conversations flow and friendships grow.",
            "<a:heartspar:1335854160322498653> {user}, you're now part of {server}! Settle in, unwind, and enjoy the journey."
        ]

    async def setup_database(self):
        self.greeting_channels = self.bot.mongo_client['discord_bot']['greeting_channels']
        self.greeting_history = self.bot.mongo_client['discord_bot']['greeting_history']

    async def cog_load(self):
        await self.setup_database()

    @app_commands.command(name="greet_enable")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="The channel where greetings will be sent",
        custom_message="Optional custom greeting message",
        cooldown="Cooldown in seconds between greetings (default: 60)",
        delete_after="Seconds before deleting greeting (0 for permanent, default: 60)"
    )
    async def greet_enable(
        self, 
        interaction: discord.Interaction, 
        channel: discord.TextChannel,
        custom_message: Optional[str] = None,
        cooldown: Optional[int] = 60,
        delete_after: Optional[int] = 60
    ):
        """Enable greetings in a channel with optional custom message, cooldown, and auto-delete"""
        try:
            # Verify bot permissions in the channel
            permissions = channel.permissions_for(interaction.guild.me)
            if not permissions.send_messages:
                await interaction.response.send_message(
                    f"<:sukoon_info:1323251063910043659> | I don't have permission to send messages in {channel.mention}",
                    ephemeral=True
                )
                return

            # Check if channel already exists
            existing = await self.greeting_channels.find_one({
                "guild_id": interaction.guild.id,
                "channel_id": channel.id
            })
            action = "updated" if existing else "added"

            greeting = custom_message if custom_message else None
            await self.greeting_channels.update_one(
                {"guild_id": interaction.guild.id, "channel_id": channel.id},
                {"$set": {
                    "greeting_message": greeting,
                    "cooldown_time": cooldown,
                    "delete_after": delete_after,
                    "enabled": True
                }},
                upsert=True
            )

            delete_msg = "Permanent (no auto-delete)" if delete_after == 0 else f"{delete_after} seconds"
            response_message = (
                f"<a:sukoon_whitetick:1323992464058482729> | Greetings {action} for {channel.mention}\n"
                f"Cooldown: {cooldown} seconds\n"
                f"Delete after: {delete_msg}\n"
            )
            if greeting:
                response_message += f"Custom message: {greeting}"
            else:
                response_message += "Using randomized default greetings"

            await interaction.response.send_message(
                response_message,
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"Error enabling greeting: {e}", exc_info=True)
            await interaction.response.send_message(
                "<:sukoon_info:1323251063910043659> | An error occurred while enabling greetings",
                ephemeral=True
            )

    @app_commands.command(name="greet_disable")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="The channel to disable greetings in")
    async def greet_disable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Disable greetings in a specific channel"""
        try:
            result = await self.greeting_channels.update_one(
                {"guild_id": interaction.guild.id, "channel_id": channel.id},
                {"$set": {"enabled": False}}
            )
            if result.matched_count == 0:
                await interaction.response.send_message(
                    f"<:sukoon_info:1323251063910043659> | Greetings not enabled in {channel.mention}",
                    ephemeral=True
                )
                return
            await interaction.response.send_message(
                f"<a:sukoon_whitetick:1323992464058482729> | Greetings disabled in {channel.mention}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error disabling greeting: {e}", exc_info=True)
            await interaction.response.send_message(
                "<:sukoon_info:1323251063910043659> | An error occurred while disabling greetings",
                ephemeral=True
            )

    @app_commands.command(name="greet_list")
    @app_commands.checks.has_permissions(administrator=True)
    async def greet_list(self, interaction: discord.Interaction):
        """List all configured greeting channels"""
        try:
            cursor = self.greeting_channels.find(
                {"guild_id": interaction.guild.id, "enabled": True},
                {"channel_id": 1, "greeting_message": 1, "cooldown_time": 1, "delete_after": 1, "error_count": 1}
            )
            channels = await cursor.to_list(length=None)

            if not channels:
                await interaction.response.send_message(
                    "No active greeting channels configured",
                    ephemeral=True
                )
                return

            response = ["**Configured Greeting Channels:**"]
            for channel_data in channels:
                channel = interaction.guild.get_channel(channel_data["channel_id"])
                name = channel.mention if channel else f"Deleted Channel ({channel_data['channel_id']})"
                message_text = channel_data.get("greeting_message") if channel_data.get("greeting_message") is not None else "Random default greetings"
                truncated = message_text[:50] + '...' if len(message_text) > 50 else message_text
                delete_msg = "Permanent" if channel_data.get("delete_after", 60) == 0 else f"{channel_data.get('delete_after', 60)}s"
                response.append(
                    f"{name}\n"
                    f"Message: {truncated}\n"
                    f"Cooldown: {channel_data.get('cooldown_time', 60)}s | Delete: {delete_msg} | Errors: {channel_data.get('error_count', 0)}"
                )

            await interaction.response.send_message(
                "\n\n".join(response),
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"Error listing greetings: {e}", exc_info=True)
            await interaction.response.send_message(
                "<:sukoon_info:1323251063910043659> | Failed to retrieve greeting channels",
                ephemeral=True
            )

    async def _check_cooldown(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        """Check if a user is in cooldown for a specific channel"""
        channel_config = await self.greeting_channels.find_one({
            "guild_id": guild_id,
            "channel_id": channel_id
        })
        if not channel_config:
            return False
        cooldown_time = channel_config.get("cooldown_time", 60)

        last_greeting = await self.greeting_history.find_one(
            {"guild_id": guild_id, "user_id": user_id, "channel_id": channel_id},
            sort=[("timestamp", -1)]
        )
        if not last_greeting:
            return True  # No previous greeting

        last_time = datetime.fromisoformat(last_greeting["timestamp"])
        return (datetime.utcnow() - last_time).total_seconds() >= cooldown_time

    async def _send_greeting(
        self,
        guild: discord.Guild,
        member: discord.Member,
        test_mode: bool = False
    ):
        """Handle greeting logic with proper error handling and cooldown checks"""
        if member.bot and not test_mode:
            return

        try:
            cursor = self.greeting_channels.find(
                {"guild_id": guild.id, "enabled": True},
                {"channel_id": 1, "greeting_message": 1, "delete_after": 1}
            )
            channels = await cursor.to_list(length=None)

            if not channels:
                return

            for channel_data in channels:
                channel_id = channel_data["channel_id"]
                greeting_message = channel_data.get("greeting_message")
                delete_after = channel_data.get("delete_after", 60)

                channel = guild.get_channel(channel_id)
                if not channel:
                    await self.greeting_channels.delete_one({"guild_id": guild.id, "channel_id": channel_id})
                    continue

                # Check cooldown from database
                if not await self._check_cooldown(guild.id, member.id, channel_id):
                    continue

                # Format message
                if greeting_message is None:
                    greeting_message = random.choice(self.default_greetings)
                message = greeting_message.format(
                    user=member.mention,
                    server=guild.name,
                    member_count=guild.member_count
                )

                # Send message with permission check
                try:
                    if not channel.permissions_for(guild.me).send_messages:
                        raise discord.Forbidden(f"No permissions in {channel.name}")

                    # Send the greeting message and schedule deletion if needed
                    sent_message = await channel.send(message)
                    if delete_after > 0:  # Only delete if delete_after is greater than 0
                        await sent_message.delete(delay=delete_after)
                    success = True
                    error_msg = None
                except discord.HTTPException as e:
                    success = False
                    error_msg = str(e)
                    logger.error(f"Error sending greeting in {channel.name}: {error_msg}")
                except Exception as e:
                    success = False
                    error_msg = str(e)
                    logger.error(f"Unexpected error sending greeting: {e}")

                # Log to history
                timestamp = datetime.utcnow().isoformat()
                await self.greeting_history.insert_one({
                    "guild_id": guild.id,
                    "user_id": member.id,
                    "channel_id": channel_id,
                    "message": message,
                    "timestamp": timestamp,
                    "success": success,
                    "error_message": error_msg
                })

                # Update error count
                await self.greeting_channels.update_one(
                    {"guild_id": guild.id, "channel_id": channel_id},
                    {"$inc": {"error_count": 0 if success else 1}}
                )

        except Exception as e:
            logger.error(f"Error in greeting handler: {e}", exc_info=True)

    @app_commands.command(name="test_greet")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Optional channel to test in", user="Optional user to test as")
    async def test_greet(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        user: Optional[discord.Member] = None
    ):
        """Test the greeting system in a specific channel"""
        target_channel = channel or interaction.channel
        test_user = user or interaction.user

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("<:sukoon_info:1323251063910043659> | Invalid channel type", ephemeral=True)
            return

        await self._send_greeting(interaction.guild, test_user, test_mode=True)
        await interaction.response.send_message(
            f"<a:sukoon_whitetick:1323992464058482729> | Test greeting sent to {target_channel.mention}",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle member join events"""
        await self._send_greeting(member.guild, member)

async def setup(bot: commands.Bot):
    await bot.add_cog(GreetingCog(bot))
