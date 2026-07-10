import discord
import logging
import re
import asyncio
from discord.ext import commands

logger = logging.getLogger(__name__)
EMBED_COLOR = discord.Color.from_rgb(47, 49, 54)


class BrandCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="brand")
    @commands.has_permissions(manage_emojis_and_stickers=True)
    async def brand(self, ctx, *, brand: str = None):
        """Rename all server emojis/stickers with a brand prefix.
        Usage: .brand <name>"""
        if not brand:
            return await ctx.send("Provide a brand name. Example: `.brand eclairs`")

        clean = re.sub(r"[^a-zA-Z0-9\s]", "_", brand).strip().replace(" ", "_").lower()
        clean = clean.strip("_")
        if not clean:
            return await ctx.send("Invalid brand name. Use letters and numbers only.")

        embed = discord.Embed(
            title=f"Brand: `{clean}`",
            description="Choose what to rename:",
            color=EMBED_COLOR
        )
        view = BrandSelector(ctx, clean)
        await ctx.send(embed=embed, view=view)


class BrandSelector(discord.ui.View):
    def __init__(self, ctx, brand):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.brand = brand

    async def interaction_check(self, interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("Not your command!", ephemeral=True)
            return False
        return True

    async def _start(self, interaction, item_type):
        self.disable_all()
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self.ctx.send(f"Starting rebranding with prefix `{self.brand}`...")
        await _rename_items(self.ctx, self.brand, item_type)

    @discord.ui.button(label="Emojis", style=discord.ButtonStyle.primary, emoji="😀")
    async def rename_emojis(self, interaction, button):
        await self._start(interaction, "emoji")

    @discord.ui.button(label="Stickers", style=discord.ButtonStyle.success, emoji="📋")
    async def rename_stickers(self, interaction, button):
        await self._start(interaction, "sticker")

    @discord.ui.button(label="Both", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def rename_both(self, interaction, button):
        self.disable_all()
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self.ctx.send(f"Starting full rebranding with prefix `{self.brand}`...")
        await _rename_items(self.ctx, self.brand, "emoji")
        await _rename_items(self.ctx, self.brand, "sticker")

    def disable_all(self):
        for child in self.children:
            child.disabled = True

    async def on_timeout(self):
        pass


async def _rename_items(ctx, brand, item_type):
    """Rename all emojis or stickers with brand prefix, showing live progress."""
    guild = ctx.guild
    items = list(guild.emojis) if item_type == "emoji" else list(guild.stickers)
    total = len(items)
    if total == 0:
        return await ctx.send(f"No {item_type}s found in this server.")

    embed = discord.Embed(
        title=f"Rebranding {item_type}s",
        description=f"Prefix: `{brand}`\nTotal: {total}\nRenamed: 0\nFailed: 0\nPending: {total}",
        color=EMBED_COLOR
    )
    status_msg = await ctx.send(embed=embed)

    renamed = 0
    failed = 0
    failed_errors = []

    def update_embed():
        embed.description = (
            f"Prefix: `{brand}`\n"
            f"Total: {total}\n"
            f"Renamed: {renamed}\n"
            f"Failed: {failed}\n"
            f"Pending: {total - renamed - failed}"
        )

    for item in items:
        original = item.name
        new_name = _sticker_name(brand, original) if item_type == "sticker" else _branded_name(brand, original)

        retries = 0
        max_retries = 3
        success = False
        while retries < max_retries and not success:
            try:
                await item.edit(name=new_name, reason=f"Branded by {ctx.author}")
                renamed += 1
                success = True
            except discord.HTTPException as e:
                if e.status == 429:
                    retries += 1
                    if retries >= max_retries:
                        failed += 1
                        failed_errors.append(f"`{original}`: rate limited")
                    else:
                        await asyncio.sleep(getattr(e, 'retry_after', 2 ** retries))
                else:
                    failed += 1
                    failed_errors.append(f"`{original}`: {e}")
                    break

        if (renamed + failed) % 5 == 0:
            update_embed()
            try:
                await status_msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        await asyncio.sleep(0.3)

    update_embed()
    embed.title = f"✅ Done rebranding {item_type}s"
    try:
        await status_msg.edit(embed=embed)
    except discord.HTTPException:
        pass

    msg = f"Finished {item_type}s: {renamed} renamed, {failed} failed."
    if failed_errors:
        errors_shown = failed_errors[:5]
        msg += "\n" + "\n".join(errors_shown)
        if len(failed_errors) > 5:
            msg += f"\n...and {len(failed_errors) - 5} more"
    await ctx.send(msg)


def _branded_name(brand, original):
    """Create a branded emoji name (2-32 chars, underscore-separated)."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "", original).strip("_") or "emoji"

    prefix = f"{brand}_"
    if clean.startswith(prefix):
        clean = clean[len(prefix):]

    branded = f"{brand}_{clean}"
    branded = branded[:32]
    if len(branded) < 2:
        branded = branded.ljust(2, "_")
    return branded


def _sticker_name(brand, original):
    """Create a branded sticker name: .gg/{brand} {name with spaces} (2-32 chars)."""
    prefix = f".gg/{brand} "
    if original.startswith(prefix):
        return original

    clean = re.sub(r"^\.gg/[\w]+ ", "", original)
    clean = re.sub(r"[_\-]", " ", clean)
    clean = re.sub(r"[^a-zA-Z0-9\s]", "", clean).strip() or "sticker"

    branded = f"{prefix}{clean}"
    if len(branded) > 30:
        max_clean = 30 - len(prefix) - 1
        clean = clean[:max_clean].rstrip()
        branded = f"{prefix}{clean}"
    if len(branded) < 2:
        branded = branded.ljust(2, " ")
    return branded


async def setup(bot):
    await bot.add_cog(BrandCog(bot))
