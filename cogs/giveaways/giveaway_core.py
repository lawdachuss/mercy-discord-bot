import discord
import random
import asyncio
import pytz
import os
import time
import json
from datetime import datetime, timezone
from discord.ext import commands, tasks
from discord import ButtonStyle, ui, TextChannel
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

# Load environment variables
load_dotenv()

# Import configuration
try:
    from cogs.giveaways.config import (
        REACTION_EMOJI, DOT_EMOJI, RED_DOT_EMOJI, EMBED_COLOR,
        CLEANUP_INTERVAL, ENTRIES_PER_PAGE, GiveawayConfig,
        MIN_GIVEAWAY_DURATION, MAX_GIVEAWAY_DURATION, MIN_WINNERS, MAX_WINNERS,
        DURATION_UNITS, PRIZE_EMOJI, WINNER_EMOJI, TIME_EMOJI, GIVEAWAY_THUMBNAIL_URL,
        FOOTER_ICON_URL, GIFT_EMOJI
    )
except ImportError:
    # Fallback if GIVEAWAY_THUMBNAIL_URL not in config yet
    from cogs.giveaways.config import (
        REACTION_EMOJI, DOT_EMOJI, RED_DOT_EMOJI, EMBED_COLOR,
        CLEANUP_INTERVAL, ENTRIES_PER_PAGE, GiveawayConfig,
        MIN_GIVEAWAY_DURATION, MAX_GIVEAWAY_DURATION, MIN_WINNERS, MAX_WINNERS,
        DURATION_UNITS
    )
    # Temporary fallback values
    PRIZE_EMOJI = "<:ogs_gif:1428639542100885585>"
    WINNER_EMOJI = "<:ogs_crow:1428639113317453825>"
    TIME_EMOJI = "<:ogs_time:1428638675608141906>"
    GIFT_EMOJI = "<a:ogs_gift:1428659726790426686>"
    GIVEAWAY_THUMBNAIL_URL = "https://images-ext-1.discordapp.net/external/7RBwotHDp9qC1T5jYqRrwYTE_QQk7jAsJTiYkJ5DAyo/https/i.postimg.cc/j5x98YMw/1f381.gif?width=640&height=640"
    FOOTER_ICON_URL = "https://media.discordapp.net/attachments/1428636041538965607/1428647953496539227/b8b7454ac714509f8c173209f79496a9-removebg-preview.png"

def get_current_utc_timestamp():
    """Get current UTC timestamp as integer."""
    return int(time.time())

def get_utc_datetime():
    """Get current UTC datetime object."""
    return datetime.now(timezone.utc)

def create_fake_participant_id(original_user_id: str, sequence: int) -> str:
    """Create a fake participant ID with proper validation.
    
    Args:
        original_user_id: The original Discord user ID
        sequence: Sequence number for this fake entry
        
    Returns:
        Formatted fake participant ID
    """
    if not original_user_id or not str(original_user_id).isdigit():
        raise ValueError(f"Invalid user ID: {original_user_id}")
    return f"fake_{original_user_id}_{sequence}"

def parse_fake_participant_id(fake_id: str) -> Optional[str]:
    """Extract original user ID from fake participant ID.
    
    Args:
        fake_id: The fake participant ID
        
    Returns:
        Original user ID if valid fake ID, None otherwise
    """
    if not fake_id or not isinstance(fake_id, str):
        return None
    
    # New format: fake_<user_id>_<sequence>
    if fake_id.startswith("fake_"):
        parts = fake_id.split("_")
        if len(parts) >= 3 and parts[1].isdigit():
            return parts[1]
    
    # Legacy format: <user_id>_fake_<sequence>
    if "_fake_" in fake_id:
        parts = fake_id.split("_fake_")
        if len(parts) == 2 and parts[0].isdigit():
            return parts[0]
    
    return None

def is_fake_participant(user_id: str) -> bool:
    """Check if a user ID represents a fake participant.
    
    Args:
        user_id: The user ID to check
        
    Returns:
        True if fake participant, False otherwise
    """
    return user_id.startswith("fake_") or "_fake_" in user_id

def get_thumbnail_url(guild):
    """Get thumbnail URL with fallback to server icon."""
    try:
        # Try to use the custom thumbnail
        if GIVEAWAY_THUMBNAIL_URL and GIVEAWAY_THUMBNAIL_URL.startswith('http'):
            return GIVEAWAY_THUMBNAIL_URL
    except Exception:
        pass
    
    # Fallback to server icon
    if guild and hasattr(guild, 'icon') and guild.icon:
        return guild.icon.url
    
    return None

def format_time_display(timestamp, display_timezone='UTC'):
    """(Unused) Legacy formatting; we now use Discord native timestamps."""
    try:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if display_timezone != 'UTC':
            try:
                dt = dt.astimezone(pytz.timezone(display_timezone))
            except pytz.UnknownTimeZoneError:
                pass

        time_part = dt.strftime("%I:%M %p").lstrip("0")
        today = get_utc_datetime()
        if display_timezone != 'UTC':
            try:
                today = today.astimezone(pytz.timezone(display_timezone))
            except pytz.UnknownTimeZoneError:
                pass

        return f"{dt.strftime('%A')} at {time_part}" if dt.date() > today.date() else f"Today at {time_part}"
    except Exception:
        return "Unknown time"

# Note: Button-based system removed - now using reaction-only system
# Users react with the giveaway emoji to join, handled by on_raw_reaction_add listener

class EntriesView(ui.View):
    """Persistent view for displaying giveaway entries with pagination."""

    def __init__(self, message_id: str, db_manager):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.db = db_manager
        self.current_page = 0

    @ui.button(label="📊 Entries", style=ButtonStyle.gray, custom_id="entries_button")
    async def entries_button(self, interaction: discord.Interaction, button: ui.Button):
        """Show entries for this giveaway."""
        await self.show_entries(interaction)

    async def show_entries_followup(self, interaction: discord.Interaction, page: int = 0):
        """Display the entries embed with pagination (for already deferred interactions)."""
        try:
            # Don't defer - interaction already deferred
            await self._show_entries_internal(interaction, page, use_followup=True)
        except Exception as e:
            logging.error(f"Error showing entries: {e}")
            await interaction.followup.send("An error occurred while loading entries.", ephemeral=True)

    async def show_entries(self, interaction: discord.Interaction, page: int = 0):
        """Display the entries embed with pagination."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self._show_entries_internal(interaction, page, use_followup=True)
        except Exception as e:
            logging.error(f"Error showing entries: {e}")
            await interaction.followup.send("An error occurred while loading entries.", ephemeral=True)

    async def _show_entries_internal(self, interaction: discord.Interaction, page: int = 0, use_followup: bool = True):
        """Internal method to display entries."""
        try:

            giveaway = await self.db.giveaways.find_one({"message_id": self.message_id})
            if not giveaway:
                await interaction.followup.send("Giveaway not found!", ephemeral=True)
                return

            # Ensure we have the prize name
            prize_name = giveaway.get('prize', 'Unknown')

            # Get all participants from the database (includes both real and fake)
            participants = await self.db.participants.find({"message_id": self.message_id}).to_list(length=None)

            # Track unique user IDs to prevent duplicates
            unique_user_ids = set()
            all_participants = []
            bot_id = str(interaction.client.user.id) if interaction.client and interaction.client.user else "0"

            # Add all participants (excluding bot)
            # Fake participants are already in the participants table with is_fake=1
            if participants:
                for p in participants:
                    user_id = p['user_id']
                    # Skip bot and already added users
                    if user_id != bot_id and user_id not in unique_user_ids:
                        unique_user_ids.add(user_id)
                        all_participants.append({
                            'id': user_id,
                            'type': 'fake' if p.get('is_fake', 0) == 1 else 'real'
                        })

            total = len(all_participants)
            if total == 0:
                embed = discord.Embed(
                    title="📊 Giveaway Entries",
                    description="No participants found for this giveaway.",
                    color=EMBED_COLOR
                )
                embed.add_field(name="Prize", value=prize_name, inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            total_pages = max(1, (total + ENTRIES_PER_PAGE - 1) // ENTRIES_PER_PAGE)
            page = max(0, min(page, total_pages - 1))
            start = page * ENTRIES_PER_PAGE
            end = min(start + ENTRIES_PER_PAGE, total)
            slice_participants = all_participants[start:end]

            embed = discord.Embed(title="📊 Giveaway Entries", color=EMBED_COLOR)
            embed.add_field(name="Prize", value=prize_name, inline=False)
            
            text = ""
            for idx, part in enumerate(slice_participants, start=start + 1):
                uid = part['id']
                # Use helper function to parse fake IDs
                display = parse_fake_participant_id(uid) if is_fake_participant(uid) else uid
                if not display:
                    display = uid  # Fallback to original if parsing fails
                try:
                    user = interaction.client.get_user(int(display))
                    if user:
                        text += f"`{idx:3d}.` **{user.display_name}** (@{user.name})\n"
                    else:
                        text += f"`{idx:3d}.` User ID: {display}\n"
                except Exception:
                    text += f"`{idx:3d}.` User ID: {display}\n"

            embed.description = text
            embed.set_footer(text=f"Page {page+1} of {total_pages} | {total} total entries")

            view = EntriesPaginationView(self.message_id, self.db, page, total_pages)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"Error showing entries: {e}")
            await interaction.followup.send("An error occurred while loading entries.", ephemeral=True)

class EntriesPaginationView(ui.View):
    """View for paginating through entries."""

    def __init__(self, message_id: str, db_manager, current_page: int, total_pages: int):
        super().__init__(timeout=300)
        self.message_id = message_id
        self.db = db_manager
        self.current_page = current_page
        self.total_pages = total_pages

        is_single = total_pages <= 1
        first_last_disabled = is_single
        prev_disabled = is_single or current_page == 0
        next_disabled = is_single or current_page == total_pages - 1

        self.first_page.disabled = first_last_disabled
        self.previous_page.disabled = prev_disabled
        self.next_page.disabled = next_disabled
        self.last_page.disabled = first_last_disabled
    
    async def on_timeout(self):
        """Called when the view times out."""
        try:
            for item in self.children:
                item.disabled = True
        except Exception:
            pass

    @ui.button(label="⏪", style=ButtonStyle.gray)
    async def first_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, 0)

    @ui.button(label="◀️", style=ButtonStyle.gray)
    async def previous_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, max(0, self.current_page - 1))

    @ui.button(label="▶️", style=ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, min(self.total_pages - 1, self.current_page + 1))

    @ui.button(label="⏩", style=ButtonStyle.gray)
    async def last_page(self, interaction: discord.Interaction, button: ui.Button):
        await EntriesView(self.message_id, self.db).show_entries(interaction, self.total_pages - 1)

class GiveawayEndedView(ui.View):
    """Persistent view for ended giveaways with participant count button."""

    def __init__(self, participant_count: int, message_id: str, db_manager, bot):
        super().__init__(timeout=None)
        self.participant_count = participant_count
        self.message_id = message_id
        self.db = db_manager
        self.bot = bot
        
        # Set the correct label for entries button
        self.entries_button.label = f"{participant_count} entries"
    
    @ui.button(emoji=REACTION_EMOJI, style=ButtonStyle.gray, custom_id="giveaway_ended_join", row=0)
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        """Show message that giveaway has ended."""
        try:
            await interaction.response.send_message(
                "<:ogs_bell:1427918360401940552> This giveaway has ended. You can no longer join.",
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Error in ended giveaway join button: {e}")
    
    @ui.button(label="0 entries", style=ButtonStyle.gray, custom_id="giveaway_ended_entries", row=0)
    async def entries_button(self, interaction: discord.Interaction, button: ui.Button):
        """Show entries list for ended giveaway."""
        try:
            # Defer immediately before database operations
            await interaction.response.defer(ephemeral=True)
            view = EntriesView(self.message_id, self.db)
            await view.show_entries_followup(interaction)
        except Exception as e:
            logging.error(f"Error in GiveawayEndedView entries button: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("An error occurred.", ephemeral=True)
                else:
                    await interaction.followup.send("An error occurred.", ephemeral=True)
            except Exception:
                pass

class GiveawayEditView(ui.View):
    """Interactive view for giveaway management."""
    
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot
    
    async def on_timeout(self):
        """Called when the view times out."""
        try:
            for item in self.children:
                item.disabled = True
        except Exception:
            pass
    
    @ui.select(
        placeholder="Choose an action...",
        options=[
            discord.SelectOption(label="Stats", description="View giveaway statistics", emoji="📊", value="stats"),
            discord.SelectOption(label="Fill", description="Fill giveaway with fake reactions", emoji="📈", value="fill"),
            discord.SelectOption(label="Force Winner", description="Force specific users to win", emoji="👑", value="force"),
            discord.SelectOption(label="Extend", description="Extend giveaway duration", emoji="⏰", value="extend"),
            discord.SelectOption(label="Cancel", description="Cancel an active giveaway", emoji="❌", value="cancel"),
        ]
    )
    async def select_action(self, interaction: discord.Interaction, select: ui.Select):
        action = select.values[0]
        
        if action == "stats":
            # Show stats directly
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if giveaway_cog:
                await giveaway_cog.giveaway_stats(interaction)
            else:
                await interaction.response.send_message("Giveaway system not available!", ephemeral=True)
        
        elif action == "fill":
            modal = FillModal(self.bot)
            await interaction.response.send_modal(modal)
        
        elif action == "force":
            modal = ForceWinnerModal(self.bot)
            await interaction.response.send_modal(modal)
        
        elif action == "extend":
            modal = ExtendModal(self.bot)
            await interaction.response.send_modal(modal)
        
        elif action == "cancel":
            modal = CancelModal(self.bot)
            await interaction.response.send_modal(modal)

class FillModal(ui.Modal, title="Fill Giveaway"):
    message_id = ui.TextInput(label="Message ID", placeholder="Enter the giveaway message ID", required=True)
    total_reactions = ui.TextInput(label="Total Fake Reactions", placeholder="Number of fake reactions (10-1000)", required=True)
    duration = ui.TextInput(label="Duration (minutes)", placeholder="Duration in minutes (1-1440)", required=True)
    
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
    
    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = self.bot.get_cog("GiveawayAdminCog")
        if admin_cog:
            try:
                await admin_cog.fill(
                    interaction,
                    self.message_id.value,
                    int(self.total_reactions.value),
                    int(self.duration.value)
                )
            except ValueError as e:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Invalid input: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Invalid input: {e}", ephemeral=True)
            except Exception as e:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("Admin cog not loaded!", ephemeral=True)
            else:
                await interaction.followup.send("Admin cog not loaded!", ephemeral=True)

class ForceWinnerModal(ui.Modal, title="Force Winner"):
    message_id = ui.TextInput(label="Message ID", placeholder="Enter the giveaway message ID", required=True)
    users = ui.TextInput(
        label="Users",
        placeholder="Mention users or enter IDs (comma separated)",
        style=discord.TextStyle.paragraph,
        required=True
    )
    
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
    
    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = self.bot.get_cog("GiveawayAdminCog")
        if admin_cog:
            try:
                await admin_cog.force_winner_cmd(interaction, self.message_id.value, self.users.value)
            except Exception as e:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("Admin cog not loaded!", ephemeral=True)
            else:
                await interaction.followup.send("Admin cog not loaded!", ephemeral=True)

class ExtendModal(ui.Modal, title="Extend Giveaway"):
    message_id = ui.TextInput(label="Message ID", placeholder="Enter the giveaway message ID", required=True)
    additional_time = ui.TextInput(
        label="Additional Time",
        placeholder="e.g., 1h, 30m, 1h30m, 2d",
        required=True
    )
    
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
    
    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = self.bot.get_cog("GiveawayAdminCog")
        if admin_cog:
            try:
                await admin_cog.extend(interaction, self.message_id.value, self.additional_time.value)
            except Exception as e:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("Admin cog not loaded!", ephemeral=True)
            else:
                await interaction.followup.send("Admin cog not loaded!", ephemeral=True)

class CancelModal(ui.Modal, title="Cancel Giveaway"):
    message_id = ui.TextInput(label="Message ID", placeholder="Enter the giveaway message ID", required=True)
    reason = ui.TextInput(
        label="Reason",
        placeholder="Reason for cancellation",
        style=discord.TextStyle.paragraph,
        required=False,
        default="Cancelled by administrator"
    )
    
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
    
    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = self.bot.get_cog("GiveawayAdminCog")
        if admin_cog:
            try:
                reason = self.reason.value if self.reason.value else "Cancelled by administrator"
                await admin_cog.cancel(interaction, self.message_id.value, reason)
            except Exception as e:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("Admin cog not loaded!", ephemeral=True)
            else:
                await interaction.followup.send("Admin cog not loaded!", ephemeral=True)

class DatabaseManager:
    """Manages MongoDB interactions."""

    def __init__(self, mongo_url: str):
        self.mongo_url = mongo_url
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None
        self.connected = False
        
        # Collections
        self.giveaways = None
        self.participants = None
        self.fake_reactions = None
        self.giveaway_stats = None
        self.giveaway_history = None  # Historical tracking

    async def init(self):
        try:
            # Optimized for very large servers (10k+ members)
            self.client = AsyncIOMotorClient(
                self.mongo_url,
                serverSelectionTimeoutMS=30000,  # Increased from 5s to 30s
                connectTimeoutMS=30000,  # Increased from 10s to 30s
                socketTimeoutMS=120000,  # Increased from 45s to 120s
                maxPoolSize=200,  # Increased for high concurrency
                minPoolSize=20,   # Keep connections warm
                maxIdleTimeMS=300000,  # Increased from 45s to 5 minutes
                retryWrites=True,
                retryReads=True,
                w=1,  # Write concern: acknowledge from primary only (faster)
                readPreference='primaryPreferred',  # Read from primary when available
                compressors='snappy,zlib',  # Compress network traffic
                zlibCompressionLevel=6,
                heartbeatFrequencyMS=30000  # Check connection every 30s
            )
            self.db = self.client["giveaways"]
            self.giveaways = self.db["giveaways"]
            self.participants = self.db["participants"]
            self.fake_reactions = self.db["fake_reactions"]
            self.giveaway_stats = self.db["giveaway_stats"]
            self.giveaway_history = self.db["giveaway_history"]
            self.connected = True
            await self._create_indexes()
            logging.info("Giveaway MongoDB connection initialized")
        except Exception as e:
            logging.error(f"Error initializing MongoDB: {e}")
            self.connected = False

    async def _create_indexes(self):
        """Create MongoDB indexes for better query performance."""
        try:
            # Giveaways collection indexes
            await self.giveaways.create_index("message_id", unique=True, background=True)
            await self.giveaways.create_index("status", background=True)
            await self.giveaways.create_index("end_time", background=True)
            await self.giveaways.create_index("guild_id", background=True)
            await self.giveaways.create_index(
                [("status", ASCENDING), ("end_time", ASCENDING)],
                background=True
            )
            
            # Participants collection indexes
            await self.participants.create_index(
                [("message_id", ASCENDING), ("user_id", ASCENDING)],
                unique=True,
                background=True
            )
            await self.participants.create_index("message_id", background=True)
            await self.participants.create_index("user_id", background=True)
            
            # Fake reactions collection indexes
            await self.fake_reactions.create_index("message_id", unique=True, background=True)
            await self.fake_reactions.create_index("status", background=True)
            
            # Stats collection indexes
            await self.giveaway_stats.create_index("guild_id", unique=True, background=True)
            
            # History collection indexes
            await self.giveaway_history.create_index("guild_id", background=True)
            await self.giveaway_history.create_index("timestamp", background=True)
            await self.giveaway_history.create_index(
                [("guild_id", ASCENDING), ("timestamp", ASCENDING)],
                background=True
            )
            
            logging.info("Giveaway indexes created successfully")
        except Exception as e:
            logging.error(f"Error creating giveaway indexes: {e}")

    async def close(self):
        if self.client:
            self.client.close()
            self.client = None
            self.db = None
            self.connected = False

class GiveawayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # Load configuration
        self.config = GiveawayConfig.from_env()
        
        # Get MongoDB URL from environment
        mongo_url = os.getenv('MONGO_URL')
        if not mongo_url:
            raise ValueError("MONGO_URL is not set in the environment variables.")

        # Logging: file only
        os.makedirs('logs', exist_ok=True)
        log_file = os.path.join('logs', 'giveaway_bot.log')
        self.logger = logging.getLogger('GiveawayBot')
        self.logger.handlers.clear()

        file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(file_handler)

        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)  # Changed from ERROR to INFO to log all messages

        self.db = DatabaseManager(mongo_url)
        self._ready = asyncio.Event()
        self._checking_lock = asyncio.Lock()
        self.timezone = os.getenv('BOT_TIMEZONE', 'UTC')
        self.active_fake_reaction_tasks: Dict[str, asyncio.Task] = {}
        
        # Performance optimizations for very large servers
        self._participant_cache: Dict[str, int] = {}  # message_id -> count cache
        self._cache_lock = asyncio.Lock()
        self._user_cooldowns: Dict[str, float] = {}  # user_id -> last_action_time
        self._cooldown_duration = 2.0  # seconds between actions per user

    @discord.app_commands.command(name="giveaway-edit", description="Edit and manage giveaways")
    @discord.app_commands.default_permissions(administrator=True)
    async def giveaway_edit_cmd(self, interaction: discord.Interaction):
        """Show interactive menu for giveaway management."""
        embed = discord.Embed(
            title=f"{REACTION_EMOJI} Giveaway Management",
            description="Select an action from the menu below:",
            color=EMBED_COLOR
        )
        embed.add_field(
            name="📊 Stats",
            value="View giveaway statistics for this server",
            inline=False
        )
        embed.add_field(
            name="📈 Fill",
            value="Gradually fill a giveaway with fake reactions",
            inline=False
        )
        embed.add_field(
            name="👑 Force Winner",
            value="Force specific users to win a giveaway",
            inline=False
        )
        embed.add_field(
            name="⏰ Extend",
            value="Extend or modify the duration of an active giveaway",
            inline=False
        )
        embed.add_field(
            name="❌ Cancel",
            value="Cancel an active giveaway",
            inline=False
        )
        
        view = GiveawayEditView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def cog_load(self):
        await self.db.init()
        self.check_giveaways.start()
        self._ready.set()
        asyncio.create_task(self.register_persistent_views())
        
        # The command group is automatically registered when the cog loads
        self.logger.info("[OK] GiveawayCog loaded with reaction-based giveaway system")

    async def register_persistent_views(self):
        """Register persistent views for ended giveaways on bot restart."""
        if not self.db.connected:
            return
        try:
            # Register views for ended giveaways only (active giveaways use reactions)
            ended = await self.db.giveaways.find({"status": "ended"}).to_list(length=None)
            for gw in ended:
                mid = gw['message_id']
                parts = await self.db.participants.find({"message_id": mid}).to_list(length=None)
                bot_id = str(self.bot.user.id)
                real = sum(1 for p in parts if p['user_id'] != bot_id and p.get('is_fake', 0) == 0)
                fake = sum(1 for p in parts if p.get('is_fake', 0) == 1)
                total = real + fake
                view = GiveawayEndedView(total, mid, self.db, self.bot)
                self.bot.add_view(view, message_id=int(mid))

        except Exception as e:
            self.logger.error(f"Error registering views: {e}")

    def cog_unload(self):
        self.check_giveaways.cancel()
        # Cancel all active fake reaction tasks
        for task in self.active_fake_reaction_tasks.values():
            if not task.done():
                task.cancel()
        self.active_fake_reaction_tasks.clear()
        asyncio.create_task(self.db.close())
        self.logger.info("[INFO] GiveawayCog unloaded")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction adds for giveaway participation."""
        # Ignore bot reactions
        if payload.user_id == self.bot.user.id:
            return
        
        # Check if reaction is the giveaway emoji
        reaction_str = str(payload.emoji)
        if reaction_str != REACTION_EMOJI:
            return
        
        if not self.db.connected:
            return
        
        try:
            # Check if this message is an active giveaway
            gw = await self.db.giveaways.find_one({
                "message_id": str(payload.message_id),
                "status": "active"
            })
            
            if not gw:
                return
            
            # Add user to participants
            user_id = str(payload.user_id)
            result = await self.db.participants.update_one(
                {
                    "message_id": str(payload.message_id),
                    "user_id": user_id
                },
                {
                    "$setOnInsert": {
                        "message_id": str(payload.message_id),
                        "user_id": user_id,
                        "joined_at": get_current_utc_timestamp(),
                        "is_forced": 0,
                        "is_fake": 0,
                        "original_user_id": None
                    }
                },
                upsert=True
            )
            
            # Update cache if new participant
            if result.upserted_id:
                async with self._cache_lock:
                    current = self._participant_cache.get(str(payload.message_id), 0)
                    self._participant_cache[str(payload.message_id)] = current + 1
                    
        except Exception as e:
            self.logger.error(f"Error handling reaction add: {e}")
    
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle reaction removes for giveaway participation."""
        # Ignore bot reactions
        if payload.user_id == self.bot.user.id:
            return
        
        # Check if reaction is the giveaway emoji
        reaction_str = str(payload.emoji)
        if reaction_str != REACTION_EMOJI:
            return
        
        if not self.db.connected:
            return
        
        try:
            # Check if this message is an active giveaway
            gw = await self.db.giveaways.find_one({
                "message_id": str(payload.message_id),
                "status": "active"
            })
            
            if not gw:
                return
            
            # Remove user from participants
            user_id = str(payload.user_id)
            result = await self.db.participants.delete_one({
                "message_id": str(payload.message_id),
                "user_id": user_id
            })
            
            # Update cache if participant was removed
            if result.deleted_count > 0:
                async with self._cache_lock:
                    current = self._participant_cache.get(str(payload.message_id), 0)
                    self._participant_cache[str(payload.message_id)] = max(0, current - 1)
                    
        except Exception as e:
            self.logger.error(f"Error handling reaction remove: {e}")

    async def check_bot_permissions(self, channel):
        if not channel or not hasattr(channel, 'guild') or not channel.guild or not hasattr(channel, 'permissions_for'):
            return False
        if not channel.guild.me:
            return False
        perms = channel.permissions_for(channel.guild.me)
        needed = {'send_messages', 'embed_links', 'read_message_history'}
        return all(getattr(perms, p, False) for p in needed)
    
    async def _update_guild_stats(self, guild_id: int, giveaways_delta: int = 0, 
                                  participants_delta: int = 0, winners_delta: int = 0) -> None:
        """Update guild statistics."""
        try:
            await self.db.giveaway_stats.update_one(
                {"guild_id": guild_id},
                {
                    "$inc": {
                        "total_giveaways": giveaways_delta,
                        "total_participants": participants_delta,
                        "total_winners": winners_delta
                    },
                    "$set": {"last_giveaway": get_current_utc_timestamp()}
                },
                upsert=True
            )
        except Exception as e:
            self.logger.error(f"Error updating guild stats: {e}")
    
    async def _record_giveaway_history(self, guild_id: int, event_type: str, 
                                       giveaway_data: dict) -> None:
        """Record giveaway event to history for trend analysis.
        
        Args:
            guild_id: The guild ID
            event_type: Type of event (started, ended, cancelled)
            giveaway_data: Data about the giveaway
        """
        try:
            history_entry = {
                "guild_id": guild_id,
                "timestamp": get_current_utc_timestamp(),
                "event_type": event_type,
                "message_id": giveaway_data.get("message_id"),
                "prize": giveaway_data.get("prize"),
                "participants": giveaway_data.get("participants", 0),
                "winners_count": giveaway_data.get("winners_count", 0),
                "actual_winners": giveaway_data.get("actual_winners", 0),
                "duration_seconds": giveaway_data.get("duration_seconds", 0)
            }
            await self.db.giveaway_history.insert_one(history_entry)
        except Exception as e:
            self.logger.error(f"Error recording giveaway history: {e}")
    
    async def _verify_winner(self, guild: discord.Guild, user_id: str) -> Tuple[bool, Optional[discord.Member]]:
        """Verify if a winner is still valid (in server, not banned)."""
        if not self.config.enable_winner_verification:
            return True, None
        
        try:
            # Extract actual user ID (handle fake IDs using helper function)
            if is_fake_participant(user_id):
                actual_id_str = parse_fake_participant_id(user_id)
                if not actual_id_str:
                    return False, None
                actual_id = int(actual_id_str)
            else:
                actual_id = int(user_id)
            
            member = guild.get_member(actual_id)
            
            if not member:
                # Try fetching
                try:
                    member = await guild.fetch_member(actual_id)
                except (discord.NotFound, discord.HTTPException):
                    return False, None
            
            return True, member
        except Exception as e:
            self.logger.error(f"Error verifying winner {user_id}: {e}")
            return False, None
    

    @tasks.loop(seconds=CLEANUP_INTERVAL)
    async def check_giveaways(self):
        await self._ready.wait()
        if not self.db.connected:
            return
        async with self._checking_lock:
            try:
                now = get_current_utc_timestamp()
                act = await self.db.giveaways.find({
                    "end_time": {"$lte": now},
                    "status": "active"
                }).to_list(length=None)
                for row in act:
                    try:
                        await self.end_giveaway(row['message_id'])
                    except Exception as gw_error:
                        self.logger.error(f"Error ending giveaway {row.get('message_id', 'unknown')}: {gw_error}")
            except Exception as e:
                self.logger.error(f"Error in check_giveaways: {e}")

    @discord.app_commands.command(name="giveaway-start", description="Start a new giveaway")
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def start_giveaway(
        self,
        interaction: discord.Interaction,
        duration: str,
        winners: int,
        prize: str
    ):
        try:
            await interaction.response.defer(ephemeral=True)
            if not interaction.channel or not isinstance(interaction.channel, TextChannel):
                return await interaction.followup.send("Giveaways can only be started in text channels.", ephemeral=True)
            if not await self.check_bot_permissions(interaction.channel):
                return await interaction.followup.send("I need proper permissions.", ephemeral=True)
            if not self.config.min_winners <= winners <= self.config.max_winners:
                raise ValueError(f"Winners must be between {self.config.min_winners} and {self.config.max_winners}.")

            import re
            pattern = r'(\d+)([smhdw])'
            matches = re.findall(pattern, duration.lower())
            if not matches:
                raise ValueError("Use formats like: 30s, 1h, 1h30m, 2d5h30m, 1w")
            secs = sum(int(n)*DURATION_UNITS[u] for n, u in matches)
            if not self.config.min_duration <= secs <= self.config.max_duration:
                raise ValueError(f"Duration must be between {self.config.min_duration}s and {self.config.max_duration}s.")

            end_ts = get_current_utc_timestamp() + secs
            await interaction.channel.send("**<:sukoon_taaada:1324071825910792223> GIVEAWAY <:sukoon_taaada:1324071825910792223>**")

            def fmt_dur(sec):
                parts = []
                for unit_sec, label in [(86400,'d'),(3600,'h'),(60,'m')]:
                    if sec >= unit_sec:
                        cnt, sec = divmod(sec, unit_sec)
                        parts.append(f"{cnt}{label}")
                if sec:
                    parts.append(f"{sec}s")
                return " ".join(parts) or "0s"
            dur_disp = fmt_dur(secs)
            icon = None
            if interaction.guild and interaction.guild.icon:
                icon = interaction.guild.icon.url
            if not icon and FOOTER_ICON_URL:
                icon = FOOTER_ICON_URL

            embed = discord.Embed(
                title=f"{GIFT_EMOJI} {prize}",
                description=(
                    f">>> {WINNER_EMOJI} **Winner:** {winners}\n"
                    f"{TIME_EMOJI} **Ends:** <t:{end_ts}:R>\n"
                    f"{PRIZE_EMOJI} **Hosted by:** {interaction.user.mention}"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(end_ts, timezone.utc)
            )
            thumbnail_url = get_thumbnail_url(interaction.guild)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            
            if interaction.guild and interaction.guild.icon:
                embed.set_footer(text="made with ♡", icon_url=str(interaction.guild.icon))

            # Send message without buttons (reaction-only system)
            msg = await interaction.channel.send(embed=embed)
            
            # Add reaction for users to join
            try:
                await msg.add_reaction(REACTION_EMOJI)
            except Exception as e:
                self.logger.error(f"Failed to add reaction: {e}")

            if self.db.connected:
                await self.db.giveaways.insert_one({
                    "message_id": str(msg.id),
                    "channel_id": interaction.channel.id,
                    "guild_id": interaction.guild.id,
                    "end_time": end_ts,
                    "winners_count": winners,
                    "prize": prize,
                    "status": "active",
                    "host_id": interaction.user.id,
                    "created_at": get_current_utc_timestamp(),
                    "winner_ids": [],
                    "forced_winner_ids": []
                })
                
                # Initialize cache for new giveaway
                async with self._cache_lock:
                    self._participant_cache[str(msg.id)] = 0
                
                # Update statistics
                if self.config.enable_statistics:
                    await self._update_guild_stats(interaction.guild.id, giveaways_delta=1)
                    # Record to history
                    await self._record_giveaway_history(
                        interaction.guild.id,
                        "started",
                        {
                            "message_id": str(msg.id),
                            "prize": prize,
                            "winners_count": winners,
                            "duration_seconds": secs
                        }
                    )
                
                await interaction.followup.send("Giveaway started!", ephemeral=True)
            else:
                await interaction.followup.send("Giveaway started! (No database)", ephemeral=True)

        except ValueError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
        except Exception as e:
            self.logger.error(f"Error starting giveaway: {e}")
            await interaction.followup.send("Unexpected error.", ephemeral=True)

    async def end_giveaway(self, message_id: str):
        try:
            gw = await self.db.giveaways.find_one({
                "message_id": message_id,
                "status": "active"
            })
            if not gw:
                return

            chan = self.bot.get_channel(gw['channel_id'])
            if not chan:
                await self.db.giveaways.update_one(
                    {"message_id": message_id},
                    {"$set": {"status": "error", "error": "Channel not found"}}
                )
                return
            
            try:
                msg = await chan.fetch_message(int(message_id))
            except discord.NotFound:
                await self.db.giveaways.update_one(
                    {"message_id": message_id},
                    {"$set": {"status": "error", "error": "Message not found"}}
                )
                return
            except Exception as e:
                await self.db.giveaways.update_one(
                    {"message_id": message_id},
                    {"$set": {"status": "error", "error": f"Failed to fetch message: {str(e)}"}}
                )
                return
            
            if not await self.check_bot_permissions(chan):
                await self.db.giveaways.update_one(
                    {"message_id": message_id},
                    {"$set": {"status": "error", "error": "Missing permissions"}}
                )
                return

            # Get all participants (excluding bot) - includes both real and fake
            parts = await self.db.participants.find({"message_id": message_id}).to_list(length=None)
            bot_id = str(self.bot.user.id)
            # Include both real and fake participants in the winner pool
            valid = [p['user_id'] for p in parts if p['user_id'] != bot_id]
            
            # Count fake entries that were successfully added
            fake_count = sum(1 for p in parts if p.get('is_fake', 0) == 1)

            # Calculate total participants (real + fake)
            total_participants = len(valid)

            forced = gw.get('forced_winner_ids', [])
            winners = []
            verified_winners = []
            
            # Select winners and verify them
            if forced:
                # Forced winners are ALWAYS included, no verification needed
                verified_winners = forced[:]
                # Calculate how many more winners we need
                remaining_slots = gw['winners_count'] - len(forced)
                if remaining_slots > 0 and valid:
                    # Get participants who aren't already forced winners
                    remaining = [u for u in valid if u not in forced]
                    if remaining:
                        # Select additional winners from remaining participants
                        additional = random.sample(remaining, min(len(remaining), remaining_slots))
                        # Verify the additional (non-forced) winners
                        for winner_id in additional:
                            is_valid, member = await self._verify_winner(chan.guild, winner_id)
                            if is_valid:
                                verified_winners.append(winner_id)
            elif valid:
                # No forced winners, select randomly from valid participants
                winners = random.sample(valid, min(len(valid), gw['winners_count']))
                # Verify winners (no DM sending)
                for winner_id in winners:
                    is_valid, member = await self._verify_winner(chan.guild, winner_id)
                    if is_valid:
                        verified_winners.append(winner_id)
                
                # If we still need more winners, try to reroll
                if len(verified_winners) < gw['winners_count']:
                    remaining = [u for u in valid if u not in verified_winners]
                    needed = min(len(remaining), gw['winners_count'] - len(verified_winners))
                    if needed > 0:
                        extra = random.sample(remaining, needed)
                        for winner_id in extra:
                            is_valid, member = await self._verify_winner(chan.guild, winner_id)
                            if is_valid:
                                verified_winners.append(winner_id)

            # Generate winner mentions using helper function
            mentions = []
            for w in verified_winners:
                if is_fake_participant(w):
                    original_id = parse_fake_participant_id(w)
                    if original_id:
                        mentions.append(f"<@{original_id}>")
                else:
                    mentions.append(f"<@{w}>")
            if not mentions:
                mentions = ["No winners."]
            now_ts = get_current_utc_timestamp()
            icon = None
            if chan.guild and chan.guild.icon:
                icon = chan.guild.icon.url
            if not icon and FOOTER_ICON_URL:
                icon = FOOTER_ICON_URL

            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed = discord.Embed(
                title=f"{GIFT_EMOJI} {prize_name}",
                description=(
                    f">>> {WINNER_EMOJI} **Winner:** {', '.join(mentions)}\n"
                    f"{TIME_EMOJI} **Ends:** <t:{now_ts}:R>\n"
                    f"{PRIZE_EMOJI} **Hosted by:** <@{gw['host_id']}>"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            thumbnail_url = get_thumbnail_url(chan.guild)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            
            if chan.guild and chan.guild.icon:
                embed.set_footer(text="made with ♡", icon_url=str(chan.guild.icon))

            # Cancel any active fake reaction task
            admin_cog = self.bot.get_cog("GiveawayAdminCog")
            if admin_cog and message_id in admin_cog.active_fake_reaction_tasks:
                admin_cog.active_fake_reaction_tasks[message_id].cancel()
                try:
                    await self.db.fake_reactions.update_one(
                        {"message_id": message_id},
                        {"$set": {
                            "status": "cancelled",
                            "cancelled_at": get_current_utc_timestamp()
                        }}
                    )
                except Exception as db_error:
                    self.logger.error(f"Error updating fake reaction status: {db_error}")

            view = GiveawayEndedView(total_participants, message_id, self.db, self.bot)
            
            await msg.edit(embed=embed, view=view)
            
            # Remove all reactions from the message
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                # If bot doesn't have permission to clear all reactions, try removing just the giveaway emoji
                try:
                    await msg.clear_reaction(REACTION_EMOJI)
                except Exception as reaction_error:
                    self.logger.error(f"Failed to remove reactions: {reaction_error}")
            except Exception as reaction_error:
                self.logger.error(f"Failed to clear reactions: {reaction_error}")
            
            # Update database first before sending reply
            try:
                await self.db.giveaways.update_one(
                    {"message_id": message_id},
                    {"$set": {
                        "status": "ended",
                        "winner_ids": verified_winners,
                        "ended_at": now_ts
                    }}
                )
            except Exception as db_error:
                self.logger.error(f"Error updating giveaway status: {db_error}")
            
            # Update statistics
            if self.config.enable_statistics:
                try:
                    await self._update_guild_stats(
                        chan.guild.id,
                        participants_delta=total_participants,
                        winners_delta=len(verified_winners)
                    )
                    # Record to history
                    duration = now_ts - gw.get("created_at", now_ts)
                    await self._record_giveaway_history(
                        chan.guild.id,
                        "ended",
                        {
                            "message_id": message_id,
                            "prize": gw.get("prize", "Unknown"),
                            "participants": total_participants,
                            "winners_count": gw["winners_count"],
                            "actual_winners": len(verified_winners),
                            "duration_seconds": duration
                        }
                    )
                except Exception as stats_error:
                    self.logger.error(f"Error updating stats: {stats_error}")
            
            # Send winner announcement
            if verified_winners:
                try:
                    await msg.reply(f"{REACTION_EMOJI} Congratulations {', '.join(mentions)}! You won **{gw['prize']}**!")
                except Exception as reply_error:
                    self.logger.error(f"Error sending winner announcement: {reply_error}")

        except Exception as e:
            self.logger.error(f"Error ending giveaway {message_id}: {e}")
            await self.db.giveaways.update_one(
                {"message_id": message_id},
                {"$set": {"status": "error", "error": str(e)}}
            )
        finally:
            # Clean up cache entry for ended giveaway (single cleanup point)
            async with self._cache_lock:
                self._participant_cache.pop(message_id, None)


    @commands.command(name="reroll")
    @commands.has_permissions(manage_guild=True)
    async def reroll_giveaway(self, ctx):
        if not self.db.connected:
            return await ctx.send("DB not connected.")
        if not ctx.message.reference:
            return await ctx.send("Reply to a giveaway message to reroll.")
        try:
            orig = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            gw = await self.db.giveaways.find_one({"message_id": str(orig.id)})
            if not gw:
                return await ctx.send("Giveaway not found.")

            if gw['status'] == 'active':
                await self.end_giveaway(str(orig.id))

            # Get all participants (excluding bot) - includes both real and fake
            parts = await self.db.participants.find({"message_id": str(orig.id)}).to_list(length=None)
            bot_id = str(self.bot.user.id)
            # Include both real and fake participants in the reroll pool
            valid = [p['user_id'] for p in parts if p['user_id'] != bot_id]
            
            # Count fake entries that were successfully added
            fake_count = sum(1 for p in parts if p.get('is_fake', 0) == 1)

            # Calculate total participants (real + fake)
            total_participants = len(valid)

            prev = gw.get('winner_ids', [])
            remaining = [u for u in valid if u not in prev]
            if not remaining:
                return await ctx.send("No participants left for reroll.")

            new = random.sample(remaining, min(len(remaining), gw['winners_count']))
            # Generate mentions properly handling fake participants
            mentions = []
            for w in new:
                if is_fake_participant(w):
                    original_id = parse_fake_participant_id(w)
                    if original_id:
                        mentions.append(f"<@{original_id}>")
                else:
                    mentions.append(f"<@{w}>")
            now_ts = get_current_utc_timestamp()
            icon = None
            if ctx.guild and hasattr(ctx.guild, 'icon') and ctx.guild.icon:
                icon = ctx.guild.icon.url

            # Make sure we use the correct prize name from the giveaway data
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed = discord.Embed(
                title=f"{GIFT_EMOJI} {prize_name}",
                description=(
                    f">>> {WINNER_EMOJI} **Winner:** {', '.join(mentions)}\n"
                    f"{TIME_EMOJI} **Ends:** <t:{now_ts}:R>\n"
                    f"{PRIZE_EMOJI} **Hosted by:** <@{gw['host_id']}>"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            thumbnail_url = get_thumbnail_url(ctx.guild)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            
            if ctx.guild and ctx.guild.icon:
                embed.set_footer(text="made with ♡", icon_url=str(ctx.guild.icon))

            view = GiveawayEndedView(total_participants, str(orig.id), self.db, self.bot)
            await orig.edit(embed=embed, view=view)
            
            # Remove all reactions from the message
            try:
                await orig.clear_reactions()
            except discord.Forbidden:
                # If bot doesn't have permission to clear all reactions, try removing just the giveaway emoji
                try:
                    await orig.clear_reaction(REACTION_EMOJI)
                except Exception as reaction_error:
                    self.logger.error(f"Failed to remove reactions: {reaction_error}")
            except Exception as reaction_error:
                self.logger.error(f"Failed to clear reactions: {reaction_error}")

            await self.db.giveaways.update_one(
                {"message_id": str(orig.id)},
                {"$set": {
                    "winner_ids": new,
                    "rerolled_at": now_ts,
                    "rerolled_by": ctx.author.id
                }}
            )
            
            # Send winner announcement as reply to giveaway message
            try:
                await orig.reply(f"{REACTION_EMOJI} Congratulations {', '.join(mentions)}! You won **{gw['prize']}**!")
            except Exception as reply_error:
                # Fallback to channel send if reply fails
                await ctx.send(f"{REACTION_EMOJI} Congratulations {', '.join(mentions)}! You won **{gw['prize']}**!")
        except Exception as e:
            self.logger.error(f"Error rerolling: {e}")
            await ctx.send(f"Error rerolling: {e}")

    async def giveaway_stats(self, interaction: discord.Interaction):
        """Display giveaway statistics for the current guild."""
        try:
            await interaction.response.defer(ephemeral=True)
            
            if not self.config.enable_statistics:
                return await interaction.followup.send(
                    "Statistics tracking is disabled.",
                    ephemeral=True
                )
            
            stats = await self.db.giveaway_stats.find_one({"guild_id": interaction.guild.id})
            
            if not stats:
                return await interaction.followup.send(
                    "No giveaway statistics found for this server.",
                    ephemeral=True
                )
            
            # Get active giveaways count
            active = await self.db.giveaways.find({
                "guild_id": interaction.guild.id,
                "status": "active"
            }).to_list(length=None)
            
            # Get historical data (last 30 days)
            thirty_days_ago = get_current_utc_timestamp() - (30 * 86400)
            history = await self.db.giveaway_history.find({
                "guild_id": interaction.guild.id,
                "timestamp": {"$gte": thirty_days_ago}
            }).to_list(length=None)
            
            # Calculate trends
            ended_count = len([h for h in history if h["event_type"] == "ended"])
            cancelled_count = len([h for h in history if h["event_type"] == "cancelled"])
            avg_participants = sum(h.get("participants", 0) for h in history if h["event_type"] == "ended") / max(ended_count, 1)
            
            embed = discord.Embed(
                title="📊 Giveaway Statistics",
                color=EMBED_COLOR,
                timestamp=get_utc_datetime()
            )
            
            embed.add_field(
                name="📈 All-Time Stats",
                value=(
                    f"**Total Giveaways:** {stats.get('total_giveaways', 0)}\n"
                    f"**Total Participants:** {stats.get('total_participants', 0)}\n"
                    f"**Total Winners:** {stats.get('total_winners', 0)}"
                ),
                inline=False
            )
            
            embed.add_field(
                name="🔥 Current Activity",
                value=f"**Active Giveaways:** {len(active)}",
                inline=True
            )
            
            if stats.get('last_giveaway'):
                embed.add_field(
                    name="⏰ Last Giveaway",
                    value=f"<t:{stats['last_giveaway']}:R>",
                    inline=True
                )
            
            if history:
                embed.add_field(
                    name="📅 Last 30 Days",
                    value=(
                        f"**Completed:** {ended_count}\n"
                        f"**Cancelled:** {cancelled_count}\n"
                        f"**Avg Participants:** {int(avg_participants)}"
                    ),
                    inline=False
                )
            
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            
            embed.set_footer(text="Historical data available for the last 30 days")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            self.logger.error(f"Error displaying stats: {e}")
            await interaction.followup.send(
                f"Error retrieving statistics: {e}",
                ephemeral=True
            )

    @commands.command(name="gstart")
    @commands.has_permissions(administrator=True)
    async def gstart_prefix(self, ctx: commands.Context, time: str, winners: int, *, prize: str):
        """Start a giveaway using prefix command. Admin only.
        
        Usage: .gstart <time> <winners> <prize>
        Example: .gstart 1h 2 Discord Nitro
        
        Time format: 1s/1m/1h/1d (seconds/minutes/hours/days)
        """
        try:
            # Parse time
            time_str = time.lower()
            multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
            
            if not time_str[-1] in multipliers:
                await ctx.send("❌ Invalid time format. Use: 1s, 1m, 1h, or 1d", delete_after=10)
                return
            
            try:
                amount = int(time_str[:-1])
                unit = time_str[-1]
                seconds = amount * multipliers[unit]
            except ValueError:
                await ctx.send("❌ Invalid time format. Use: 1s, 1m, 1h, or 1d", delete_after=10)
                return
            
            # Validate duration
            if seconds < MIN_GIVEAWAY_DURATION:
                await ctx.send(f"❌ Duration must be at least {MIN_GIVEAWAY_DURATION} seconds.", delete_after=10)
                return
            if seconds > MAX_GIVEAWAY_DURATION:
                await ctx.send(f"❌ Duration cannot exceed {MAX_GIVEAWAY_DURATION} seconds.", delete_after=10)
                return
            
            # Validate winners
            if winners < MIN_WINNERS or winners > MAX_WINNERS:
                await ctx.send(f"❌ Winners must be between {MIN_WINNERS} and {MAX_WINNERS}.", delete_after=10)
                return
            
            # Calculate end time
            end_ts = get_current_utc_timestamp() + seconds
            
            # Get server icon
            icon = None
            if ctx.guild and ctx.guild.icon:
                icon = ctx.guild.icon.url
            if not icon and FOOTER_ICON_URL:
                icon = FOOTER_ICON_URL
            
            # Send giveaway announcement
            await ctx.send("**<:sukoon_taaada:1324071825910792223> GIVEAWAY <:sukoon_taaada:1324071825910792223>**")
            
            # Create embed
            winners_text = f"{winners} {'winner' if winners == 1 else 'winners'}"
            embed = discord.Embed(
                title=f"{GIFT_EMOJI} {prize}",
                description=(
                    f">>> {WINNER_EMOJI} **Winner:** {winners_text}\n"
                    f"{TIME_EMOJI} **Ends:** <t:{end_ts}:R>\n"
                    f"{PRIZE_EMOJI} **Hosted by:** {ctx.author.mention}"
                ),
                color=EMBED_COLOR,
                timestamp=datetime.fromtimestamp(end_ts, timezone.utc)
            )
            thumbnail_url = get_thumbnail_url(ctx.guild)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            
            if ctx.guild and ctx.guild.icon:
                embed.set_footer(text="made with ♡", icon_url=str(ctx.guild.icon))

            # Send message without buttons (reaction-only system)
            msg = await ctx.send(embed=embed)
            
            # Add reaction for users to join
            try:
                await msg.add_reaction(REACTION_EMOJI)
            except Exception as e:
                self.logger.error(f"Failed to add reaction: {e}")
            
            # Save to database
            if self.db.connected:
                await self.db.giveaways.insert_one({
                    "message_id": str(msg.id),
                    "channel_id": ctx.channel.id,
                    "guild_id": ctx.guild.id,
                    "end_time": end_ts,
                    "prize": prize,
                    "winners_count": winners,
                    "host_id": str(ctx.author.id),
                    "status": "active",
                    "created_at": get_current_utc_timestamp()
                })
                
                # Initialize cache for new giveaway
                async with self._cache_lock:
                    self._participant_cache[str(msg.id)] = 0
                
                self.logger.info(f"Giveaway started via prefix command by {ctx.author} in {ctx.guild.name}")
            
            # Delete the command message
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass  # Ignore if we can't delete (missing permissions)
            
        except commands.MissingPermissions:
            await ctx.send("❌ You need Administrator permissions to use this command.", delete_after=10)
        except Exception as e:
            self.logger.error(f"Error in gstart prefix command: {e}")
            await ctx.send(f"❌ Error starting giveaway: {e}", delete_after=10)

async def setup(bot):
    await bot.add_cog(GiveawayCog(bot))
