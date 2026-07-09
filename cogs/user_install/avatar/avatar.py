import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ext.commands import Cog
import logging
from datetime import datetime, timedelta
import asyncio

logging.basicConfig(level=logging.ERROR)

class AvatarUserInstall(Cog):
    """
    USER INSTALL COG - Slash commands only, no prefix commands.
    
    A Discord cog for displaying a user's avatar and banner via slash commands.
    Works in DMs and servers (User Install compatible).
    Includes caching and periodic cleanup to optimize API calls.
    
    IMPORTANT: This cog uses ONLY app_commands (slash commands).
    Do NOT add @commands.command() prefix commands here.
    """
    def __init__(self, bot):
        self.bot = bot
        self.cache = {}
        self.cache_expiration = timedelta(minutes=5)
        self.cache_cleanup.start()

    # ----- Avatar & Banner Slash Command -----
    @app_commands.command(name="avatar", description="Display a user's avatar and banner")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The user whose avatar you want to see (leave empty for yourself)")
    async def avatar(
        self, 
        interaction: discord.Interaction, 
        user: discord.User = None
    ):
        """
        Slash command to display a user's avatar and banner.
        Works in DMs and servers.
        """
        target_user = user or interaction.user

        try:
            # Defer response in case fetching takes time
            await interaction.response.defer()
            
            avatar_url, banner_url = await self.get_user_data(target_user)

            # Avatar embed with invisible color
            avatar_embed = discord.Embed(
                title=f"{target_user.display_name}'s Avatar",
                timestamp=datetime.utcnow()
            )
            
            # Get high quality avatar with proper format handling
            avatar = target_user.display_avatar.with_size(2048)
            avatar_embed.set_image(url=avatar.url)
            
            # Add download links
            links = []
            links.append(f"[PNG]({avatar.with_format('png').url})")
            links.append(f"[JPG]({avatar.with_format('jpg').url})")
            if avatar.is_animated():
                links.append(f"[GIF]({avatar.with_format('gif').url})")
            
            avatar_embed.description = " • ".join(links)

            # Show banner if available
            if banner_url:
                view = BannerView(target_user, banner_url)
                await interaction.followup.send(embed=avatar_embed, view=view)
            else:
                avatar_embed.set_footer(text="No banner available.")
                await interaction.followup.send(embed=avatar_embed)

        except discord.HTTPException as e:
            logging.error(f"HTTP error in avatar command: {e}")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "❌ Failed to fetch avatar data. Please try again.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "❌ Failed to fetch avatar data. Please try again.",
                        ephemeral=True
                    )
            except:
                pass
        except asyncio.TimeoutError:
            logging.error(f"Timeout while fetching data for {target_user} ({target_user.id}).")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "⏱️ Request timed out. Please try again.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "⏱️ Request timed out. Please try again.",
                        ephemeral=True
                    )
            except:
                pass
        except Exception as e:
            logging.error(f"Unexpected error in avatar command: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "❌ An unexpected error occurred. Please try again later.",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "❌ An unexpected error occurred. Please try again later.",
                        ephemeral=True
                    )
            except:
                pass

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        self.cache_cleanup.cancel()

    # ----- Caching Helpers -----
    async def get_user_data(self, user):
        """
        Fetch and cache the avatar and banner URLs for a user.
        Returns: (avatar_url, banner_url or None)
        """
        cached_data = self.cache.get(user.id, {})
        current_time = datetime.utcnow()

        # Avatar - always available from display_avatar
        avatar_url = self._get_cached_data(cached_data, "avatar", current_time)
        if not avatar_url:
            avatar_url = str(user.display_avatar.with_size(2048).url)
            self._cache_data(user.id, "avatar", avatar_url, current_time)

        # Banner - requires fetch_user, cache empty string for "no banner"
        banner_url = self._get_cached_data(cached_data, "banner", current_time)
        if banner_url is None:  # None means not checked yet
            try:
                fetched_user = await asyncio.wait_for(self.bot.fetch_user(user.id), timeout=10)
                if fetched_user.banner:
                    banner_url = str(fetched_user.banner.with_size(2048).url)
                else:
                    banner_url = ""  # Empty string means no banner
                self._cache_data(user.id, "banner", banner_url, current_time)
            except asyncio.TimeoutError:
                logging.warning(f"Timeout fetching banner for user {user.id}")
                banner_url = ""
                self._cache_data(user.id, "banner", banner_url, current_time)
            except discord.NotFound:
                logging.warning(f"User {user.id} not found when fetching banner")
                banner_url = ""
                self._cache_data(user.id, "banner", banner_url, current_time)
            except Exception as e:
                logging.error(f"Error fetching banner for {user} ({user.id}): {e}")
                banner_url = ""
                self._cache_data(user.id, "banner", banner_url, current_time)

        # Return None instead of empty string for banner_url
        return avatar_url, banner_url if banner_url else None

    def _get_cached_data(self, cached_data, key, current_time):
        """Get cached data if not expired. Returns None if not found or expired."""
        if key in cached_data:
            entry = cached_data[key]
            if current_time - entry["timestamp"] < self.cache_expiration:
                return entry["url"]
        return None

    def _cache_data(self, user_id, key, value, current_time):
        """Update the cache."""
        if user_id not in self.cache:
            self.cache[user_id] = {}
        self.cache[user_id][key] = {"url": value, "timestamp": current_time}

    @tasks.loop(minutes=1)
    async def cache_cleanup(self):
        """
        Periodically clean up expired cache entries.
        """
        try:
            current_time = datetime.utcnow()
            expired = []
            
            for user_id, data in list(self.cache.items()):
                # Check if all entries are expired
                all_expired = all(
                    current_time - entry["timestamp"] > self.cache_expiration 
                    for entry in data.values()
                )
                if all_expired:
                    expired.append(user_id)
            
            for user_id in expired:
                del self.cache[user_id]
            
            if expired:
                logging.debug(f"Cleaned up {len(expired)} expired cache entries")
        except asyncio.CancelledError:
            # Task cancelled during reload/shutdown - expected
            raise
        except Exception as e:
            logging.error(f"Error in cache cleanup: {e}")

    @cache_cleanup.before_loop
    async def before_cache_cleanup(self):
        await self.bot.wait_until_ready()


class BannerView(discord.ui.View):
    """View with button to show user banner"""
    def __init__(self, user: discord.User, banner_url: str):
        super().__init__(timeout=180)
        self.user = user
        self.banner_url = banner_url

    @discord.ui.button(label="Show Banner", style=discord.ButtonStyle.secondary)
    async def show_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Button callback to display banner"""
        try:
            # Check if interaction is still valid
            if not interaction.response.is_done():
                banner_embed = discord.Embed(
                    title=f"{self.user.display_name}'s Banner",
                    timestamp=datetime.utcnow()
                )
                banner_embed.set_image(url=self.banner_url)
                
                # Add download links for banner
                links = []
                base_url = self.banner_url.split('?')[0]  # Remove query params
                
                # Check file extension to determine format
                if '.gif' in base_url:
                    links.append(f"[PNG]({base_url.replace('.gif', '.png')}?size=2048)")
                    links.append(f"[JPG]({base_url.replace('.gif', '.jpg')}?size=2048)")
                    links.append(f"[GIF]({base_url}?size=2048)")
                elif '.webp' in base_url:
                    links.append(f"[PNG]({base_url.replace('.webp', '.png')}?size=2048)")
                    links.append(f"[JPG]({base_url.replace('.webp', '.jpg')}?size=2048)")
                    links.append(f"[WEBP]({base_url}?size=2048)")
                else:
                    # Default PNG/JPG
                    links.append(f"[PNG]({base_url}?size=2048)")
                    links.append(f"[JPG]({base_url.replace('.png', '.jpg')}?size=2048)")
                
                banner_embed.description = " • ".join(links)
                banner_embed.set_footer(text=f"Requested by {interaction.user.display_name}")
                
                await interaction.response.send_message(embed=banner_embed, ephemeral=True)
        except discord.InteractionResponded:
            # User already clicked the button
            pass
        except discord.HTTPException as e:
            logging.error(f"HTTP error showing banner: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Failed to display banner.",
                        ephemeral=True
                    )
            except:
                pass
        except Exception as e:
            logging.error(f"Unexpected error showing banner: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ An error occurred.",
                        ephemeral=True
                    )
            except:
                pass


async def setup(bot):
    await bot.add_cog(AvatarUserInstall(bot))
