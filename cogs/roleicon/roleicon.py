import discord
from discord.ext import commands
from discord.ext.commands import Cog
from discord import app_commands
import logging
import re

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


class RoleIconCog(Cog):
    """
    A Discord cog for managing role icons with emojis.
    Allows users to set role icons using emojis or images.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="roleicon", aliases=["ri"])
    @commands.has_permissions(manage_roles=True)
    async def roleicon(self, ctx, role_id: str, *, emoji_or_image: str = None):
        """
        Set a role icon using an emoji or image.
        
        Usage:
        - `.roleicon 123456789 <:custom_emoji:987654321>` - Set with custom emoji
        - `.roleicon @RoleName <:custom_emoji:987654321>` - Set with role mention
        - `.roleicon 123456789` - Remove role icon (no emoji provided)
        """
        role_obj = self._parse_role_id(ctx, role_id)
        if not role_obj:
            embed = discord.Embed(
                title="❌ Role Not Found",
                description=f"Could not find role with ID: `{role_id}`",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        if not await self._check_permissions(ctx, role_obj):
            return

        if not emoji_or_image:
            await self._remove_role_icon(ctx, role_obj)
            return

        emoji_info = self._parse_emoji(emoji_or_image)
        if not emoji_info:
            embed = discord.Embed(
                title="❌ Invalid Emoji",
                description=f"Could not parse emoji: `{emoji_or_image}`\n\nUse a custom emoji like `<:name:id>`",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        await self._set_role_icon(ctx, role_obj, emoji_info)

    @app_commands.command(name="roleicon", description="Set a role icon with an emoji")
    @app_commands.describe(
        role="The role ID or mention to modify",
        emoji="Custom emoji to set as the role icon (leave empty to remove)"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def roleicon_slash(
        self,
        interaction: discord.Interaction,
        role: str,
        emoji: str = None
    ):
        """Slash command version of roleicon."""
        await interaction.response.defer()

        role_obj = self._parse_role_id_slash(interaction, role)
        if not role_obj:
            embed = discord.Embed(
                title="❌ Role Not Found",
                description=f"Could not find role with ID: `{role}`",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed)

        if not await self._check_permissions_slash(interaction, role_obj):
            return

        if not emoji:
            await self._remove_role_icon_slash(interaction, role_obj)
            return

        emoji_info = self._parse_emoji(emoji)
        if not emoji_info:
            embed = discord.Embed(
                title="❌ Invalid Emoji",
                description=f"Could not parse emoji: `{emoji}`\n\nUse a custom emoji like `<:name:id>`",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed)

        await self._set_role_icon_slash(interaction, role_obj, emoji_info)

    # ===== Helper Methods =====

    def _parse_role_id(self, ctx, role_input: str) -> discord.Role:
        """Parse role from ID or mention."""
        if not ctx or not ctx.guild:
            return None
        try:
            # Handle role mention format: <@&123456789>
            if role_input.startswith("<@&") and role_input.endswith(">"):
                role_id = int(role_input[3:-1])
            else:
                # Handle plain role ID
                role_id = int(role_input)
            return ctx.guild.get_role(role_id)
        except (ValueError, AttributeError, TypeError):
            return None

    def _parse_role_id_slash(self, interaction: discord.Interaction, role_input: str) -> discord.Role:
        """Parse role from ID or mention for slash commands."""
        if not interaction or not interaction.guild:
            return None
        try:
            # Handle role mention format: <@&123456789>
            if role_input.startswith("<@&") and role_input.endswith(">"):
                role_id = int(role_input[3:-1])
            else:
                # Handle plain role ID
                role_id = int(role_input)
            return interaction.guild.get_role(role_id)
        except (ValueError, AttributeError, TypeError):
            return None

    def _parse_emoji(self, emoji_input: str) -> dict | None:
        """
        Parse emoji from string and return emoji info.
        
        Supports any custom emoji (from any server) by constructing CDN URL directly.
        
        Returns:
            dict with keys: 'id', 'name', 'animated', 'url', 'display'
            None if parsing fails
        """
        if not emoji_input:
            return None
        
        emoji_input = emoji_input.strip()
        if not emoji_input:
            return None

        # Match custom emoji format: <:name:id> or <a:name:id>
        match = re.match(r"<(a?):(\w+):(\d+)>", emoji_input)
        if not match:
            return None
        
        animated = match.group(1) == 'a'
        name = match.group(2)
        emoji_id = match.group(3)
        
        # Construct CDN URL - use gif for animated, png for static
        extension = 'gif' if animated else 'png'
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
        
        return {
            'id': emoji_id,
            'name': name,
            'animated': animated,
            'url': url,
            'display': emoji_input
        }

    async def _check_permissions(self, ctx, role: discord.Role) -> bool:
        """Check if bot and user have permissions to edit the role."""
        if not ctx or not ctx.guild or not ctx.author:
            return False
            
        bot_member = ctx.guild.me
        if not bot_member:
            return False
        
        # Check bot permissions
        if not bot_member.guild_permissions.manage_roles:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have the **Manage Roles** permission.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False

        if bot_member.top_role.position <= role.position:
            embed = discord.Embed(
                title="❌ Role Hierarchy Error",
                description=f"My highest role must be above {role.mention} in the hierarchy.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False

        # Prevent editing @everyone role
        if role.id == ctx.guild.id:
            embed = discord.Embed(
                title="❌ Cannot Edit @everyone",
                description="You cannot modify the @everyone role.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False

        return True

    async def _check_permissions_slash(self, interaction: discord.Interaction, role: discord.Role) -> bool:
        """Check if bot and user have permissions to edit the role (slash command version)."""
        if not interaction or not interaction.guild or not interaction.user:
            return False
            
        bot_member = interaction.guild.me
        if not bot_member:
            return False
        
        # Check bot permissions
        if not bot_member.guild_permissions.manage_roles:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have the **Manage Roles** permission.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return False

        if bot_member.top_role.position <= role.position:
            embed = discord.Embed(
                title="❌ Role Hierarchy Error",
                description=f"My highest role must be above {role.mention} in the hierarchy.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return False

        # Prevent editing @everyone role
        if role.id == interaction.guild.id:
            embed = discord.Embed(
                title="❌ Cannot Edit @everyone",
                description="You cannot modify the @everyone role.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return False

        return True

    async def _set_role_icon(self, ctx, role: discord.Role, emoji_info: dict):
        """Set a role icon from emoji info dict."""
        try:
            icon_url = emoji_info['url']
            try:
                async with self.bot.session.get(icon_url) as resp:
                    if resp.status != 200:
                        embed = discord.Embed(
                            title="❌ Error Downloading Emoji",
                            description="Failed to download emoji image.",
                            color=discord.Color.red()
                        )
                        return await ctx.send(embed=embed)
                    icon_data = await resp.read()
            except Exception as e:
                logger.error(f"Error downloading emoji: {e}")
                embed = discord.Embed(
                    title="❌ Error Downloading Emoji",
                    description="Failed to download emoji image.",
                    color=discord.Color.red()
                )
                return await ctx.send(embed=embed)

            await role.edit(display_icon=icon_data, reason=f"Icon set by {ctx.author}")

            # Simple text response
            await ctx.send(f"✅ Set role icon for {role.mention} to {emoji_info['display']}")

        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have permission to edit this role.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Error Setting Icon",
                description=f"Failed to set role icon: {str(e)[:100]}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            logger.error(f"Error setting role icon: {e}")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            logger.error(f"Unexpected error in _set_role_icon: {e}")

    async def _set_role_icon_slash(self, interaction: discord.Interaction, role: discord.Role, emoji_info: dict):
        """Set a role icon from emoji info dict (slash command version)."""
        try:
            icon_url = emoji_info['url']
            try:
                async with self.bot.session.get(icon_url) as resp:
                    if resp.status != 200:
                        embed = discord.Embed(
                            title="❌ Error Downloading Emoji",
                            description="Failed to download emoji image.",
                            color=discord.Color.red()
                        )
                        return await interaction.followup.send(embed=embed)
                    icon_data = await resp.read()
            except Exception as e:
                logger.error(f"Error downloading emoji: {e}")
                embed = discord.Embed(
                    title="❌ Error Downloading Emoji",
                    description="Failed to download emoji image.",
                    color=discord.Color.red()
                )
                return await interaction.followup.send(embed=embed)

            await role.edit(display_icon=icon_data, reason=f"Icon set by {interaction.user}")

            # Simple text response
            await interaction.followup.send(f"✅ Set role icon for {role.mention} to {emoji_info['display']}")

        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have permission to edit this role.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Error Setting Icon",
                description=f"Failed to set role icon: {str(e)[:100]}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            logger.error(f"Error setting role icon: {e}")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            logger.error(f"Unexpected error in _set_role_icon_slash: {e}")

    async def _remove_role_icon(self, ctx, role: discord.Role):
        """Remove a role's icon."""
        if not role.icon:
            embed = discord.Embed(
                title="❌ No Icon to Remove",
                description=f"{role.mention} does not have an icon set.",
                color=discord.Color.orange()
            )
            return await ctx.send(embed=embed)

        try:
            await role.edit(display_icon=None, reason=f"Icon removed by {ctx.author}")

            # Simple text response
            await ctx.send(f"✅ Removed role icon from {role.mention}")

        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have permission to edit this role.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Error Removing Icon",
                description=f"Failed to remove role icon: {str(e)[:100]}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            logger.error(f"Error removing role icon: {e}")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            logger.error(f"Unexpected error in _remove_role_icon: {e}")

    async def _remove_role_icon_slash(self, interaction: discord.Interaction, role: discord.Role):
        """Remove a role's icon (slash command version)."""
        if not role.icon:
            embed = discord.Embed(
                title="❌ No Icon to Remove",
                description=f"{role.mention} does not have an icon set.",
                color=discord.Color.orange()
            )
            return await interaction.followup.send(embed=embed)

        try:
            await role.edit(display_icon=None, reason=f"Icon removed by {interaction.user}")

            # Simple text response
            await interaction.followup.send(f"✅ Removed role icon from {role.mention}")

        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="I don't have permission to edit this role.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Error Removing Icon",
                description=f"Failed to remove role icon: {str(e)[:100]}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            logger.error(f"Error removing role icon: {e}")
        except Exception as e:
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            logger.error(f"Unexpected error in _remove_role_icon_slash: {e}")


async def setup(bot):
    await bot.add_cog(RoleIconCog(bot))
