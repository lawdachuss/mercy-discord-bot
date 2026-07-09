import discord
from discord.ext import commands
from discord.ui import Button, View
from datetime import datetime, timedelta
from typing import List
import backoff
import logging
import asyncio
import random

logger = logging.getLogger('discord')

class SnipeView(View):
    def __init__(self, cog, ctx: commands.Context, messages: List[dict], timeout: float = 7 * 24 * 60 * 60):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.messages = messages
        self.current_page = 0
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        try:
            self.clear_items()
            prev_button = Button(
                emoji="<:sukoon_left_arrow:1344204740405231727>",
                style=discord.ButtonStyle.secondary,
                disabled=self.current_page == 0
            )
            prev_button.callback = self.previous_page
            self.add_item(prev_button)

            counter_button = Button(
                label=f"Page {self.current_page + 1}/{len(self.messages)}",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )
            self.add_item(counter_button)

            next_button = Button(
                emoji="<:sukoon_right_arrow:1344204531520638987>",
                style=discord.ButtonStyle.secondary,
                disabled=self.current_page >= len(self.messages) - 1
            )
            next_button.callback = self.next_page
            self.add_item(next_button)
        except Exception as e:
            logger.error(f"Error updating buttons: {type(e).__name__}: {e}")
            raise

    async def previous_page(self, interaction: discord.Interaction):
        try:
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("You cannot use these controls!", ephemeral=True)
                return

            if self.current_page > 0:
                self.current_page -= 1
                self.update_buttons()
                embed = await self.cog.create_snipe_embed(self.ctx, self.messages[self.current_page])
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                try:
                    await interaction.followup.send("Rate limited, please wait a moment before trying again.", ephemeral=True)
                except:
                    pass
            else:
                logger.error(f"HTTP error handling previous page: {e}")
        except Exception as e:
            logger.error(f"Error handling previous page: {type(e).__name__}: {e}")

    async def next_page(self, interaction: discord.Interaction):
        try:
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("You cannot use these controls!", ephemeral=True)
                return

            if self.current_page < len(self.messages) - 1:
                self.current_page += 1
                self.update_buttons()
                embed = await self.cog.create_snipe_embed(self.ctx, self.messages[self.current_page])
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                try:
                    await interaction.followup.send("Rate limited, please wait a moment before trying again.", ephemeral=True)
                except:
                    pass
            else:
                logger.error(f"HTTP error handling next page: {e}")
        except Exception as e:
            logger.error(f"Error handling next page: {type(e).__name__}: {e}")

    async def on_timeout(self):
        try:
            if self.message:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
        except discord.HTTPException as e:
            if e.status != 429:  # Don't log rate limits on timeout
                logger.error(f"Error handling view timeout: {e}")
        except Exception as e:
            logger.error(f"Error handling view timeout: {type(e).__name__}: {e}")

class Snipe(commands.Cog):
    _loaded = False

    def __init__(self, bot: commands.Bot):
        if Snipe._loaded:
            logger.error("Snipe cog is already loaded! Duplicate loading prevented.")
            return
        Snipe._loaded = True

        self.bot = bot
        self.max_age = timedelta(days=7)
        self.connected = False
        self.cleanup_task = None
        self.db_ready = False

        # Cache for member lookups to reduce API calls
        self.member_cache = {}
        self.cache_expiry = {}

        self.embed_colors = [
            0xFF6B6B, 0x4ECDC4, 0x45B7D1, 0x96CEB4, 0xFF9F1C, 0x2D3047,
            0xD4A373, 0x588B8B, 0xFF7F51, 0x9B5DE5, 0x00BBF9, 0xFEE440,
            0xF15BB5, 0x9B2226, 0x006D77, 0xFCAF58, 0x4EA8DE, 0x8AC926,
            0xAA8B56, 0x9381FF, 0xFF70A6, 0x43AA8B, 0x277DA1, 0xF94144,
            0x90BE6D, 0xF8961E, 0xF9C74F, 0x577590, 0xB5838D, 0x495057
        ]

    async def cog_load(self):
        """Called when the cog is loaded"""
        await self._init_db()

    async def _init_db(self):
        try:
            self.collection = self.bot.mongo_client['discord_bot']['deleted_messages']
            self.db_ready = True

            if not self.cleanup_task:
                self.cleanup_task = self.bot.loop.create_task(self._periodic_cleanup())

        except Exception as e:
            logger.error(f"Critical error initializing database: {e}")
            self.db_ready = False
            raise

    async def _periodic_cleanup(self):
        while True:
            try:
                if self.db_ready is True:
                    cutoff_date = datetime.utcnow() - self.max_age
                    cutoff_iso = cutoff_date.isoformat()
                    result = await self.collection.delete_many({"deleted_at": {"$lt": cutoff_iso}})
                    deleted_count = result.deleted_count
                    if deleted_count > 0:
                        logger.info(f"Deleted {deleted_count} expired messages from database.")

                # Clean up member cache periodically
                current_time = datetime.utcnow()
                expired_keys = [k for k, exp_time in self.cache_expiry.items() if current_time > exp_time]
                for key in expired_keys:
                    self.member_cache.pop(key, None)
                    self.cache_expiry.pop(key, None)

            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")
            await asyncio.sleep(3600)  # Run every hour

    @backoff.on_exception(
        backoff.expo,
        (discord.ConnectionClosed, discord.GatewayNotFound, discord.HTTPException),
        max_tries=8,
        max_time=300
    )
    async def connect_with_backoff(self, token: str) -> None:
        if not self.connected:
            try:
                await self.bot.start(token)
                self.connected = True
            except Exception as e:
                logger.error(f"Failed to connect after all retries: {str(e)}")
                raise

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if not self.db_ready:
            return

        safe_attachments = [
            att.url for att in message.attachments
            if att.url.startswith('https://cdn.discordapp.com/')
        ]

        try:
            await self.collection.insert_one({
                'channel_id': message.channel.id,
                'content': message.content,
                'author': message.author.name,
                'author_id': message.author.id,
                'deleted_at': datetime.utcnow().isoformat(),
                'attachments': ','.join(safe_attachments) if safe_attachments else None
            })
        except Exception as e:
            logger.error(f"Error storing deleted message: {e}")

    async def get_member_cached(self, guild: discord.Guild, author_id: int, author_name: str):
        """Get member with caching to reduce API calls"""
        cache_key = f"{guild.id}_{author_id}"
        current_time = datetime.utcnow()

        # Check cache first
        if cache_key in self.member_cache:
            if current_time < self.cache_expiry.get(cache_key, datetime.min):
                return self.member_cache[cache_key]

        # Try to find member
        member = None
        try:
            if author_id:
                member = guild.get_member(author_id)

            if not member:
                # Only search by name if we don't have the member from ID
                author_name_lower = author_name.lower()
                member = discord.utils.get(guild.members, name=author_name)
                if not member:
                    member = discord.utils.get(guild.members, display_name=author_name)

            # Cache the result (even if None) for 5 minutes
            self.member_cache[cache_key] = member
            self.cache_expiry[cache_key] = current_time + timedelta(minutes=5)

        except Exception as e:
            logger.error(f"Error during member lookup: {type(e).__name__}: {e}")

        return member

    async def create_snipe_embed(self, ctx: commands.Context, deleted_msg: dict) -> discord.Embed:
        try:
            deleted_at = datetime.fromisoformat(deleted_msg['deleted_at'])
            time_diff = datetime.utcnow() - deleted_at
            readable_time = (
                (f"{time_diff.days}d " if time_diff.days > 0 else "") +
                (f"{time_diff.seconds // 3600}h " if time_diff.seconds >= 3600 else "") +
                (f"{(time_diff.seconds % 3600) // 60}m " if time_diff.seconds >= 60 else "") +
                f"{time_diff.seconds % 60}s"
            ).strip()

            # Handle both int and None types for author_id
            author_id = None
            if 'author_id' in deleted_msg and deleted_msg['author_id'] is not None:
                try:
                    author_id = int(deleted_msg['author_id'])
                except (ValueError, TypeError):
                    author_id = None

            # Use cached member lookup
            member = await self.get_member_cached(ctx.guild, author_id, deleted_msg['author'])

            # Get author mention
            author_mention = deleted_msg['author']
            if member:
                author_mention = member.mention
            elif author_id:
                author_mention = f"<@{author_id}>"

            # Format content
            content = deleted_msg['content'] or "*No content*"

            # Prepare content with any attachments
            content_section = content
            if deleted_msg['attachments']:
                attachments = deleted_msg['attachments'].split(',')
                if len(attachments) > 1:
                    attachment_list = "\n".join([f"[Attachment {i+1}]({url})" for i, url in enumerate(attachments)])
                    content_section += f"\n\n**Attachments:**\n{attachment_list}"

            embed = discord.Embed(
                title="Deleted Msgs",
                color=random.choice(self.embed_colors)
            )

            # Add fields exactly as requested
            embed.add_field(name="author mention", value=author_mention, inline=False)
            embed.add_field(name="deleted at", value=f"{readable_time}", inline=False)

            # Add the content section
            embed.add_field(name="content", value=content_section, inline=False)

            # Set the user's avatar as thumbnail
            if member and member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            elif author_id:
                # Fallback to default avatar url pattern if we only have the ID
                embed.set_thumbnail(url=f"https://cdn.discordapp.com/avatars/{author_id}/avatar.png")

            # Set the first attachment as image if there's only one
            if deleted_msg['attachments']:
                attachments = deleted_msg['attachments'].split(',')
                if len(attachments) == 1:
                    embed.set_image(url=attachments[0])

            # Set footer with requester info
            formatted_time = datetime.utcnow().strftime('%H:%M:%S')
            footer_text = f"requested by {ctx.author.name} | at {formatted_time}"
            footer_icon = ctx.author.display_avatar.url if ctx.author.display_avatar else None
            embed.set_footer(text=footer_text, icon_url=footer_icon)

            return embed
        except Exception as e:
            logger.error(f"Error creating snipe embed: {e}")
            # Return a basic error embed
            return discord.Embed(
                title="Error",
                description="Failed to create message embed",
                color=discord.Color.red()
            )

    @commands.command(name='snipe')
    @commands.has_permissions(administrator=True)
    @commands.cooldown(1, 5, commands.BucketType.user)  # 1 use per 5 seconds per user
    async def snipe(self, ctx: commands.Context) -> None:
        if not self.db_ready:
            embed = discord.Embed(
                title="Database Not Ready",
                description="The snipe database is still initializing. Please try again in a moment.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return

        try:

            cursor = self.collection.find({"channel_id": ctx.channel.id}).sort("deleted_at", -1).limit(10)
            deleted_msgs = await cursor.to_list(length=10)

            if not deleted_msgs:
                embed = discord.Embed(
                    title="No Messages Found",
                    description="No recently deleted messages found in this channel!",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            current_time = datetime.utcnow()
            valid_msgs = []

            for msg in deleted_msgs:
                try:
                    msg_time = datetime.fromisoformat(msg['deleted_at'])
                    if (current_time - msg_time) <= self.max_age:
                        valid_msgs.append(dict(msg))
                except Exception as e:
                    logger.error(f"Error processing message timestamp: {e}")
                    continue

            if not valid_msgs:
                embed = discord.Embed(
                    title="Messages Too Old",
                    description=f"All deleted messages are older than {self.max_age.days} days!",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            first_embed = await self.create_snipe_embed(ctx, valid_msgs[0])
            view = SnipeView(self, ctx, valid_msgs)

            try:
                sent_message = await ctx.reply(embed=first_embed, view=view)
                view.message = sent_message
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    await ctx.send("⚠️ Rate limited! Please wait before using this command again.")
                    return
                else:
                    raise

            # Add reaction to original command with rate limit handling
            try:
                await ctx.message.add_reaction("<a:sukoon_whitetick:1344600976962748458>")
            except discord.HTTPException as e:
                if e.status != 429:  # Don't log rate limit errors for reactions
                    pass
            except Exception:
                pass

        except discord.HTTPException as e:
            if e.status == 429:
                await ctx.send("⚠️ Rate limited! Please wait before using this command again.")
            else:
                logger.error(f"HTTP error in snipe command: {e}")
                await ctx.send("An HTTP error occurred. Please try again later.")
        except Exception as e:
            logger.error(f"Unexpected error in snipe command: {type(e).__name__}: {e}")
            error_embed = discord.Embed(
                title="Error",
                description="An unexpected error occurred while retrieving deleted messages.",
                color=discord.Color.red()
            )
            await ctx.send(embed=error_embed)

    @snipe.error
    async def snipe_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                title="Permission Denied",
                description="You need administrator permissions to use this command.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        elif isinstance(error, commands.CommandOnCooldown):
            embed = discord.Embed(
                title="Cooldown",
                description=f"Please wait {error.retry_after:.1f} seconds before using this command again.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
        else:
            logger.error(f"Command error: {type(error).__name__}: {error}")
            await ctx.send("An error occurred while executing the command.")

    def cog_unload(self):
        """Called when the cog is unloaded"""
        if self.cleanup_task:
            self.cleanup_task.cancel()
        self.member_cache.clear()
        self.cache_expiry.clear()
        Snipe._loaded = False

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Snipe(bot))
