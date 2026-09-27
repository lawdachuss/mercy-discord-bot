import discord
import aiohttp
import logging
import re
import time
from typing import Any, Dict, Optional

from discord import app_commands
from discord.ext import commands
import io
import asyncio

logger = logging.getLogger(__name__)

# Embed color constant
EMBED_COLOR = discord.Color.from_rgb(47, 49, 54)  # #2f3136

CONFIG_CACHE_TTL = 30.0
ROLE_MENTION = re.compile(r"^<@&(\d+)>$")


def _default_config(guild_id: int) -> Dict[str, Any]:
    return {
        "guild_id": guild_id,
        "steal_role_id": None,
        "updated_at": None,
    }


class StealSetupError(Exception):
    """A user-facing error. The message is shown to the invoker as-is."""


class StealEmoji(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: aiohttp.ClientSession = None
        self._config_cache: Dict[int, tuple] = {}

    async def cog_load(self):
        """Initialize aiohttp session when cog loads."""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = aiohttp.ClientSession()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    @property
    def collection(self):
        client = getattr(self.bot, "mongo_client", None)
        if client is None:
            return None
        return client["discord_bot"]["steal_settings"]

    async def get_config(self, guild_id: int) -> Dict[str, Any]:
        """Guild configuration with a short TTL cache (defaults if Mongo is down)."""
        now = time.time()
        cached = self._config_cache.get(guild_id)
        if cached and now - cached[0] < CONFIG_CACHE_TTL:
            # Hand out a copy so callers cannot mutate the cached document.
            return dict(cached[1])

        config = _default_config(guild_id)
        collection = self.collection
        if collection is not None:
            try:
                doc = await collection.find_one({"guild_id": guild_id})
                if doc:
                    doc.pop("_id", None)
                    config.update(doc)
                    config["guild_id"] = guild_id
            except Exception as e:
                logger.warning(f"[Steal] Could not read config for {guild_id}: {e}")

        self._config_cache[guild_id] = (now, config)
        return config

    async def save_config(self, config: Dict[str, Any]) -> bool:
        collection = self.collection
        if collection is None:
            return False
        config["updated_at"] = discord.utils.utcnow().isoformat()
        try:
            await collection.replace_one(
                {"guild_id": config["guild_id"]}, config, upsert=True
            )
        except Exception as e:
            logger.error(f"[Steal] Could not save config for {config['guild_id']}: {e}")
            return False
        self._config_cache[config["guild_id"]] = (time.time(), dict(config))
        return True

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    @staticmethod
    def _holds_steal_role(member: discord.Member, config: Dict[str, Any]) -> bool:
        role_id = config.get("steal_role_id")
        if not role_id:
            return False
        return any(role.id == role_id for role in member.roles)

    def _can_steal(self, member: discord.Member, config: Dict[str, Any]) -> bool:
        """Role *grants* access - it never takes the permission away.

        Administrators, members holding the configured role, and anyone with
        Manage Emojis and Stickers may all steal.
        """
        if member.guild_permissions.manage_emojis_and_stickers:
            return True
        if member.guild_permissions.administrator:
            return True
        return self._holds_steal_role(member, config)

    def access_denial(self, member: discord.Member, config: Dict[str, Any]) -> Optional[str]:
        """None when the member may steal, otherwise the message to show them."""
        if self._can_steal(member, config):
            return None

        role_id = config.get("steal_role_id")
        role = member.guild.get_role(role_id) if role_id else None
        if role is not None:
            return (
                f"You need the {role.mention} role or **Manage Emojis and Stickers** "
                f"to steal emojis and stickers."
            )
        return (
            "You need **Manage Emojis and Stickers** to steal emojis and stickers.\n"
            "An admin can allow a specific role instead: `.stealsetup role @Role` "
            "or `/stealsetup`."
        )

    @staticmethod
    def _plain_text(embed: discord.Embed) -> str:
        """Render an embed as text so it survives without Embed Links."""
        lines = [embed.title or "", embed.description or ""]
        lines += [f"**{field.name}:** {field.value}" for field in embed.fields]
        return "\n".join(line for line in lines if line)

    async def _send(self, destination: Any, *, embed: Optional[discord.Embed] = None,
                    content: Optional[str] = None, ephemeral: bool = False) -> None:
        """Send an embed, falling back to plain text if Embed Links is missing."""
        interaction = destination if isinstance(destination, discord.Interaction) else None

        async def deliver(the_embed, the_content):
            if interaction is None:
                await destination.send(content=the_content, embed=the_embed)
            elif interaction.response.is_done():
                # The first attempt failed after the response was consumed;
                # send through the follow-up endpoint instead.
                await interaction.followup.send(
                    content=the_content, embed=the_embed, ephemeral=ephemeral
                )
            else:
                await interaction.response.send_message(
                    content=the_content, embed=the_embed, ephemeral=ephemeral
                )

        if embed is not None:
            try:
                await deliver(embed, None)
                return
            except discord.Forbidden:
                content = self._plain_text(embed)
                embed = None
            except Exception as e:
                logger.warning(f"[Steal] Could not send embed: {e}")
                return

        if not content:
            return
        try:
            await deliver(None, content)
        except Exception as e:
            logger.warning(f"[Steal] Could not send message: {e}")

    async def _send_transient(self, destination: Any, content: str) -> None:
        """Send a message and clear it after 5s, matching the cog's error style."""
        try:
            message = await destination.send(content)
        except Exception as e:
            logger.warning(f"[Steal] Could not send message: {e}")
            return
        await asyncio.sleep(5)
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    @commands.command(name="steal")
    @commands.guild_only()
    async def steal(self, ctx):
        """Handles stealing emojis and stickers from a referenced message."""
        config = await self.get_config(ctx.guild.id)
        denied = self.access_denial(ctx.author, config)
        if denied:
            return await self._send_transient(ctx, denied)

        if not ctx.message.reference:
            return await ctx.send("You must reply to a message containing an emoji or sticker.")

        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.NotFound:
            return await ctx.send("The referenced message was deleted.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to fetch message: {e}")

        branding = await self.ask_branding(ctx)
        if branding is None:
            return

        if replied_message.stickers:
            await self.steal_sticker(ctx, replied_message, branding)
        elif emojis := self.extract_emojis(replied_message):
            await self.steal_emoji(ctx, emojis, branding)
        else:
            await ctx.send("No emoji or sticker found in the referenced message.")

    async def ask_branding(self, ctx):
        """Ask the user for branding prefix using a modal."""
        embed = discord.Embed(
            title="Brand Prefix",
            description="Click **Set Brand** to add a prefix to emoji/sticker names, or **Skip** to use original names.",
            color=EMBED_COLOR
        )
        view = BrandView(ctx.author)
        prompt = await ctx.send(embed=embed, view=view)
        view._message = prompt
        await view.wait()

        for child in view.children:
            child.disabled = True

        if view.result == "timeout":
            embed.title = "Timed Out"
            embed.description = "You took too long. Use the command again if needed."
            try:
                await prompt.edit(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return None

        try:
            await prompt.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        if view.result == "skip" or not view.brand:
            return ""

        clean = re.sub(r"[^a-zA-Z0-9\s]", "_", view.brand).strip().replace(" ", "_").lower()
        clean = clean.strip("_")
        return clean or ""

    async def steal_sticker(self, ctx, message, branding):
        """Handles stealing stickers with processing and success message."""
        sticker = message.stickers[0]
        # The invoker was already vetted in .steal; re-check anyway so this
        # helper stays safe if it is ever called from somewhere else.
        denied = self.access_denial(ctx.author, await self.get_config(ctx.guild.id))
        if denied:
            return await ctx.send(denied)
        if not ctx.guild.me.guild_permissions.manage_emojis_and_stickers:
            return await ctx.send("I lack the necessary permissions to manage stickers.")

        # Ensure session is available
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

        sticker_url = sticker.url
        embed = discord.Embed(
            description="<a:sukoon_loading:1322897472338526240> **Processing** to Steal Sticker...",
            color=EMBED_COLOR
        )
        processing_message = await ctx.send(embed=embed)

        headers = {
            "User-Agent": "MercyBot/1.0",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
        }

        async with self.session.get(sticker_url, headers=headers) as resp:
            if resp.status != 200:
                await processing_message.edit(content=f"Failed to fetch sticker: HTTP {resp.status}")
                await asyncio.sleep(5)
                await processing_message.delete()
                return

            sticker_data = await resp.read()
            sticker_name = sticker.name.replace(" ", "_")
            if branding:
                sticker_name = f"{branding}_{sticker_name}"
            file_extension = "png" if sticker.format in [discord.StickerFormatType.png, discord.StickerFormatType.apng] else "json"

            sticker_file = io.BytesIO(sticker_data)
            try:
                retries = 0
                max_retries = 3
                while retries < max_retries:
                    try:
                        new_sticker = await ctx.guild.create_sticker(
                            name=sticker_name, description=f"{branding or 'Original'} sticker", emoji=":smile:",
                            file=discord.File(sticker_file, filename=f"{sticker_name}.{file_extension}")
                        )
                        break
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retries += 1
                            if retries >= max_retries:
                                raise
                            retry_after = getattr(e, 'retry_after', 2 ** retries)
                            await asyncio.sleep(retry_after)
                        else:
                            raise

                # Send the sticker directly as a message
                await processing_message.delete()  # Remove the processing embed
                success_message = await ctx.send(f"Sticker Added!")
                await ctx.send(stickers=[new_sticker])  # Send the sticker directly to the channel

            except discord.HTTPException as e:
                # Handle specific error code for max stickers reached
                if "Maximum number of stickers reached" in str(e):
                    await processing_message.edit(content="Maximum number of stickers reached. Unable to add sticker.")
                    await asyncio.sleep(5)  # Auto-delete after 5 seconds
                    await processing_message.delete()  # Delete the bot's message

                else:
                    await self.handle_bot_error(ctx, f"Failed to add sticker: {e}")

            finally:
                sticker_file.close()

    async def steal_emoji(self, ctx, emojis, branding):
        """Handles stealing emojis with processing and success message."""
        embed = discord.Embed(
            description="<a:sukoon_loading:1322897472338526240> **Processing** to Steal Emojis...",
            color=EMBED_COLOR
        )
        processing_message = await ctx.send(embed=embed)

        total = len(emojis)
        added = 0
        for emoji in emojis:
            emoji_parts = emoji.strip("<>").split(":")
            emoji_id = emoji_parts[-1]
            emoji_name = emoji_parts[1] if len(emoji_parts) > 2 else emoji_parts[0]
            if branding:
                emoji_name = f"{branding}_{emoji_name}"
            emoji_ext = "gif" if emoji.startswith("<a:") else "png"
            emoji_url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{emoji_ext}"
            result = await self.add_emoji(ctx, emoji_url, emoji_name)
            if result:
                added += 1

        if added == 0:
            await processing_message.edit(content="Failed to steal any emojis.")
        else:
            success_embed = discord.Embed(
                description=f"<a:stolen_success:1322894423755063316> Successfully created **{added}/{total}** Emojis",
                color=EMBED_COLOR
            )
            await processing_message.edit(embed=success_embed)

    def extract_emojis(self, message):
        """Extract custom emojis from a message."""
        return [word for word in message.content.split() if word.startswith("<:") or word.startswith("<a:")]

    async def add_emoji(self, ctx, emoji_url, name, image_data=None):
        """Add emoji to the server. If image_data is provided, skip download."""
        guild = ctx.guild
        if not guild.me.guild_permissions.manage_emojis_and_stickers:
            await ctx.send("I lack the necessary permissions to manage emojis.")
            return None

        if image_data is None:
            if not self.session or self.session.closed:
                self.session = aiohttp.ClientSession()
            headers = {
                "User-Agent": "MercyBot/1.0",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8"
            }
            async with self.session.get(emoji_url, headers=headers) as resp:
                if resp.status != 200:
                    await ctx.send(f"Failed to fetch emoji: HTTP {resp.status}")
                    return None
                image_data = await resp.read()

        name = await self.get_unique_emoji_name(name, guild)
        return await self._create_emoji(ctx, name, image_data)

    async def _create_emoji(self, ctx, name, image_data):
        """Create emoji on Discord with rate limit retry."""
        guild = ctx.guild
        retries = 0
        max_retries = 3
        while retries < max_retries:
            try:
                return await guild.create_custom_emoji(name=name, image=image_data)
            except discord.HTTPException as e:
                if e.status == 429:
                    retries += 1
                    if retries >= max_retries:
                        await ctx.send(f"Rate limited. Failed after {max_retries} retries.")
                        return None
                    retry_after = getattr(e, 'retry_after', 2 ** retries)
                    await asyncio.sleep(retry_after)
                else:
                    await ctx.send(f"Error creating emoji: {e}")
                    return None

    async def get_unique_emoji_name(self, name, guild):
        """Generate a unique name for the emoji (must be 2-32 chars)."""
        clean = re.sub(r"[^a-zA-Z0-9_]", "", name).strip("_") or "emoji"
        while len(clean) < 2:
            clean += "_"
        clean = clean[:32]

        existing_names = {emoji.name for emoji in guild.emojis}
        unique_name = clean
        counter = 1
        while unique_name in existing_names:
            suffix = f"_{counter}"
            max_base = 32 - len(suffix)
            unique_name = f"{clean[:max_base]}{suffix}"
            counter += 1
        return unique_name

    async def cog_unload(self):
        """Close aiohttp session when the cog is unloaded."""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def handle_bot_error(self, ctx, error_message):
        """Handle bot-specific errors like 'Maximum number of stickers reached'."""
        # Send the error message
        error_message_sent = await ctx.send(error_message)

        # Auto delete the error message after 5 seconds
        await asyncio.sleep(5)
        await error_message_sent.delete()

    @steal.error
    async def steal_error(self, ctx, error):
        """Handle errors for the steal command."""
        if isinstance(error, commands.NoPrivateMessage):
            error_msg = "This command only works in a server."
        elif isinstance(error, commands.CheckFailure):
            error_msg = "You are not authorized to use this command."
        else:
            logger.error(f"[Steal] .steal failed: {type(error).__name__}: {error}", exc_info=error)
            error_msg = f"An unexpected error occurred: {error}"

        await self._send_transient(ctx, error_msg)

    # ------------------------------------------------------------------
    # .stealsetup  (prefix)
    # ------------------------------------------------------------------
    @commands.command(
        name="stealsetup",
        aliases=["stealconfig", "stealsettings"],
        help="Configure who may steal: .stealsetup [role <@role>|clear|show]",
    )
    @commands.guild_only()
    async def stealsetup_prefix(self, ctx: commands.Context, action: str = "show", *,
                                target: str = ""):
        if not ctx.author.guild_permissions.administrator:
            await self._send_transient(ctx, "❌ You need **Administrator** to set this up.")
            return

        action = action.lower()
        target = target.strip()
        config = await self.get_config(ctx.guild.id)
        changed = False

        try:
            if action in {"role", "setrole"}:
                role = self._resolve_role(ctx.guild, target)
                if role is None:
                    raise StealSetupError(
                        "Give me a role: `.stealsetup role @Role` (mention, ID or exact name)."
                    )
                if role.is_default():
                    raise StealSetupError(
                        "That is `@everyone` - pick a real role so only its members can steal."
                    )
                config["steal_role_id"] = role.id
                changed = True

            elif action in {"clear", "unset", "remove", "off"}:
                if not config.get("steal_role_id"):
                    raise StealSetupError("No role is configured in this server yet.")
                config["steal_role_id"] = None
                changed = True

            elif action in {"show", "view", "config", "help"}:
                pass
            else:
                raise StealSetupError(
                    "Unknown option. Usage: `.stealsetup [role <@role>] [clear] [show]`"
                )

            if changed and not await self.save_config(config):
                raise StealSetupError(
                    "I could not save that to the database (MongoDB is unavailable)."
                )
        except StealSetupError as e:
            await self._send_transient(ctx, f"❌ {e}")
            return

        await self._send(ctx, embed=self.config_embed(ctx.guild, config))

    @staticmethod
    def _resolve_role(guild: discord.Guild, raw: str) -> Optional[discord.Role]:
        raw = raw.strip()
        if not raw:
            return None
        match = ROLE_MENTION.match(raw)
        if match:
            return guild.get_role(int(match.group(1)))
        if raw.isdigit() and len(raw) >= 17:
            return guild.get_role(int(raw))
        lowered = raw.lower()
        return next((r for r in guild.roles if r.name.lower() == lowered), None)

    def config_embed(self, guild: discord.Guild, config: Dict[str, Any]) -> discord.Embed:
        role = guild.get_role(config["steal_role_id"]) if config.get("steal_role_id") else None

        bot_member = guild.me
        can_steal = bool(
            bot_member and bot_member.guild_permissions.manage_emojis_and_stickers
        )

        embed = discord.Embed(
            title="⚙️ Steal setup",
            color=discord.Color.green() if can_steal else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🎭 Role allowed to steal",
            value=(role.mention if role else "❌ Not configured") + (
                "" if role else "\nUse `.stealsetup role <@role>` or `/stealsetup role:`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🪪 Who can use `.steal`",
            value=(
                "Server **Administrators** always can; so does anyone with "
                "**Manage Emojis and Stickers**"
                + (f"; so does {role.mention}" if role else "; no role configured yet")
                + "."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔧 My permissions",
            value=(
                "✅ Manage Emojis and Stickers"
                if can_steal
                else "❌ Missing **Manage Emojis and Stickers** - I cannot add anything."
            ),
            inline=False,
        )
        embed.set_footer(
            text=".stealsetup role <@role> · .stealsetup clear · .stealsetup show"
        )
        return embed

    # ------------------------------------------------------------------
    # /stealsetup  (slash)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="stealsetup",
        description="Configure which role may steal emojis and stickers",
    )
    @app_commands.describe(
        role="Role allowed to use .steal without Manage Emojis and Stickers",
        action="Show or clear the current configuration",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Show configuration", value="show"),
        app_commands.Choice(name="Clear the configured role", value="clear_role"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def stealsetup_slash(self, interaction: discord.Interaction,
                               role: Optional[discord.Role] = None,
                               action: str = "show"):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "❌ You need **Administrator** to set this up.", ephemeral=True
            )
            return

        config = await self.get_config(interaction.guild.id)
        changed = False

        if action == "clear_role":
            config["steal_role_id"] = None
            changed = True

        if role is not None:
            if role.is_default():
                await interaction.response.send_message(
                    "❌ That is `@everyone` - pick a real role so only its members can steal.",
                    ephemeral=True,
                )
                return
            config["steal_role_id"] = role.id
            changed = True

        if changed and not await self.save_config(config):
            await interaction.response.send_message(
                "❌ I could not save that to the database (MongoDB is unavailable).",
                ephemeral=True,
            )
            return

        await self._send(interaction, embed=self.config_embed(interaction.guild, config))


class BrandModal(discord.ui.Modal, title="Set Brand Prefix"):
    answer = discord.ui.TextInput(
        label="Brand name",
        placeholder="e.g. eclairs",
        required=False,
        max_length=50
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction):
        text = self.answer.value.strip()
        if text:
            self.view.brand = text
            self.view.result = "brand"
            for child in self.view.children:
                child.disabled = True
            if self.view._message:
                try:
                    await self.view._message.edit(content=f"Brand set to: `{text}`", view=self.view)
                except discord.HTTPException:
                    pass
            self.view.stop()
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass


class BrandView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=120)
        self.author = author
        self.result = None  # "brand" | "skip" | "timeout"
        self.brand = None
        self._message = None

    async def interaction_check(self, interaction):
        if interaction.user != self.author:
            try:
                await interaction.response.send_message("This isn't your prompt!", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    @discord.ui.button(label="Set Brand", style=discord.ButtonStyle.primary)
    async def set_brand(self, interaction, button):
        modal = BrandModal(self)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            return

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        if self.result is not None:
            return
        self.result = "skip"
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        if self.result is not None:
            return
        self.result = "timeout"
        for child in self.children:
            child.disabled = True
        if self._message:
            try:
                await self._message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()


async def setup(bot):
    await bot.add_cog(StealEmoji(bot))


async def teardown(bot):
    cog = bot.get_cog("StealEmoji")
    if cog:
        await cog.cog_unload()
