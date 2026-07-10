import discord
import aiohttp
import logging
import re
from discord.ext import commands
import io
import asyncio

logger = logging.getLogger(__name__)

# Embed color constant
EMBED_COLOR = discord.Color.from_rgb(47, 49, 54)  # #2f3136

class StealEmoji(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session: aiohttp.ClientSession = None
    
    async def cog_load(self):
        """Initialize aiohttp session when cog loads."""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = aiohttp.ClientSession()

    @commands.command(name="steal")
    @commands.has_permissions(manage_emojis_and_stickers=True)  # Only users with the 'Manage Emojis and Stickers' permission can use this command
    async def steal(self, ctx):
        """Handles stealing emojis and stickers from a referenced message."""
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
        if not ctx.author.guild_permissions.manage_emojis_and_stickers or not ctx.guild.me.guild_permissions.manage_emojis_and_stickers:
            return await ctx.send("Insufficient permissions to manage stickers.")

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
        if isinstance(error, commands.MissingPermissions):
            error_msg = "You do not have the required permissions to use this command."
        elif isinstance(error, commands.MissingRole):
            error_msg = "You do not have the required role to use this command."
        elif isinstance(error, commands.CheckFailure):
            error_msg = "You are not authorized to use this command."
        else:
            error_msg = f"An unexpected error occurred: {error}"

        # Send error message
        error_message = await ctx.send(error_msg)

        # Auto delete the error message after 5 seconds
        await asyncio.sleep(5)
        await error_message.delete()

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
