import discord
from discord import app_commands
from discord.ext import commands, tasks
import motor.motor_asyncio
import os
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dotenv import load_dotenv
from typing import List, Optional

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL not found in environment")

# ----- Helpers -----


def discohook_to_embeds(disco_json) -> List[discord.Embed]:
    embeds: List[discord.Embed] = []
    try:
        embed_list = disco_json if isinstance(disco_json, list) else disco_json.get("embeds", [])
        if not isinstance(embed_list, list):
            return embeds
    except Exception:
        logging.exception("Invalid discohook JSON structure")
        return embeds

    for e in embed_list:
        try:
            if not isinstance(e, dict):
                continue
            
            # Validate embed has some content
            if not any([e.get("title"), e.get("description"), e.get("fields"), e.get("image"), e.get("thumbnail")]):
                logging.warning("Skipping empty embed")
                continue
                
            color = e.get("color", 0) or 0
            
            # Truncate title and description to Discord limits
            title = e.get("title")
            if title and len(title) > 256:
                title = title[:253] + "..."
            
            description = e.get("description")
            if description and len(description) > 4096:
                description = description[:4093] + "..."
            
            emb = discord.Embed(title=title, description=description, color=color)
            
            # Add fields (max 25 fields per embed)
            fields = (e.get("fields", []) or [])[:25]
            for f in fields:
                if not isinstance(f, dict):
                    continue
                name = f.get("name", "\u200b")
                value = f.get("value", "\u200b")
                inline = bool(f.get("inline", False))
                
                # Truncate field name and value to Discord limits
                if len(name) > 256:
                    name = name[:253] + "..."
                if len(value) > 1024:
                    value = value[:1021] + "..."
                    
                emb.add_field(name=name, value=value, inline=inline)
            
            # Add footer
            if e.get("footer"):
                footer_text = e["footer"].get("text", "")
                if len(footer_text) > 2048:
                    footer_text = footer_text[:2045] + "..."
                emb.set_footer(text=footer_text, icon_url=e["footer"].get("icon_url"))
            
            # Add image
            if e.get("image") and e["image"].get("url"):
                emb.set_image(url=e["image"].get("url"))
            
            # Add thumbnail
            if e.get("thumbnail") and e["thumbnail"].get("url"):
                emb.set_thumbnail(url=e["thumbnail"].get("url"))
            
            # Add author
            if e.get("author"):
                author_name = e["author"].get("name", "")
                if len(author_name) > 256:
                    author_name = author_name[:253] + "..."
                emb.set_author(name=author_name, icon_url=e["author"].get("icon_url"))
            
            embeds.append(emb)
        except Exception:
            logging.exception("Failed to parse one embed from discohook JSON")
            continue
    return embeds


def normalize_emoji_key(emoji_raw: Optional[str]) -> str:
    if not emoji_raw:
        return ""
    return emoji_raw.replace(":", "").replace(" ", "_")


def make_button_custom_id(guild_id: int, channel_id: int, button_index: int) -> str:
    # Use button index instead of emoji to avoid collisions and normalization issues
    # Discord custom_id has a 100 character limit
    base = f"stickybtn:{guild_id}:{channel_id}:{button_index}"
    if len(base) > 100:
        # Fallback to shorter format if too long
        base = f"sb:{guild_id % 10000}:{channel_id % 10000}:{button_index}"
    return base


def is_admin_check(interaction: discord.Interaction) -> bool:
    try:
        return interaction.user.guild_permissions.administrator
    except Exception:
        return False


# ----- Views / UI -----


class StickyView(discord.ui.View):
    """View used for the final sticky message containing multiple buttons."""

    def __init__(self, bot: commands.Bot, guild_id: int, channel_id: int, buttons_data: List[dict], *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.buttons_data = buttons_data or []
        self._build_buttons()

    def _parse_emoji(self, emoji_raw: Optional[str]):
        if not emoji_raw:
            return None
        try:
            return discord.PartialEmoji.from_str(emoji_raw)
        except Exception:
            return emoji_raw

    def _build_buttons(self):
        self.clear_items()
        # Limit checks
        max_buttons_supported = 20  # discord allows up to 25 components; keep margin
        if len(self.buttons_data) > max_buttons_supported:
            logging.warning("Too many buttons in sticky; only first %d will be shown", max_buttons_supported)
        for idx, btn in enumerate(self.buttons_data[:max_buttons_supported]):
            emoji_raw = btn.get("emoji")
            label = btn.get("label")
            
            # Discord requires at least emoji or label
            if not emoji_raw and not label:
                logging.warning("Button missing both emoji and label, skipping")
                continue
                
            custom_id = make_button_custom_id(self.guild_id, self.channel_id, idx)
            emoji = self._parse_emoji(emoji_raw)
            try:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    custom_id=custom_id,
                    emoji=emoji if emoji else None,
                    label=label if label else None
                )
                button.callback = self._create_callback(custom_id, idx)
                self.add_item(button)
            except Exception:
                logging.exception("Failed to add button to StickyView")

    def _create_callback(self, custom_id: str, button_index: int):
        async def callback(interaction: discord.Interaction):
            try:
                if not interaction.guild:
                    await interaction.response.send_message("This button works only in servers.", ephemeral=True)
                    return

                parts = custom_id.split(":", 3)
                if len(parts) < 4:
                    await interaction.response.send_message("Invalid button configuration.", ephemeral=True)
                    return

                prefix, guild_s, channel_s, btn_idx_s = parts
                
                # Handle both old and new custom_id formats
                if prefix == "sb":
                    # Short format - need to find the actual guild/channel from context
                    guild_id = interaction.guild.id if interaction.guild else 0
                    channel_id = interaction.channel.id if interaction.channel else 0
                else:
                    # Full format
                    try:
                        guild_id = int(guild_s)
                        channel_id = int(channel_s)
                    except Exception:
                        await interaction.response.send_message("Invalid button target.", ephemeral=True)
                        return
                
                try:
                    btn_idx = int(btn_idx_s)
                except Exception:
                    await interaction.response.send_message("Invalid button index.", ephemeral=True)
                    return

                db = getattr(self.bot, "mongo_db", None)
                if db is None:
                    await interaction.response.send_message("Database not available.", ephemeral=True)
                    return

                coll = db.stickies
                sticky = await coll.find_one({"guild_id": guild_id, "channel_id": channel_id})
                if not sticky:
                    await interaction.response.send_message("No configuration found for this button.", ephemeral=True)
                    return
                    return

                buttons = sticky.get("buttons") or []
                if btn_idx < 0 or btn_idx >= len(buttons):
                    await interaction.response.send_message("Button configuration not found.", ephemeral=True)
                    return

                target_btn = buttons[btn_idx]
                embed_dicts = target_btn.get("embed_data") or []
                
                # Handle empty embed data
                if not embed_dicts:
                    await interaction.response.send_message("This button has no content configured.", ephemeral=True)
                    return
                
                embeds = discohook_to_embeds(embed_dicts if isinstance(embed_dicts, dict) else {"embeds": embed_dicts})
                if not embeds:
                    await interaction.response.send_message("No valid embeds configured for this button.", ephemeral=True)
                    return

                # send ephemeral (limit embeds to 10 per Discord's limit)
                try:
                    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)
                except discord.HTTPException as e:
                    # Fallback: send embeds one by one
                    try:
                        if embeds:
                            await interaction.response.send_message(embed=embeds[0], ephemeral=True)
                            for embed in embeds[1:10]:
                                await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message("Failed to display embeds.", ephemeral=True)
                    except Exception:
                        try:
                            if not interaction.response.is_done():
                                await interaction.response.send_message("Error displaying button content.", ephemeral=True)
                            else:
                                await interaction.followup.send("Error displaying button content.", ephemeral=True)
                        except Exception:
                            pass
            except Exception:
                logging.exception("Error in button callback")
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("An error occurred while processing the button.", ephemeral=True)
                    else:
                        await interaction.followup.send("An error occurred while processing the button.", ephemeral=True)
                except Exception:
                    pass

        return callback


class StickyManagerSelect(discord.ui.Select):
    def __init__(self, cog: "StickyCog"):
        options = [
            discord.SelectOption(label="Setup / Edit sticky text", value="setup", description="Create or update the sticky message text"),
            discord.SelectOption(label="Add Button (Discohook JSON)", value="add_button", description="Add a new button by pasting Discohook JSON"),
            discord.SelectOption(label="Remove Button", value="remove_button", description="Remove a button by its number"),
            discord.SelectOption(label="List Buttons", value="list_buttons", description="List all configured buttons with numbers"),
            discord.SelectOption(label="Remove sticky", value="remove_sticky", description="Delete sticky from this channel"),
        ]
        super().__init__(placeholder="Choose an action...", min_values=1, max_values=1, options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        # Only admins can use management functions
        if not is_admin_check(interaction):
            await interaction.response.send_message("You must be an administrator to use this.", ephemeral=True)
            return

        choice = self.values[0]
        if choice == "setup":
            await interaction.response.send_modal(StickyTextModal(self.cog, interaction))
        elif choice == "add_button":
            await interaction.response.send_modal(ButtonEmojiModal(self.cog, interaction))
        elif choice == "remove_button":
            await interaction.response.send_modal(RemoveButtonModal(self.cog, interaction))
        elif choice == "list_buttons":
            await self.cog._handle_list_buttons(interaction)
        elif choice == "remove_sticky":
            await self.cog._handle_remove_sticky(interaction)
        else:
            await interaction.response.send_message("Unknown option.", ephemeral=True)


class StickyManagerView(discord.ui.View):
    def __init__(self, cog: "StickyCog", *, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.add_item(StickyManagerSelect(cog))


# ----- Modals -----


class StickyTextModal(discord.ui.Modal, title="Sticky Message Setup"):
    text = discord.ui.TextInput(label="Sticky text", style=discord.TextStyle.long, placeholder="Enter the sticky message text (plain text).", required=True, max_length=2000)

    def __init__(self, cog: "StickyCog", interaction_ctx: discord.Interaction):
        super().__init__()
        self.cog = cog
        self.interaction_ctx = interaction_ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            channel = interaction.channel
            if not guild or not channel:
                await interaction.response.send_message("This command must be used in a server channel.", ephemeral=True)
                return

            sticky_text = self.text.value.strip()
            
            # Validate text length
            if len(sticky_text) > 2000:
                await interaction.response.send_message("Sticky text is too long (max 2000 characters).", ephemeral=True)
                return

            coll = self.cog.stickies
            # Upsert sticky text
            await coll.update_one(
                {"guild_id": guild.id, "channel_id": channel.id},
                {"$set": {"guild_id": guild.id, "channel_id": channel.id, "text": sticky_text, "last_repost": datetime.now(timezone.utc)}},
                upsert=True
            )
            # Force immediate repost
            await self.cog.repost_sticky(channel, force=True)
            await interaction.response.send_message("Sticky saved and posted.", ephemeral=True)
        except Exception:
            logging.exception("Failed to save sticky text from modal")
            try:
                await interaction.response.send_message("Failed to save sticky. Check the bot logs.", ephemeral=True)
            except Exception:
                pass


class ButtonEmojiModal(discord.ui.Modal, title="Add/Replace Button (Discohook JSON)"):
    emoji = discord.ui.TextInput(label="Emoji (optional)", style=discord.TextStyle.short, placeholder="Emoji like 💬 or <:name:id>", required=False, max_length=80)
    label = discord.ui.TextInput(label="Label (optional)", style=discord.TextStyle.short, placeholder="Optional label (emoji or label required)", required=False, max_length=80)
    json_data = discord.ui.TextInput(label="Discohook JSON", style=discord.TextStyle.long,
                                     placeholder='Paste Discohook JSON here (the {"embeds":[...]} JSON).', required=True, max_length=4000)

    def __init__(self, cog: "StickyCog", interaction_ctx: discord.Interaction):
        super().__init__()
        self.cog = cog
        self.interaction_ctx = interaction_ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            channel = interaction.channel
            if not guild or not channel:
                await interaction.response.send_message("This command must be used in a server channel.", ephemeral=True)
                return

            # Parse JSON
            try:
                disco_json = json.loads(self.json_data.value)
            except Exception:
                await interaction.response.send_message("Invalid JSON. Paste a valid Discohook JSON with an 'embeds' list.", ephemeral=True)
                return

            if not isinstance(disco_json, dict) or "embeds" not in disco_json or not isinstance(disco_json["embeds"], list):
                await interaction.response.send_message("The JSON doesn't look like a Discohook embed file (missing 'embeds' list).", ephemeral=True)
                return

            emoji_raw = self.emoji.value.strip() if self.emoji.value else ""
            label_val = self.label.value.strip() if self.label.value else None

            # Validate at least one of emoji or label is provided
            if not emoji_raw and not label_val:
                await interaction.response.send_message("You must provide at least an emoji or a label for the button.", ephemeral=True)
                return

            coll = self.cog.stickies
            sticky = await coll.find_one({"guild_id": guild.id, "channel_id": channel.id})
            
            # Check button limit (Discord max is 25, we use 20 for safety)
            existing_buttons = sticky.get("buttons", []) if sticky else []
            if len(existing_buttons) >= 20:
                await interaction.response.send_message("Maximum button limit reached (20 buttons). Remove some buttons before adding new ones.", ephemeral=True)
                return
            
            new_button = {"emoji": emoji_raw, "label": label_val, "embed_data": disco_json.get("embeds", [])}
            
            if not sticky:
                base = {
                    "guild_id": guild.id,
                    "channel_id": channel.id,
                    "text": "",
                    "buttons": [new_button],
                    "last_repost": datetime.now(timezone.utc)
                }
                await coll.insert_one(base)
            else:
                # Simply append the new button - allow duplicate emojis
                buttons = sticky.get("buttons", []) or []
                buttons.append(new_button)
                await coll.update_one({"guild_id": guild.id, "channel_id": channel.id}, {"$set": {"buttons": buttons}})

            # Repost sticky to update buttons
            await self.cog.repost_sticky(channel, force=True)
            await interaction.response.send_message("Button added and saved.", ephemeral=True)
        except Exception:
            logging.exception("Failed to handle add-button modal")
            try:
                await interaction.response.send_message("Failed to add button. Check bot logs.", ephemeral=True)
            except Exception:
                pass


class RemoveButtonModal(discord.ui.Modal, title="Remove Button by Index"):
    index = discord.ui.TextInput(label="Button number to remove", style=discord.TextStyle.short, placeholder="Enter button number (1, 2, 3, etc.)", required=True, max_length=10)

    def __init__(self, cog: "StickyCog", interaction_ctx: discord.Interaction):
        super().__init__()
        self.cog = cog
        self.interaction_ctx = interaction_ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            channel = interaction.channel
            if not guild or not channel:
                await interaction.response.send_message("This command must be used in a server channel.", ephemeral=True)
                return

            sticky = await self.cog.stickies.find_one({"guild_id": guild.id, "channel_id": channel.id})
            if not sticky:
                await interaction.response.send_message("No sticky configured for this channel.", ephemeral=True)
                return

            try:
                btn_num = int(self.index.value.strip())
                if btn_num < 1:
                    await interaction.response.send_message("Button number must be 1 or greater.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Invalid number. Please enter a valid button number.", ephemeral=True)
                return

            buttons = sticky.get("buttons", []) or []
            btn_idx = btn_num - 1  # Convert to 0-based index
            
            if btn_idx >= len(buttons):
                await interaction.response.send_message(f"Button #{btn_num} doesn't exist. There are only {len(buttons)} button(s).", ephemeral=True)
                return

            # Remove the button at the specified index
            removed_btn = buttons.pop(btn_idx)
            await self.cog.stickies.update_one({"guild_id": guild.id, "channel_id": channel.id}, {"$set": {"buttons": buttons}})
            await self.cog.repost_sticky(channel, force=True)
            
            emoji_display = removed_btn.get("emoji", "")
            label_display = removed_btn.get("label", "")
            display = f"{emoji_display} {label_display}".strip() or "(no label)"
            await interaction.response.send_message(f"Button #{btn_num} ({display}) removed.", ephemeral=True)
        except Exception:
            logging.exception("Error in RemoveButtonModal")
            await interaction.response.send_message("An error occurred.", ephemeral=True)


# ----- Cog -----


class StickyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            # Fixed: Use lowercase parameter name as per motor documentation
            self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL, serverselectiontimeoutms=5000)
            self.db = self.mongo_client.discord_bot
            setattr(self.bot, "mongo_db", self.db)
            # Test connection immediately
            self.bot.loop.create_task(self._test_mongo_connection())
        except Exception:
            logging.exception("Failed to create Motor client")
            raise

        self.stickies = self.db.stickies
        self.rate_limits = defaultdict(deque)
        self.last_sticky_messages = {}  # channel_id -> message_id
        self.registered_views = set()  # track registered view keys to prevent duplicates
        self.repost_queue = asyncio.Queue(maxsize=1000)
        self.repost_task = None
        self.processing_channels = set()

        # start tasks (periodic_repost set to 1 minute per requirement)
        try:
            self.cleanup_task.start()
            self.periodic_repost.start()
            self.recovery_task.start()
            self.health_check_task.start()
        except RuntimeError:
            self.bot.loop.create_task(self._start_tasks_on_ready())

        # re-add views on ready
        self.bot.loop.create_task(self._readd_views_on_ready())
        
        # Start queue processor
        self.bot.loop.create_task(self._start_queue_processor())

    async def _test_mongo_connection(self):
        try:
            await self.mongo_client.admin.command('ping')
            logging.info("MongoDB connection successful")
        except Exception:
            logging.exception("Failed to connect to MongoDB - operations may fail")

    async def _start_queue_processor(self):
        await self.bot.wait_until_ready()
        # Add a small delay to ensure other initialization is complete
        await asyncio.sleep(1)
        if self.repost_task is None or self.repost_task.done():
            self.repost_task = asyncio.create_task(self._process_repost_queue())
            logging.info("Started repost queue processor")

    async def _start_tasks_on_ready(self):
        await self.bot.wait_until_ready()
        try:
            if not self.cleanup_task.is_running():
                self.cleanup_task.start()
                logging.info("Started cleanup task")
            if not self.periodic_repost.is_running():
                self.periodic_repost.start()
                logging.info("Started periodic repost task")
            if not self.recovery_task.is_running():
                self.recovery_task.start()
                logging.info("Started recovery task")
            if not self.health_check_task.is_running():
                self.health_check_task.start()
                logging.info("Started health check task")
        except Exception:
            logging.exception("Failed to start tasks on ready")

    def cog_unload(self):
        try:
            if getattr(self, "cleanup_task", None) and self.cleanup_task.is_running():
                self.cleanup_task.cancel()
            if getattr(self, "periodic_repost", None) and self.periodic_repost.is_running():
                self.periodic_repost.cancel()
            if getattr(self, "recovery_task", None) and self.recovery_task.is_running():
                self.recovery_task.cancel()
            if getattr(self, "health_check_task", None) and self.health_check_task.is_running():
                self.health_check_task.cancel()
            if getattr(self, "repost_task", None):
                self.repost_task.cancel()
            # DO NOT close mongo_client here - it's shared across cogs
            # The bot should handle closing the client on shutdown
        except Exception:
            logging.exception("Error during cog_unload cleanup")

    # repost queue processor
    async def _process_repost_queue(self):
        while True:
            try:
                # Use a timeout to prevent hanging indefinitely
                try:
                    channel_id, force = await asyncio.wait_for(self.repost_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Timeout is normal, just continue the loop
                    continue
                    
                try:
                    if channel_id in self.processing_channels:
                        self.repost_queue.task_done()
                        continue
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        self.processing_channels.add(channel_id)
                        try:
                            await self._repost_sticky_internal(channel, force)
                        finally:
                            self.processing_channels.discard(channel_id)
                except Exception:
                    logging.exception("Error processing individual repost for channel %s", channel_id)
                finally:
                    self.repost_queue.task_done()
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                logging.info("Repost queue processor cancelled")
                break
            except Exception:
                logging.exception("Critical error in repost queue processor - restarting")
                await asyncio.sleep(5)  # Wait before continuing to prevent rapid restart loops

    # tasks
    @tasks.loop(minutes=5)
    async def cleanup_task(self):
        try:
            now = datetime.now(timezone.utc)
            
            # Clean up rate limits
            for channel_id in list(self.rate_limits.keys()):
                try:
                    while self.rate_limits[channel_id] and now - self.rate_limits[channel_id][0] > timedelta(seconds=5):
                        self.rate_limits[channel_id].popleft()
                    if not self.rate_limits[channel_id]:
                        del self.rate_limits[channel_id]
                except Exception:
                    logging.exception("Error cleaning rate limits for channel %s", channel_id)
            
            # Clean up last sticky messages for deleted channels
            for ch in list(self.last_sticky_messages.keys()):
                if not self.bot.get_channel(ch):
                    del self.last_sticky_messages[ch]
            
            # Clean up processing channels set (in case of stuck entries)
            for ch_id in list(self.processing_channels):
                if not self.bot.get_channel(ch_id):
                    self.processing_channels.discard(ch_id)
            
            # Restart queue processor if it died
            if self.repost_task and self.repost_task.done():
                logging.warning("Repost queue processor died, restarting...")
                self.repost_task = asyncio.create_task(self._process_repost_queue())
            
            # Restart health check task if it died
            if hasattr(self, 'health_check_task') and not self.health_check_task.is_running():
                logging.warning("Health check task died, restarting...")
                try:
                    self.health_check_task.start()
                except RuntimeError:
                    pass  # Already running
                
        except asyncio.CancelledError:
            # Task cancelled during reload/shutdown - expected
            raise
        except Exception:
            logging.exception("Error in cleanup_task")

    @tasks.loop(minutes=1)
    async def periodic_repost(self):
        # Periodic check to ensure stickies are at the bottom of channels
        # Only reposts if the last message in the channel is not the sticky
        try:
            # Check database connection health
            try:
                # Check if MongoDB client is still open
                if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                    logging.warning("MongoDB client is closed, skipping periodic repost")
                    return
                await self.mongo_client.admin.command('ping')
            except Exception:
                logging.error("MongoDB connection lost during periodic repost")
                return
                
            # Process stickies in batches to avoid overwhelming the system
            processed_count = 0
            max_per_cycle = 10  # Limit processing to prevent blocking
            
            async for sticky in self.stickies.find({}):
                if processed_count >= max_per_cycle:
                    break
                    
                channel_id = sticky.get("channel_id")
                if not channel_id:
                    continue
                    
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    # Channel no longer exists or bot doesn't have access
                    continue
                    
                if channel.id in self.processing_channels:
                    continue
                    
                try:
                    # Check if the last message is the sticky
                    last_msg = None
                    async for msg in channel.history(limit=1):
                        last_msg = msg
                        break
                    
                    # Only repost if:
                    # 1. There's a message in the channel
                    # 2. Last message is not from the bot OR
                    # 3. Last message is from bot but not the tracked sticky
                    if last_msg and (last_msg.author != self.bot.user or last_msg.id != self.last_sticky_messages.get(channel.id)):
                        await self.repost_sticky(channel, force=False)  # Use non-forced to respect rate limits
                        processed_count += 1
                        await asyncio.sleep(0.5)
                except (discord.Forbidden, discord.HTTPException) as e:
                    logging.warning("Cannot access channel %s for periodic repost: %s", channel.id, e)
                except Exception:
                    logging.exception("Error reposting sticky for channel %s", channel.id)
        except asyncio.CancelledError:
            # Task cancelled during reload/shutdown - expected
            raise
        except Exception:
            logging.exception("Error in periodic_repost")

    @tasks.loop(count=1)
    async def recovery_task(self):
        await self.bot.wait_until_ready()
        try:
            # Check if MongoDB client is still open
            if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                logging.warning("MongoDB client is closed, skipping recovery task")
                return
            
            async for sticky in self.stickies.find():
                channel = self.bot.get_channel(sticky["channel_id"])
                if not channel:
                    continue
                try:
                    async for message in channel.history(limit=50):
                        if message.author == self.bot.user and (message.content or "") == (sticky.get("text") or ""):
                            self.last_sticky_messages[channel.id] = message.id
                            break
                except (discord.Forbidden, discord.HTTPException):
                    continue
        except Exception:
            logging.exception("Error in recovery_task")

    @tasks.loop(minutes=10)
    async def health_check_task(self):
        """Health check task to ensure sticky system is working properly"""
        try:
            # Check MongoDB connection
            try:
                # Check if MongoDB client is still open
                if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                    logging.warning("MongoDB client is closed, skipping health check")
                    return
                await self.mongo_client.admin.command('ping')
            except Exception:
                logging.error("MongoDB connection lost in health check")
                return

            # Restart queue processor if it died
            if not self.repost_task or self.repost_task.done():
                logging.warning("Repost queue processor died, restarting...")
                self.repost_task = asyncio.create_task(self._process_repost_queue())

            # Check for stickies that haven't been reposted in a while
            cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=30)
            async for sticky in self.stickies.find({"last_repost": {"$lt": cutoff_time}}):
                channel = self.bot.get_channel(sticky.get("channel_id"))
                if channel:
                    logging.info("Health check triggering repost for stale sticky in channel %s", channel.id)
                    await self.repost_sticky(channel, force=True)
                    await asyncio.sleep(1)  # Prevent rate limits

        except Exception:
            logging.exception("Error in health_check_task")

    async def _readd_views_on_ready(self):
        await self.bot.wait_until_ready()
        try:
            # Check if MongoDB client is still open
            if self.mongo_client is None or not hasattr(self.mongo_client, '_topology') or self.mongo_client._topology._closed:
                logging.warning("MongoDB client is closed, skipping view re-add")
                return
            
            async for sticky in self.stickies.find():
                try:
                    # Validate required fields exist
                    if "guild_id" not in sticky or "channel_id" not in sticky:
                        logging.warning("Skipping sticky with missing guild_id or channel_id: %s", sticky.get("_id"))
                        continue
                    
                    view_key = f"{sticky['guild_id']}:{sticky['channel_id']}"
                    if view_key not in self.registered_views:
                        view = StickyView(self.bot, sticky["guild_id"], sticky["channel_id"], sticky.get("buttons", []) or [], timeout=None)
                        self.bot.add_view(view)
                        self.registered_views.add(view_key)
                except Exception:
                    logging.exception("Failed to add view for sticky during ready re-add")
        except Exception:
            logging.exception("Failed to iterate stickies during ready re-add")

    # DB helpers
    async def get_sticky(self, guild_id, channel_id):
        try:
            return await self.stickies.find_one({"guild_id": guild_id, "channel_id": channel_id})
        except Exception:
            logging.exception("DB get_sticky failed for guild %s channel %s", guild_id, channel_id)
            return None

    async def set_sticky(self, guild_id, channel_id, text):
        try:
            await self.stickies.update_one(
                {"guild_id": guild_id, "channel_id": channel_id},
                {"$set": {"guild_id": guild_id, "channel_id": channel_id, "text": text, "last_repost": datetime.now(timezone.utc)}},
                upsert=True
            )
            return True
        except Exception:
            logging.exception("DB set_sticky failed for guild %s channel %s", guild_id, channel_id)
            return False

    async def remove_sticky(self, guild_id, channel_id):
        try:
            result = await self.stickies.delete_one({"guild_id": guild_id, "channel_id": channel_id})
            # Clean up registered view tracking
            view_key = f"{guild_id}:{channel_id}"
            self.registered_views.discard(view_key)
            # Clean up last message tracking
            if channel_id in self.last_sticky_messages:
                del self.last_sticky_messages[channel_id]
            return result.deleted_count > 0
        except Exception:
            logging.exception("DB remove_sticky")
            return False

    # repost logic utilities
    async def is_rate_limited(self, channel_id):
        now = datetime.now(timezone.utc)
        q = self.rate_limits[channel_id]
        # Clean up old entries
        while q and now - q[0] > timedelta(seconds=5):
            q.popleft()
        # More lenient rate limiting - allow 3 reposts in 5 seconds instead of 2
        is_limited = len(q) >= 3
        
        # Clean up empty deques to prevent memory leaks
        if not q:
            del self.rate_limits[channel_id]
            
        return is_limited

    async def add_rate_limit(self, channel_id):
        self.rate_limits[channel_id].append(datetime.now(timezone.utc))

    async def _delete_existing_bot_stickies(self, channel: discord.TextChannel, sticky_text: str):
        # Deletes any bot messages in the last N messages in the channel that match the sticky text.
        try:
            to_delete = []
            async for m in channel.history(limit=50):
                # Delete bot's sticky messages (match by content or by tracked message ID)
                if m.author == self.bot.user:
                    # Match by content or by being the tracked sticky message
                    if (sticky_text and (m.content or "") == sticky_text) or m.id == self.last_sticky_messages.get(channel.id):
                        to_delete.append(m)
            
            # Delete in reverse order (newest first) to avoid issues
            for m in reversed(to_delete):
                try:
                    await m.delete()
                    # Remove from tracking if it was the tracked message
                    if m.id == self.last_sticky_messages.get(channel.id):
                        del self.last_sticky_messages[channel.id]
                    await asyncio.sleep(0.1)  # Small delay to avoid rate limits
                except discord.NotFound:
                    # Message already deleted, remove from tracking if needed
                    if m.id == self.last_sticky_messages.get(channel.id):
                        del self.last_sticky_messages[channel.id]
                except discord.Forbidden:
                    logging.warning("Missing permissions to delete message in channel %s", channel.id)
                    break
                except discord.HTTPException as e:
                    logging.warning("Failed to delete message in channel %s: %s", channel.id, e)
        except discord.Forbidden:
            logging.warning("Missing permissions to read history in channel %s", channel.id)
        except Exception:
            logging.exception("Failed scanning/deleting old bot sticky messages in channel %s", channel.id)

    async def _repost_sticky_internal(self, channel: discord.TextChannel, force=False):
        if not force and await self.is_rate_limited(channel.id):
            logging.debug("Rate limited repost for channel %s", channel.id)
            return False
        
        # Validate channel still exists and bot has access
        if not channel or not channel.guild:
            logging.warning("Invalid channel or guild for repost")
            return False
            
        sticky = await self.get_sticky(channel.guild.id, channel.id)
        if not sticky:
            logging.debug("No sticky found for channel %s", channel.id)
            return False
            
        try:
            text = sticky.get("text", "") or ""
            buttons = sticky.get("buttons", []) or []

            # Validate we have something to post
            if not text and not buttons:
                logging.warning("Sticky for channel %s has no text and no buttons, skipping repost", channel.id)
                return False

            # Delete any previous bot stickies matching this content (avoid duplicates)
            try:
                await self._delete_existing_bot_stickies(channel, text)
            except Exception:
                logging.exception("Error trying to cleanup old sticky messages")

            # Create view only if there are buttons
            view = None
            if buttons:
                view_key = f"{sticky['guild_id']}:{sticky['channel_id']}"
                view = StickyView(self.bot, sticky["guild_id"], sticky["channel_id"], buttons, timeout=None)
                
                # Always add view to bot (discord.py handles duplicates)
                try:
                    self.bot.add_view(view)
                    self.registered_views.add(view_key)
                except Exception as e:
                    logging.exception("Failed to persist view: %s", e)

            # Send the sticky message
            msg = await channel.send(content=text or "\u200b", view=view)
            self.last_sticky_messages[channel.id] = msg.id
            logging.debug("Successfully reposted sticky for channel %s", channel.id)

            try:
                await self.stickies.update_one(
                    {"guild_id": channel.guild.id, "channel_id": channel.id}, 
                    {"$set": {"last_repost": datetime.now(timezone.utc)}},
                    upsert=False
                )
            except Exception:
                logging.exception("Failed to update last_repost in DB")

            if not force:
                await self.add_rate_limit(channel.id)
            return True
        except discord.Forbidden:
            logging.error("Missing permissions to post in channel %s - consider removing sticky", channel.id)
            return False
        except discord.HTTPException as e:
            logging.exception("HTTPException while reposting sticky: %s", e)
            # Don't retry on certain errors
            if e.status in (403, 404):  # Forbidden or Not Found
                return False
            # Retry once with exponential backoff
            if not force:
                await asyncio.sleep(2)
                return await self._repost_sticky_internal(channel, True)
        except Exception:
            logging.exception("Unexpected error in _repost_sticky_internal for channel %s", channel.id)
        return False

    async def repost_sticky(self, channel: discord.TextChannel, force=False):
        try:
            # Validate channel
            if not channel:
                logging.warning("repost_sticky called with None channel")
                return
            if not hasattr(channel, 'guild') or not channel.guild:
                logging.warning("repost_sticky called with invalid channel (no guild)")
                return
            if not isinstance(channel, discord.TextChannel):
                logging.warning("repost_sticky called with non-TextChannel: %s", type(channel))
                return
                
            sticky = await self.get_sticky(channel.guild.id, channel.id)
            if not sticky:
                return
            
            # Prevent duplicate queuing
            if channel.id in self.processing_channels:
                return
            
            # Ensure queue processor is running
            if not self.repost_task or self.repost_task.done():
                logging.warning("Repost task died, restarting...")
                self.repost_task = asyncio.create_task(self._process_repost_queue())
            
            try:
                self.repost_queue.put_nowait((channel.id, force))
            except asyncio.QueueFull:
                logging.warning("Repost queue full; dropping repost request for channel %s", channel.id)
        except Exception:
            logging.exception("Error queuing repost for channel %s", getattr(channel, 'id', 'unknown'))

    # events
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild or not isinstance(message.channel, discord.TextChannel):
            return
        
        # Don't repost if the message is the sticky itself (prevent loops)
        if message.id == self.last_sticky_messages.get(message.channel.id):
            return
            
        # Check if sticky exists before queuing repost
        try:
            sticky = await self.get_sticky(message.guild.id, message.channel.id)
            if sticky:
                # schedule repost (non-forced to allow rate limiting)
                await self.repost_sticky(message.channel, force=False)
        except Exception:
            logging.exception("Error in on_message handler for channel %s", message.channel.id)

    # Management helpers for modal callbacks
    async def _handle_list_buttons(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            channel = interaction.channel
            if not guild or not channel:
                await interaction.response.send_message("This must be used in a server channel.", ephemeral=True)
                return
            sticky = await self.stickies.find_one({"guild_id": guild.id, "channel_id": channel.id})
            if not sticky or not (sticky.get("buttons") or []):
                await interaction.response.send_message("No buttons configured for this channel.", ephemeral=True)
                return
            lines = ["**Configured Buttons:**"]
            for idx, b in enumerate(sticky.get("buttons", []) or [], start=1):
                emoji = b.get('emoji', '')
                label = b.get('label') or '(no label)'
                embed_count = len(b.get('embed_data', []) or [])
                lines.append(f"**{idx}.** {emoji} {label} — {embed_count} embed(s)")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception:
            logging.exception("Error in _handle_list_buttons")
            await interaction.response.send_message("An error occurred.", ephemeral=True)

    async def _handle_remove_sticky(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            channel = interaction.channel
            if not guild or not channel:
                await interaction.response.send_message("This must be used in a server channel.", ephemeral=True)
                return
            sticky = await self.get_sticky(guild.id, channel.id)
            if not sticky:
                await interaction.response.send_message("No sticky configured for this channel.", ephemeral=True)
                return
            # delete any bot messages matching sticky text
            try:
                await self._delete_existing_bot_stickies(channel, sticky.get("text", "") or "")
            except Exception:
                logging.exception("Failed cleaning old messages during remove")
            success = await self.remove_sticky(guild.id, channel.id)
            if success:
                await interaction.response.send_message("Sticky removed for this channel.", ephemeral=True)
            else:
                await interaction.response.send_message("Failed to remove sticky (DB error).", ephemeral=True)
        except Exception:
            logging.exception("Error in _handle_remove_sticky")
            await interaction.response.send_message("An error occurred.", ephemeral=True)

    # Single visible slash command that opens the manager dropdown
    @app_commands.command(name="sticky", description="Open sticky manager (setup/add buttons/list/remove) — admin only for changes")
    async def sticky(self, interaction: discord.Interaction):
        try:
            # Present an ephemeral manager view with a modern select dropdown
            view = StickyManagerView(self, timeout=60.0)
            await interaction.response.send_message("Sticky manager — choose an action from the dropdown below.", view=view, ephemeral=True)
        except Exception:
            logging.exception("Error opening sticky manager")
            try:
                await interaction.response.send_message("Failed to open sticky manager.", ephemeral=True)
            except Exception:
                pass


# Cog setup
async def setup(bot: commands.Bot):
    cog = StickyCog(bot)
    await bot.add_cog(cog)
    # register command (single root command)
    try:
        # ensure we don't register duplicates
        existing_cmd = discord.utils.get(bot.tree.get_commands(), name="sticky")
        if existing_cmd:
            try:
                bot.tree.remove_command("sticky")
            except Exception:
                pass
        bot.tree.add_command(cog.sticky)
        # Sync is handled by main.py - don't sync here to avoid rate limits
        logging.info("Sticky button command registered (sync handled by main.py)")
    except Exception as e:
        logging.warning(f"Note: Command registration handled: {str(e)}")