import discord
import logging
from typing import Optional
from discord.ext import commands, tasks
from discord import app_commands

# Configure logging
logger = logging.getLogger('activity-role-bot')

class ActivityRole(commands.Cog):
    """
    Cog for managing Spotify and Crunchyroll activity roles in Discord servers.
    Automatically assigns and removes roles when users start/stop an activity.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # role_cache structure: { guild_id: { "Spotify": role_name, "Crunchyroll": role_name } }
        self.role_cache = {}
        self.collection = self.bot.mongo_client['discord_bot']['activity_config']
        self.bot.loop.create_task(self.load_config())
        self.cache_refresh.start()

    async def load_config(self):
        """Load guild-specific configurations from MongoDB"""
        try:
            self.role_cache.clear()
            docs = await self.collection.find({}).to_list(length=None)
            for doc in docs:
                guild_id = doc["guild_id"]
                self.role_cache[guild_id] = {
                    "Spotify": doc.get("spotify_role", "Spotify"),
                    "Crunchyroll": doc.get("crunchyroll_role", "Crunchyroll")
                }
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")

    async def save_guild_config(self, guild_id: int):
        """Save guild-specific configuration to MongoDB"""
        try:
            roles = self.role_cache.get(guild_id, {"Spotify": "Spotify", "Crunchyroll": "Crunchyroll"})
            await self.collection.update_one(
                {"guild_id": guild_id},
                {"$set": {"spotify_role": roles["Spotify"], "crunchyroll_role": roles["Crunchyroll"]}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")

    async def get_role_name(self, guild_id: int, activity_type: str) -> str:
        """Get the configured role name for a given activity (Spotify or Crunchyroll)"""
        return self.role_cache.get(guild_id, {}).get(activity_type, activity_type)

    async def set_role_name(self, guild_id: int, activity_type: str, role_name: str):
        """Set the role name for a given activity"""
        if guild_id not in self.role_cache:
            self.role_cache[guild_id] = {}
        self.role_cache[guild_id][activity_type] = role_name
        await self.save_guild_config(guild_id)

    async def get_activity_role(self, guild: discord.Guild, activity_type: str) -> Optional[discord.Role]:
        """Get the role object for a given activity type"""
        role_name = await self.get_role_name(guild.id, activity_type)
        return discord.utils.get(guild.roles, name=role_name)

    @tasks.loop(hours=1)
    async def cache_refresh(self):
        """Periodically refresh the role cache"""
        for guild in self.bot.guilds:
            try:
                await self.get_activity_role(guild, "Spotify")
                await self.get_activity_role(guild, "Crunchyroll")
            except Exception as e:
                logger.error(f"Error refreshing roles in {guild.name}: {e}")

    @cache_refresh.before_loop
    async def before_cache_refresh(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """When the bot joins a guild, load configuration."""

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """When the bot is removed from a guild, clear configuration."""

        if guild.id in self.role_cache:
            del self.role_cache[guild.id]
        try:
            await self.collection.delete_one({"guild_id": guild.id})
        except Exception as e:
            logger.error(f"Error deleting configuration: {e}")

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        """Detect when a user starts or stops an activity (Spotify or Crunchyroll)"""
        if after.bot:
            return

        guild = after.guild
        if not guild.me.guild_permissions.manage_roles:
            return

        try:
            spotify_role = await self.get_activity_role(guild, "Spotify")
            crunchyroll_role = await self.get_activity_role(guild, "Crunchyroll")

            # Check for Spotify activity using discord.Spotify
            is_listening_spotify = any(isinstance(a, discord.Spotify) for a in after.activities)

            # Check for Crunchyroll activity (adjust detection logic as needed)
            is_watching_crunchyroll = any(a.name and "Crunchyroll" in a.name for a in after.activities)

            # Manage Spotify role
            if is_listening_spotify and spotify_role and spotify_role not in after.roles:
                if spotify_role.position < guild.me.top_role.position:
                    await after.add_roles(spotify_role, reason="Started listening to Spotify")
            elif not is_listening_spotify and spotify_role and spotify_role in after.roles:
                if spotify_role.position < guild.me.top_role.position:
                    await after.remove_roles(spotify_role, reason="Stopped listening to Spotify")

            # Manage Crunchyroll role
            if is_watching_crunchyroll and crunchyroll_role and crunchyroll_role not in after.roles:
                if crunchyroll_role.position < guild.me.top_role.position:
                    await after.add_roles(crunchyroll_role, reason="Started watching Crunchyroll")
            elif not is_watching_crunchyroll and crunchyroll_role and crunchyroll_role in after.roles:
                if crunchyroll_role.position < guild.me.top_role.position:
                    await after.remove_roles(crunchyroll_role, reason="Stopped watching Crunchyroll")

        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Error in on_presence_update: {e}")

    config = app_commands.Group(name="config-activities", description="Configure activity tracking settings")

    @config.command(name="role", description="Set roles for Spotify and Crunchyroll activities")
    @app_commands.describe(activity="Select an activity", role="The role to assign")
    @app_commands.choices(activity=[
        app_commands.Choice(name="Spotify", value="Spotify"),
        app_commands.Choice(name="Crunchyroll", value="Crunchyroll")
    ])
    async def set_role_cmd(self, interaction: discord.Interaction, activity: app_commands.Choice[str], role: discord.Role):
        """Set the role for a specific activity."""
        guild = interaction.guild
        if role.position >= guild.me.top_role.position:
            await interaction.response.send_message(
                f"I cannot manage the {role.mention} role because it's higher than my highest role.",
                ephemeral=True
            )
            return

        await self.set_role_name(guild.id, activity.value, role.name)
        await interaction.response.send_message(
            f"Role for **{activity.value}** has been set to {role.mention}.",
            ephemeral=True
        )

    async def cog_unload(self):
        """Cancel background tasks on unload."""
        self.cache_refresh.cancel()

async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(ActivityRole(bot))
