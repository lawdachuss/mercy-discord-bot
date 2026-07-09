import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import re
import random
import importlib
from pymongo import MongoClient, ASCENDING
from pymongo.collection import ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict

# Import configuration
from .config import (
    MUTED_ROLE_NAME, JAIL_CHANNEL_NAME, LOG_CHANNEL_NAME,
    DM_AUTO_DELETE_MINUTES, DM_REASON_MAX_LENGTH,
    PERMISSION_BASE_SLEEP, PERMISSION_LARGE_SERVER_SLEEP, PERMISSION_HUGE_SERVER_SLEEP,
    PERMISSION_MAX_RETRIES, LARGE_SERVER_THRESHOLD, HUGE_SERVER_THRESHOLD,
    JAIL_WELCOME_MESSAGE
)

# Hardcoded Colors - All embeds use dark grey (#2f3136)
class Colors:
    SUCCESS = 0x2f3136
    ERROR = 0x2f3136
    WARNING = 0x2f3136
    INFO = 0x2f3136
    PENDING = 0x2f3136
    MUTE = 0x2f3136
    UNMUTE = 0x2f3136

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing from environment (.env)")

# Connect to MongoDB with proper connection pooling
mongo = MongoClient(
    MONGO_URL,
    maxPoolSize=50,
    minPoolSize=10,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000
)
db = mongo.get_database("discord_mute_system")

guild_configs = db.guild_configs
mutes_col = db.mutes
jail_messages = db.jail_messages
guild_counters = db.guild_counters
pending_dm_deletes = db.pending_dm_deletes

# Ensure indexes (safe wrapped)
try:
    guild_configs.create_index([("guild_id", ASCENDING)], unique=True)
    # Compound index for faster mute lookups
    mutes_col.create_index([("guild_id", ASCENDING), ("user_id", ASCENDING)])
    mutes_col.create_index([("guild_id", ASCENDING), ("active", ASCENDING)])
    mutes_col.create_index([("guild_id", ASCENDING), ("case_id", ASCENDING)])
    # Index for auto-unmute queries
    mutes_col.create_index([("active", ASCENDING), ("expires_at", ASCENDING)])
    guild_counters.create_index([("guild_id", ASCENDING)], unique=True)
    # TTL on jail messages for 7 days
    jail_messages.create_index([("created_at", ASCENDING)], expireAfterSeconds=7 * 24 * 3600)
    jail_messages.create_index([("guild_id", ASCENDING), ("user_id", ASCENDING)])
    # TTL for DM delete tasks: expire exactly at expires_at (expireAfterSeconds: 0)
    pending_dm_deletes.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
except Exception:
    # If index creation fails, continue; bot can still operate
    pass

# parse durations like 10m, 2h, 1d, 30s
DUR_RE = re.compile(r"^(\d+)([smhd])$")

def parse_duration(s: str) -> Optional[timedelta]:
    m = DUR_RE.match(s)
    if not m:
        return None
    v, u = m.groups()
    v = int(v)
    # Validate positive duration
    if v <= 0:
        return None
    # Validate reasonable limits (max 365 days)
    if u == "d" and v > 365:
        return None
    if u == "s": return timedelta(seconds=v)
    if u == "m": return timedelta(minutes=v)
    if u == "h": return timedelta(hours=v)
    if u == "d": return timedelta(days=v)
    return None

def utc_now():
    return datetime.now(timezone.utc)


def safe_timestamp(dt: datetime) -> int:
    """Safely convert datetime to timestamp, handling naive datetimes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class AppealButton(discord.ui.View):
    """Button view for appealing a mute from DM."""
    
    def __init__(self, guild_id: int, case_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.case_id = case_id
    
    @discord.ui.button(label="Submit Appeal", style=discord.ButtonStyle.grey, emoji="<:FairyBadg:1426484412870295714>")
    async def appeal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle appeal button click."""
        # Import here to avoid circular import
        from .appeal_system import AppealModal
        
        # Get the appeal system cog
        appeal_cog = interaction.client.get_cog("AppealSystem")
        if not appeal_cog:
            await interaction.response.send_message(
                "❌ Appeal system is not available. Please contact a moderator.",
                ephemeral=True
            )
            return
        
        # Check if user can submit appeal
        can_submit, error_msg = await appeal_cog._can_submit_appeal(
            self.guild_id,
            interaction.user.id
        )
        
        if not can_submit:
            await interaction.response.send_message(
                f"❌ {error_msg}",
                ephemeral=True
            )
            return
        
        # Show the appeal modal
        modal = AppealModal(appeal_cog, self.case_id, self.guild_id)
        await interaction.response.send_modal(modal)


class ImprovedMuteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = getattr(bot, "logger", None)
        self._startup_task = self.bot.loop.create_task(self._startup_work())
        # background loop managed via tasks.loop
        self._auto_unmute_loop.start()
        self._overwrite_locks: Dict[int, asyncio.Lock] = {}

        # Intents sanity warnings
        intents = getattr(bot, "intents", None)
        if intents:
            if not intents.message_content and self.logger:
                self.logger.info("Warning: message_content intent is disabled; on_message moderation will not see content.")
            if not intents.members and self.logger:
                self.logger.info("Warning: members intent is disabled; member resolution may fail in large guilds.")

    # ---------------------------
    # Logging helper
    # ---------------------------
    def _log(self, *args, **kwargs):
        if self.logger:
            self.logger.info(*args, **kwargs)

    # ---------------------------
    # DB helpers
    # ---------------------------
    def _next_case(self, guild_id: int) -> int:
        """Atomic increment returning next case (1-based)."""
        try:
            res = guild_counters.find_one_and_update(
                {"guild_id": guild_id},
                {"$inc": {"case_id": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            return int(res.get("case_id", 1))
        except PyMongoError:
            # fallback: try to fetch and compute; not ideal but safe
            doc = guild_counters.find_one({"guild_id": guild_id})
            if not doc:
                guild_counters.insert_one({"guild_id": guild_id, "case_id": 1})
                return 1
            else:
                nxt = doc.get("case_id", 0) + 1
                guild_counters.update_one({"guild_id": guild_id}, {"$set": {"case_id": nxt}})
                return nxt

    async def _startup_work(self):
        """Schedule pending DM deletions persisted across restarts."""
        await self.bot.wait_until_ready()
        try:
            docs = list(pending_dm_deletes.find({}))
            now = utc_now()
            for doc in docs:
                try:
                    expires = doc.get("expires_at")
                    if not expires:
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                        continue
                    delay = (expires - now).total_seconds()
                    if delay <= 0:
                        await self._delete_dm_by_doc(doc)
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    else:
                        self.bot.loop.create_task(self._schedule_delete_dm(doc["_id"], delay))
                except Exception:
                    try:
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    except Exception:
                        pass
        except Exception:
            pass

    async def _schedule_delete_dm(self, doc_id, delay: float):
        await asyncio.sleep(delay)
        try:
            doc = pending_dm_deletes.find_one({"_id": doc_id})
            if not doc:
                return
            await self._delete_dm_by_doc(doc)
        finally:
            try:
                pending_dm_deletes.delete_one({"_id": doc_id})
            except Exception:
                pass

    async def _delete_dm_by_doc(self, doc):
        """Delete DM message if accessible. Use user.create_dm() to get DM channel."""
        try:
            user_id = doc.get("user_id")
            msg_id = doc.get("dm_message_id")
            if not user_id or not msg_id:
                return
            user = await self.bot.fetch_user(user_id)
            if not user:
                return
            dm = user.dm_channel
            if dm is None:
                dm = await user.create_dm()
            try:
                msg = await dm.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                # Message already deleted
                pass
            except discord.Forbidden:
                # Can't access DMs
                pass
            except Exception:
                pass
        except Exception:
            pass

    # ---------------------------
    # Permission helpers
    # ---------------------------
    def _has_overwrite_perms(self, guild: discord.Guild) -> Tuple[bool, str]:
        me = guild.me
        if not me:
            return False, "Bot user not present in guild."
        # Manage Channels governs editing channel permission overwrites
        if not me.guild_permissions.manage_channels:
            return False, "Bot lacks Manage Channels permission required to edit permission overwrites."
        return True, ""

    async def _can_manage_member(self, guild: discord.Guild, target: discord.Member) -> Tuple[bool, Optional[str]]:
        me = guild.me
        if not me:
            return False, "Bot user not present in guild."
        if not me.guild_permissions.manage_roles:
            return False, "Bot lacks Manage Roles permission."
        if target.id == guild.owner_id:
            return False, "Cannot moderate the server owner."
        if target.top_role >= me.top_role:
            return False, "Target's top role is equal or higher than the bot's top role."
        return True, None

    def _actor_can_target(self, guild: discord.Guild, actor: discord.Member, target: discord.Member) -> Tuple[bool, Optional[str]]:
        # Server owner can target anyone
        if actor.id == guild.owner_id:
            return True, None
        
        # Cannot target server owner
        if target.id == guild.owner_id:
            return False, "Cannot moderate the server owner."
        
        # Administrators can target anyone except owner
        if actor.guild_permissions.administrator:
            return True, None
            
        # Get the config to check mod role
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if cfg:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mod_role = guild.get_role(mod_role_id)
                if mod_role and mod_role in actor.roles:
                    # Allow moderators to target members below the mod role
                    if target.top_role < mod_role:
                        return True, None
                    elif target.top_role == mod_role:
                        return False, "Cannot moderate other moderators"
                        
        # For non-admins, check role hierarchy
        if target.top_role >= actor.top_role:
            return False, "You cannot act on a member with an equal or higher role than you."
            
        return True, None
        
        return True, None

    async def _apply_muted_overwrites(
        self,
        guild: discord.Guild,
        muted_role: discord.Role,
        jail_channel_id: int,
        base_sleep: float = PERMISSION_BASE_SLEEP,
        max_retries: int = PERMISSION_MAX_RETRIES,
    ):
        """Apply overwrites to categories first (inheritance), then leaf channels, with backoff."""
        ok, why = self._has_overwrite_perms(guild)
        if not ok:
            return [why]

        failed = []
        channels = list(guild.channels)
        categories = [c for c in channels if isinstance(c, discord.CategoryChannel)]
        others = [c for c in channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel))]

        # Adjust sleep time based on server size to avoid rate limits
        total_channels = len(channels)
        if total_channels > HUGE_SERVER_THRESHOLD:
            base_sleep = PERMISSION_HUGE_SERVER_SLEEP  # Slower for very large servers
        elif total_channels > LARGE_SERVER_THRESHOLD:
            base_sleep = PERMISSION_LARGE_SERVER_SLEEP  # Moderate for large servers

        async def apply_one(ch: discord.abc.GuildChannel):
            allow_in_jail = ch.id == jail_channel_id
            for attempt in range(1, max_retries + 1):
                try:
                    if allow_in_jail:
                        await ch.set_permissions(
                            muted_role,
                            view_channel=True,
                            send_messages=True,
                            add_reactions=False,
                            reason="Mute system: allow in jail",
                        )
                    else:
                        await ch.set_permissions(
                            muted_role,
                            view_channel=False,
                            send_messages=False,
                            reason="Mute system: hide from muted",
                        )
                    await asyncio.sleep(base_sleep)
                    return
                except discord.HTTPException as e:
                    # Check if it's a rate limit error
                    if e.status == 429:
                        retry_after = getattr(e, 'retry_after', 2.0)
                        await asyncio.sleep(retry_after + random.uniform(0.5, 1.5))
                    else:
                        # exponential backoff with jitter for other HTTP errors
                        backoff = min(5.0, (2 ** (attempt - 1)) * base_sleep) + random.uniform(0, 0.5)
                        await asyncio.sleep(backoff)
                except Exception:
                    break
            failed.append(getattr(ch, "name", str(ch.id)))

        # categories then others; guard with per-guild lock to avoid concurrent sweeps
        lock = self._overwrite_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            # Apply to categories first (they inherit to child channels)
            for ch in categories:
                await apply_one(ch)
            # Then apply to individual channels
            for ch in others:
                await apply_one(ch)

        return failed

    # ---------------------------
    # Slash commands: setup, check, reset, reapply
    # ---------------------------
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="setup-mute", description="Create Muted role, jail, and punishment-logs channel.")
    @app_commands.describe(
        moderator_role="Role that can use mute/unmute commands and review appeals",
        category="Category to place punishment-logs (optional)"
    )
    async def setup_mute(self, interaction: discord.Interaction, moderator_role: discord.Role, category: Optional[discord.CategoryChannel] = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Check server size and warn if large
        channel_count = len(guild.channels)
        is_large_server = channel_count > LARGE_SERVER_THRESHOLD
        
        try:
            muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
            if muted_role is None:
                muted_role = await guild.create_role(name=MUTED_ROLE_NAME, reason="Mute system setup")

            jail_channel = discord.utils.get(guild.text_channels, name=JAIL_CHANNEL_NAME)
            if jail_channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    muted_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, add_reactions=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
                }
                jail_channel = await guild.create_text_channel(JAIL_CHANNEL_NAME, overwrites=overwrites, reason="Jail for muted members")
                try:
                    await jail_channel.send(JAIL_WELCOME_MESSAGE)
                except Exception:
                    pass

            log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
                }
                if category:
                    log_channel = await guild.create_text_channel(LOG_CHANNEL_NAME, category=category, overwrites=overwrites, reason="Logs for mute/unmute")
                else:
                    log_channel = await guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites, reason="Logs for mute/unmute")

            # Send progress update for large servers
            if is_large_server:
                try:
                    await interaction.followup.send(
                        f"⏳ **Large server detected** ({channel_count} channels)\n"
                        f"Applying permissions... This may take a few minutes.\n"
                        f"_You'll receive a notification when complete._",
                        ephemeral=True
                    )
                except:
                    pass

            # apply overwrites across categories then channels
            failed = await self._apply_muted_overwrites(guild, muted_role, jail_channel.id)

            # Validate moderator role
            if moderator_role.id == guild.id:  # @everyone role
                await interaction.followup.send("<:alert:1426440385269338164> Cannot use @everyone as moderator role.", ephemeral=True)
                return
            if moderator_role.managed:
                await interaction.followup.send("<:alert:1426440385269338164> Cannot use a managed role (bot role) as moderator role.", ephemeral=True)
                return
            
            # save config
            cfg_doc = {
                "guild_id": guild.id,
                "muted_role_id": muted_role.id,
                "jail_channel_id": jail_channel.id,
                "log_channel_id": log_channel.id,
                "mod_role_id": moderator_role.id,
                "setup_by": interaction.user.id,
                "setup_at": utc_now()
            }
            guild_configs.update_one({"guild_id": guild.id}, {"$set": cfg_doc}, upsert=True)

            embed = discord.Embed(
                title="<a:white_tick:1426439810733572136> Mute System Setup Complete",
                description="The quarantine system has been successfully configured!",
                color=Colors.SUCCESS,
                timestamp=utc_now()
            )
            embed.add_field(name="<a:heartspark_ogs:1427918324066422834> Muted Role", value=muted_role.mention, inline=True)
            embed.add_field(name="<:FairyBadg:1426484412870295714> Jail Channel", value=jail_channel.mention, inline=True)
            embed.add_field(name="<:FairyBadg:1426484412870295714> Log Channel", value=log_channel.mention, inline=True)
            embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator Role", value=moderator_role.mention, inline=True)
            
            # Add statistics for large servers
            if is_large_server:
                success_count = channel_count - len(failed)
                embed.add_field(
                    name="<:sukoon_statss:1427918633082032138> Statistics",
                    value=f"**Channels:** {channel_count}\n**Updated:** {success_count}\n**Failed:** {len(failed)}",
                    inline=False
                )
            
            if failed:
                failed_list = ', '.join(failed[:10])
                if len(failed) > 10:
                    failed_list += f" _(+{len(failed) - 10} more)_"
                embed.add_field(
                    name="<:alert:1426440385269338164> Warnings",
                    value=f"Could not update permissions for: {failed_list}",
                    inline=False
                )
                embed.add_field(
                    name="<:ogs_info:1427918257226121288> Tip",
                    value="Use `/reapply-mute-perms` to retry failed channels later.",
                    inline=False
                )
            
            embed.set_footer(text=f"Setup by {interaction.user}", icon_url=interaction.user.display_avatar.url)
            
            # Try to send via followup, fallback to DM if interaction expired
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.NotFound:
                # Interaction expired (15 min timeout), send DM instead
                try:
                    await interaction.user.send(f"Setup complete for **{guild.name}**!", embed=embed)
                except:
                    pass
        except Exception as e:
            await interaction.followup.send("An error occurred during setup. Check bot permissions and try again.", ephemeral=True)
            if self.logger:
                self.logger.exception("setup-mute failed", exc_info=e)

    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="check-muteperms", description="Check if mute role and permissions are correct.")
    async def check_muteperms(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            await interaction.response.send_message("Mute system not configured. Run /setup-mute first.", ephemeral=True)
            return
        problems = []
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        if muted_role is None:
            problems.append("Muted role missing.")
        else:
            me = guild.me
            if not me.guild_permissions.manage_roles:
                problems.append("Bot lacks Manage Roles permission.")
            elif muted_role >= me.top_role:
                problems.append("Muted role is equal/higher than bot's top role.")
        
        # Check moderator role
        mod_role_id = cfg.get("mod_role_id")
        if not mod_role_id:
            problems.append("Moderator role not configured. Use /setup-mute to reconfigure.")
        else:
            mod_role = guild.get_role(mod_role_id)
            if mod_role is None:
                problems.append("Moderator role has been deleted. Use !setmodrole to set a new one.")
        
        jail = guild.get_channel(cfg.get("jail_channel_id"))
        logch = guild.get_channel(cfg.get("log_channel_id"))
        if jail is None:
            problems.append("Jail channel missing.")
        if logch is None:
            problems.append("Log channel missing.")
        # Overwrite edit capability
        ok, why = self._has_overwrite_perms(guild)
        if not ok:
            problems.append(why)
        if problems:
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Configuration Issues Found",
                description="The following problems were detected with your mute system:",
                color=Colors.ERROR,
                timestamp=utc_now()
            )
            for i, p in enumerate(problems, 1):
                embed.add_field(name=f"Issue #{i}", value=f"<:ogs_cross:1427918018196930642> {p}", inline=False)
            embed.set_footer(text="Please resolve these issues to ensure proper functionality")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="<a:white_tick:1426439810733572136> Configuration Check Passed",
                description="All basic configuration checks passed successfully!",
                color=Colors.SUCCESS,
                timestamp=utc_now()
            )
            embed.add_field(name="<a:heartspark_ogs:1427918324066422834> Muted Role", value="✓ Configured", inline=True)
            embed.add_field(name="<:FairyBadg:1426484412870295714> Jail Channel", value="✓ Configured", inline=True)
            embed.add_field(name="<:FairyBadg:1426484412870295714> Log Channel", value="✓ Configured", inline=True)
            embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator Role", value="✓ Configured", inline=True)
            embed.add_field(name="<:original_vc_mo:1427922033211342878> Permissions", value="✓ Valid", inline=True)
            embed.set_footer(text="Your mute system is ready to use!")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="reset-muteconfig", description="Reset mute configuration (admin only).")
    @app_commands.describe(confirm="Type CONFIRM to actually reset")
    async def reset_muteconfig(self, interaction: discord.Interaction, confirm: Optional[str] = None):
        if confirm != "CONFIRM":
            await interaction.response.send_message("To confirm, run: /reset-muteconfig CONFIRM", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        # Remove config and mutes; keep channels/roles as-is
        guild_configs.delete_one({"guild_id": guild.id})
        result = mutes_col.delete_many({"guild_id": guild.id})
        embed = discord.Embed(
            title="<:ogs_bell:1427918360401940552> Configuration Reset",
            description="The mute system configuration has been reset.",
            color=Colors.WARNING,
            timestamp=utc_now()
        )
        embed.add_field(name="<:FairyBadg:1426484412870295714> Records Cleared", value=f"{result.deleted_count} mute records removed", inline=True)
        embed.add_field(name="<:alert:1426440385269338164> Note", value="Channels and roles remain intact", inline=False)
        embed.set_footer(text=f"Reset by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="reapply-mute-perms", description="Reapply Muted role overwrites across the server.")
    async def reapply_mute_perms(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            await interaction.response.send_message("Mute system not configured. Run /setup-mute first.", ephemeral=True)
            return
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        if not muted_role or not jail_ch:
            await interaction.response.send_message("Configuration invalid or incomplete. Re-run /setup-mute.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        failed = await self._apply_muted_overwrites(guild, muted_role, jail_ch.id)
        embed = discord.Embed(
            title="<a:white_tick:1426439810733572136> Permissions Reapplied",
            description="Muted role overwrites have been reapplied across all categories and channels.",
            color=Colors.SUCCESS,
            timestamp=utc_now()
        )
        if failed:
            embed.add_field(
                name="<:alert:1426440385269338164> Failed Channels",
                value=f"Could not update: {', '.join(failed[:10])}",
                inline=False
            )
            embed.color = Colors.WARNING
        else:
            embed.add_field(name="<:sukoon_statss:1427918633082032138> Status", value="All channels updated successfully", inline=False)
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------
    # Prefix commands: setmodrole, qmute, qunmute, mutelist, clearmutes, jailhistory, case
    # ---------------------------
    @commands.command(name="setmodrole")
    @commands.has_permissions(administrator=True)
    async def setmodrole(self, ctx: commands.Context, role: discord.Role):
        if not ctx.guild:
            return await ctx.send("Use this command inside a server.")
        
        # Check if the role is suitable for moderation
        if role >= ctx.guild.me.top_role:
            return await ctx.send("<:alert:1426440385269338164> The moderator role must be lower than my highest role!")
        
        guild_configs.update_one({"guild_id": ctx.guild.id}, {"$set": {"mod_role_id": role.id}}, upsert=True)
        embed = discord.Embed(
            title="<a:white_tick:1426439810733572136> Moderator Role Updated",
            description=f"The moderator role has been set to {role.mention}",
            color=Colors.SUCCESS,
            timestamp=utc_now()
        )
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Role", value=role.mention, inline=True)
        embed.add_field(name="<:FairyBadg:1426484412870295714> Role ID", value=f"`{role.id}`", inline=True)
        embed.set_footer(text=f"Updated by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="quarantine", aliases=["qmute", "jail"])
    @commands.guild_only()
    async def qmute(self, ctx: commands.Context, member: str, duration: Optional[str] = None, *, reason: Optional[str] = None):
        """
        Quarantine a member - Simple and easy to use!
        
        Usage:
         !quarantine @user
         !quarantine @user spam
         !quarantine @user 10m spamming links
         !qmute @user 1h repeated warnings
        
        Duration formats: 30s, 10m, 2h, 1d
        """
        guild = ctx.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured. Ask an admin to run /setup-mute.")
        
        # Convert member string to Member object (supports mentions, IDs, and names)
        try:
            # Try to convert using discord.py's converter
            member_obj = await commands.MemberConverter().convert(ctx, member)
        except commands.MemberNotFound:
            # If that fails, try to fetch by ID
            try:
                member_id = int(member.strip('<@!>'))
                member_obj = await guild.fetch_member(member_id)
            except (ValueError, discord.NotFound, discord.HTTPException):
                return await ctx.send(f"<:alert:1426440385269338164> Could not find member: {member}")
        
        member = member_obj  # Replace string with actual Member object

        # permission check (admin or mod role)
        allowed = ctx.author.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = guild.get_role(mod_role_id)
                # Check if moderator role exists and user has it
                if not mr:
                    return await ctx.send("<:alert:1426440385269338164> Moderator role not found or has been deleted. Ask an admin to set it up again.")
                if mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")

        # Prevent self-mute
        if member.id == ctx.author.id:
            return await ctx.send("<:alert:1426440385269338164> You cannot mute yourself.")
        
        # Prevent bot mute
        if member.id == self.bot.user.id:
            return await ctx.send("<:alert:1426440385269338164> I cannot mute myself.")
        
        # Prevent muting other bots (optional safety)
        if member.bot:
            return await ctx.send("<:alert:1426440385269338164> Cannot mute bots.")

        # bot feasibility check first (to avoid confusing error messages)
        can_manage, why = await self._can_manage_member(guild, member)
        if not can_manage:
            return await ctx.send(f"<:alert:1426440385269338164> I cannot mute that member: {why}")

        # actor vs target hierarchy
        ok, why = self._actor_can_target(guild, ctx.author, member)
        if not ok:
            return await ctx.send(f"<:alert:1426440385269338164> You cannot mute that member: {why}")

        # Parse duration and reason (much simpler now)
        expires_at = None
        
        # Check if duration is actually a duration format or part of reason
        if duration:
            dur = parse_duration(duration)
            if dur:
                # It's a valid duration
                expires_at = utc_now() + dur
            else:
                # It's not a duration, treat it as part of the reason
                if reason:
                    reason = f"{duration} {reason}"
                else:
                    reason = duration
        
        # Default reason if none provided
        if not reason:
            reason = "No reason provided"

        already = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if already:
            return await ctx.send(f"{member.mention} is already muted.")

        # config objects
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role or not jail_ch or not log_ch:
            return await ctx.send("Configuration invalid or incomplete. Re-run /setup-mute.")

        # ensure bot can assign Muted role (Muted must be below bot's top role)
        me = guild.me
        if muted_role >= me.top_role:
            return await ctx.send("Muted role is equal/higher than my top role. Move my highest role above Muted.")

        # add role
        try:
            await member.add_roles(muted_role, reason=f"Muted by {ctx.author} | {reason[:100]}")
        except discord.Forbidden:
            return await ctx.send("Failed to add Muted role — check bot role hierarchy and Manage Roles permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to add Muted role: {str(e)[:100]}")
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error adding muted role", exc_info=e)
            return await ctx.send("Failed to add Muted role due to an unexpected error.")

        # Note: Overwrites are already applied during setup. No need to reapply on every mute.
        # This was causing 2+ minute delays on large servers.

        # case id
        case = self._next_case(guild.id)

        # persist mute doc
        doc = {
            "guild_id": guild.id,
            "user_id": member.id,
            "muted_by_id": ctx.author.id,
            "reason": reason[:2000],  # Limit reason length for DB
            "muted_at": utc_now(),
            "active": True,
            "case_id": case
        }
        if expires_at:
            doc["expires_at"] = expires_at
        try:
            mutes_col.insert_one(doc)
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to insert mute doc: {e}")
            # Continue anyway - role was already added
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error inserting mute doc", exc_info=e)

        # DM the user (always send DM for transparency)
        dm_was_sent = False
        dm_error_reason = None
        if True:  # Always try to DM
            try:
                dm = await member.create_dm()
                
                # Build expiry text
                if expires_at:
                    try:
                        timestamp = safe_timestamp(expires_at)
                        expiry_text = f"<t:{timestamp}:R>"
                    except (AttributeError, ValueError, OSError):
                        expiry_text = "Manual unmute"
                else:
                    expiry_text = "Manual unmute"
                
                # Create compact single-line message
                reason_short = reason[:DM_REASON_MAX_LENGTH] if len(reason) <= DM_REASON_MAX_LENGTH else f"{reason[:DM_REASON_MAX_LENGTH-3]}..."
                dm_text = f"<a:heartspark_ogs:1427918324066422834> **Muted in {guild.name}** • Case #{case} • Expires {expiry_text} • {reason_short}"
                
                # Create appeal button view
                view = AppealButton(guild.id, case)
                dm_msg = await dm.send(content=dm_text, view=view)
                
                # persist deletion: schedule delete in configured minutes
                expires = utc_now() + timedelta(minutes=DM_AUTO_DELETE_MINUTES)
                ins = pending_dm_deletes.insert_one({
                    "guild_id": guild.id,
                    "user_id": member.id,
                    "dm_message_id": dm_msg.id,
                    "expires_at": expires
                })
                # schedule in-memory deletion too
                self.bot.loop.create_task(self._schedule_delete_dm(ins.inserted_id, DM_AUTO_DELETE_MINUTES * 60))
                dm_was_sent = True
            except discord.Forbidden:
                dm_error_reason = "User has DMs disabled or blocked the bot"
                if self.logger:
                    self.logger.warning(f"Cannot DM user {member.id} ({member}): DMs disabled or bot blocked")
            except discord.HTTPException as e:
                dm_error_reason = f"HTTP error: {str(e)[:50]}"
                if self.logger:
                    self.logger.error(f"HTTP error sending DM to {member.id} ({member}): {e}")
            except Exception as e:
                dm_error_reason = f"Unexpected error: {type(e).__name__}"
                if self.logger:
                    self.logger.exception(f"Unexpected error sending DM to {member.id} ({member})", exc_info=e)

        # log embed
        embed = discord.Embed(
            title=f"<a:heartspark_ogs:1427918324066422834> Member Muted — Case #{case}",
            description=f"**{member.mention}** has been muted and moved to the quarantine zone.",
            color=Colors.MUTE,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator", value=f"{ctx.author.mention}\n`{ctx.author} ({ctx.author.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="<:ogs_bell:1427918360401940552> Reason", value=f"```{reason[:1000]}```", inline=False)
        if expires_at:
            try:
                timestamp = safe_timestamp(expires_at)
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Expires", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=False)
            except (AttributeError, ValueError, OSError):
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Expires", value=str(expires_at), inline=False)
        else:
            embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Duration", value="<:alert:1426440385269338164> Manual unmute required", inline=False)
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        
        if dm_was_sent:
            embed.set_footer(text="✉️ User notified via DM (auto-deletes in 10 minutes)", icon_url=footer_icon)
        else:
            footer_text = "⚠️ Could not send DM to user"
            if dm_error_reason:
                footer_text += f" - {dm_error_reason}"
            embed.set_footer(text=footer_text, icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        # Simple success message
        await ctx.send(f"<a:white_tick:1426439810733572136> **Member Muted Successfully** • {member.mention}")

    @commands.command(name="unquarantine", aliases=["qunmute", "unjail"])
    @commands.guild_only()
    async def qunmute(self, ctx: commands.Context, member: str, *, reason: Optional[str] = None):
        """
        Unmute a member - Simple and easy to use!
        
        Usage:
         !qunmute @user
         !qunmute @user appeal approved
        """
        guild = ctx.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        
        # Convert member string to Member object (supports mentions, IDs, and names)
        try:
            # Try to convert using discord.py's converter
            member_obj = await commands.MemberConverter().convert(ctx, member)
        except commands.MemberNotFound:
            # If that fails, try to fetch by ID
            try:
                member_id = int(member.strip('<@!>'))
                member_obj = await guild.fetch_member(member_id)
            except (ValueError, discord.NotFound, discord.HTTPException):
                return await ctx.send(f"<:alert:1426440385269338164> Could not find member: {member}")
        
        member = member_obj  # Replace string with actual Member object

        # permission check (admin or mod role)
        allowed = ctx.author.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                role = guild.get_role(mod_role_id)
                if role and role in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        
        # Prevent self-unmute (even for administrators)
        if member.id == ctx.author.id:
            return await ctx.send("<:alert:1426440385269338164> You cannot unmute yourself.")

        # Optional: prevent moderators from unmuting targets above/equal to them
        ok, why = self._actor_can_target(guild, ctx.author, member)
        if not ok:
            return await ctx.send(f"<:alert:1426440385269338164> You cannot unmute that member: {why}")

        muted_role = guild.get_role(cfg.get("muted_role_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role:
            return await ctx.send("Muted role missing. Re-run /setup-mute.")

        doc = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if not doc:
            return await ctx.send(f"{member.mention} is not currently muted.")

        # bot feasibility not strictly needed to update DB, but try role removal
        try:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
        except discord.Forbidden:
            # Log but continue - DB will be updated
            if self.logger:
                self.logger.warning(f"Failed to remove muted role from {member.id} - Forbidden")
        except discord.HTTPException as e:
            if self.logger:
                self.logger.warning(f"Failed to remove muted role from {member.id} - HTTPException: {e}")
        except Exception as e:
            if self.logger:
                self.logger.exception(f"Unexpected error removing muted role from {member.id}", exc_info=e)

        # mark as inactive
        unmute_reason = reason if reason else "Manual unmute"
        try:
            result = mutes_col.update_many({"guild_id": guild.id, "user_id": member.id, "active": True}, {"$set": {
                "active": False, "unmuted_at": utc_now(), "unmuted_by_id": ctx.author.id, "unmute_reason": unmute_reason
            }})
            if result.modified_count == 0 and self.logger:
                self.logger.warning(f"Unmute: No active mute records found for {member.id} in guild {guild.id}")
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to update mute record: {e}")
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error updating mute record", exc_info=e)

        case = doc.get("case_id", "N/A")
        embed = discord.Embed(
            title=f"<a:heartspark_ogs:1427918324066422834> Member Unmuted — Case #{case}",
            description=f"**{member.mention}** has been unmuted and can now access the server.",
            color=Colors.UNMUTE,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator", value=f"{ctx.author.mention}\n`{ctx.author} ({ctx.author.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        muted_at = doc.get("muted_at")
        if muted_at and isinstance(muted_at, datetime):
            try:
                timestamp = safe_timestamp(muted_at)
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Originally Muted", value=f"<t:{timestamp}:F>", inline=False)
            except (AttributeError, ValueError, OSError):
                pass
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        embed.set_footer(text="✅ Mute successfully removed", icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        # DM user quietly
        try:
            dm = await member.create_dm()
            dm_text = f"<a:heartspark_ogs:1427918324066422834> **Unmuted in {guild.name}** • Case #{case}"
            await dm.send(content=dm_text)
        except Exception:
            pass

        # Simple success message
        await ctx.send(f"<a:white_tick:1426439810733572136> **Member Unmuted Successfully** • {member.mention}")

    # ==================== SLASH COMMANDS ====================
    
    @app_commands.command(name="quarantine", description="Quarantine a member (mute)")
    @app_commands.describe(
        member="The member to quarantine",
        duration="Duration (e.g., 30s, 10m, 2h, 1d) - leave empty for permanent",
        reason="Reason for quarantine"
    )
    @app_commands.guild_only()
    async def slash_quarantine(self, interaction: discord.Interaction, member: discord.Member, duration: Optional[str] = None, reason: Optional[str] = None):
        """Slash command version of quarantine"""
        await interaction.response.defer(ephemeral=False)
        
        guild = interaction.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await interaction.followup.send("Mute system not configured. Ask an admin to run /setup-mute.", ephemeral=True)
        
        # Permission check
        allowed = interaction.user.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = guild.get_role(mod_role_id)
                if not mr:
                    return await interaction.followup.send("<:alert:1426440385269338164> Moderator role not found or has been deleted.", ephemeral=True)
                if mr in interaction.user.roles:
                    allowed = True
        if not allowed:
            return await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)

        # Prevent self-mute
        if member.id == interaction.user.id:
            return await interaction.followup.send("<:alert:1426440385269338164> You cannot mute yourself.", ephemeral=True)
        
        # Prevent bot mute
        if member.id == self.bot.user.id:
            return await interaction.followup.send("<:alert:1426440385269338164> I cannot mute myself.", ephemeral=True)
        
        if member.bot:
            return await interaction.followup.send("<:alert:1426440385269338164> Cannot mute bots.", ephemeral=True)

        can_manage, why = await self._can_manage_member(guild, member)
        if not can_manage:
            return await interaction.followup.send(f"<:alert:1426440385269338164> I cannot mute that member: {why}", ephemeral=True)

        ok, why = self._actor_can_target(guild, interaction.user, member)
        if not ok:
            return await interaction.followup.send(f"<:alert:1426440385269338164> You cannot mute that member: {why}", ephemeral=True)

        # Parse duration
        expires_at = None
        if duration:
            dur = parse_duration(duration)
            if dur:
                expires_at = utc_now() + dur
            else:
                if reason:
                    reason = f"{duration} {reason}"
                else:
                    reason = duration
        
        if not reason:
            reason = "No reason provided"

        already = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if already:
            return await interaction.followup.send(f"{member.mention} is already muted.", ephemeral=True)

        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role or not jail_ch or not log_ch:
            return await interaction.followup.send("Configuration invalid or incomplete. Re-run /setup-mute.", ephemeral=True)

        me = guild.me
        if muted_role >= me.top_role:
            return await interaction.followup.send("Muted role is equal/higher than my top role. Move my highest role above Muted.", ephemeral=True)

        try:
            await member.add_roles(muted_role, reason=f"Muted by {interaction.user} | {reason[:100]}")
        except discord.Forbidden:
            return await interaction.followup.send("Failed to add Muted role — check bot role hierarchy and Manage Roles permission.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Failed to add Muted role: {str(e)[:100]}", ephemeral=True)

        case = self._next_case(guild.id)

        doc = {
            "guild_id": guild.id,
            "user_id": member.id,
            "muted_by_id": interaction.user.id,
            "reason": reason[:2000],
            "muted_at": utc_now(),
            "active": True,
            "case_id": case
        }
        if expires_at:
            doc["expires_at"] = expires_at
        try:
            mutes_col.insert_one(doc)
        except Exception:
            pass

        # DM the user
        dm_was_sent = False
        dm_error_reason = None
        try:
            dm = await member.create_dm()
            
            if expires_at:
                try:
                    timestamp = safe_timestamp(expires_at)
                    expiry_text = f"<t:{timestamp}:R>"
                except:
                    expiry_text = "Manual unmute"
            else:
                expiry_text = "Manual unmute"
            
            reason_short = reason[:DM_REASON_MAX_LENGTH] if len(reason) <= DM_REASON_MAX_LENGTH else f"{reason[:DM_REASON_MAX_LENGTH-3]}..."
            dm_text = f"<a:heartspark_ogs:1427918324066422834> **Muted in {guild.name}** • Case #{case} • Expires {expiry_text} • {reason_short}"
            
            view = AppealButton(guild.id, case)
            dm_msg = await dm.send(content=dm_text, view=view)
            
            expires = utc_now() + timedelta(minutes=DM_AUTO_DELETE_MINUTES)
            ins = pending_dm_deletes.insert_one({
                "guild_id": guild.id,
                "user_id": member.id,
                "dm_message_id": dm_msg.id,
                "expires_at": expires
            })
            self.bot.loop.create_task(self._schedule_delete_dm(ins.inserted_id, DM_AUTO_DELETE_MINUTES * 60))
            dm_was_sent = True
        except discord.Forbidden:
            dm_error_reason = "User has DMs disabled or blocked the bot"
        except Exception:
            dm_error_reason = "Unexpected error"

        # Log embed
        embed = discord.Embed(
            title=f"<a:heartspark_ogs:1427918324066422834> Member Muted — Case #{case}",
            description=f"**{member.mention}** has been muted and moved to the quarantine zone.",
            color=Colors.MUTE,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except:
            pass
        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator", value=f"{interaction.user.mention}\n`{interaction.user} ({interaction.user.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="<:ogs_bell:1427918360401940552> Reason", value=f"```{reason[:1000]}```", inline=False)
        if expires_at:
            try:
                timestamp = safe_timestamp(expires_at)
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Expires", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=False)
            except:
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Expires", value=str(expires_at), inline=False)
        else:
            embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Duration", value="<:alert:1426440385269338164> Manual unmute required", inline=False)
        
        footer_icon = None
        try:
            if guild and guild.icon:
                footer_icon = guild.icon.url
        except:
            pass
        
        if dm_was_sent:
            embed.set_footer(text="✉️ User notified via DM (auto-deletes in 10 minutes)", icon_url=footer_icon)
        else:
            footer_text = "⚠️ Could not send DM to user"
            if dm_error_reason:
                footer_text += f" - {dm_error_reason}"
            embed.set_footer(text=footer_text, icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except:
            pass

        await interaction.followup.send(f"<a:white_tick:1426439810733572136> **Member Muted Successfully** • {member.mention}")

    @app_commands.command(name="unquarantine", description="Unquarantine a member (unmute)")
    @app_commands.describe(
        member="The member to unquarantine",
        reason="Reason for unquarantine"
    )
    @app_commands.guild_only()
    async def slash_unquarantine(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        """Slash command version of unquarantine"""
        await interaction.response.defer(ephemeral=False)
        
        guild = interaction.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await interaction.followup.send("Mute system not configured.", ephemeral=True)
        
        # Permission check
        allowed = interaction.user.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                role = guild.get_role(mod_role_id)
                if role and role in interaction.user.roles:
                    allowed = True
        if not allowed:
            return await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        
        if member.id == interaction.user.id:
            return await interaction.followup.send("<:alert:1426440385269338164> You cannot unmute yourself.", ephemeral=True)

        ok, why = self._actor_can_target(guild, interaction.user, member)
        if not ok:
            return await interaction.followup.send(f"<:alert:1426440385269338164> You cannot unmute that member: {why}", ephemeral=True)

        muted_role = guild.get_role(cfg.get("muted_role_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role:
            return await interaction.followup.send("Muted role missing. Re-run /setup-mute.", ephemeral=True)

        doc = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if not doc:
            return await interaction.followup.send(f"{member.mention} is not currently muted.", ephemeral=True)

        try:
            await member.remove_roles(muted_role, reason=f"Unmuted by {interaction.user}")
        except:
            pass

        unmute_reason = reason if reason else "Manual unmute"
        try:
            mutes_col.update_many({"guild_id": guild.id, "user_id": member.id, "active": True}, {"$set": {
                "active": False, "unmuted_at": utc_now(), "unmuted_by_id": interaction.user.id, "unmute_reason": unmute_reason
            }})
        except:
            pass

        case = doc.get("case_id", "N/A")
        embed = discord.Embed(
            title=f"<a:heartspark_ogs:1427918324066422834> Member Unmuted — Case #{case}",
            description=f"**{member.mention}** has been unmuted and can now access the server.",
            color=Colors.UNMUTE,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except:
            pass
        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Moderator", value=f"{interaction.user.mention}\n`{interaction.user} ({interaction.user.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        muted_at = doc.get("muted_at")
        if muted_at and isinstance(muted_at, datetime):
            try:
                timestamp = safe_timestamp(muted_at)
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Originally Muted", value=f"<t:{timestamp}:F>", inline=False)
            except:
                pass
        footer_icon = None
        try:
            if guild and guild.icon:
                footer_icon = guild.icon.url
        except:
            pass
        embed.set_footer(text="✅ Mute successfully removed", icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except:
            pass

        # DM user
        try:
            dm = await member.create_dm()
            dm_text = f"<a:heartspark_ogs:1427918324066422834> **Unmuted in {guild.name}** • Case #{case}"
            await dm.send(content=dm_text)
        except:
            pass

        await interaction.followup.send(f"<a:white_tick:1426439810733572136> **Member Unmuted Successfully** • {member.mention}")

    @commands.command(name="mutelist")
    @commands.guild_only()
    async def mutelist(self, ctx: commands.Context):
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        docs = list(mutes_col.find({"guild_id": ctx.guild.id, "active": True}).sort("muted_at", -1).limit(100))
        if not docs:
            return await ctx.send("No members are currently muted.")

        def make_embed(batch):
            embed = discord.Embed(
                title="<a:heartspark_ogs:1427918324066422834> Currently Muted Members",
                description=f"Showing **{len(batch)}** of **{len(docs)}** total muted members",
                color=Colors.INFO,
                timestamp=utc_now()
            )
            # Discord has a 25 field limit, so cap at 10 per page
            for d in batch[:10]:
                uid = d["user_id"]
                member = ctx.guild.get_member(uid)
                mention = member.mention if member else f"<@{uid}>"
                by = d.get("muted_by_id")
                muter = ctx.guild.get_member(by)
                muter_s = muter.mention if muter else f"<@{by}>"
                reason = d.get("reason", "No reason")
                at = d.get("muted_at")
                case = d.get("case_id", "N/A")
                expires = d.get("expires_at")
                
                field_value = f"**Moderator:** {muter_s}\n"
                field_value += f"**Reason:** {reason[:100]}\n"
                if isinstance(at, datetime):
                    try:
                        field_value += f"**Muted:** <t:{safe_timestamp(at)}:R>\n"
                    except (AttributeError, ValueError, OSError):
                        pass
                if isinstance(expires, datetime):
                    try:
                        field_value += f"**Expires:** <t:{safe_timestamp(expires)}:R>"
                    except (AttributeError, ValueError, OSError):
                        field_value += f"**Expires:** <:alert:1426440385269338164> Manual unmute required"
                else:
                    field_value += f"**Expires:** <:alert:1426440385269338164> Manual unmute required"
                
                embed.add_field(name=f"Case #{case} — {mention}", value=field_value, inline=False)
            
            footer_icon = None
            try:
                if ctx.guild and ctx.guild.icon:
                    footer_icon = ctx.guild.icon.url
            except Exception:
                pass
            embed.set_footer(text=f"Total muted members: {len(docs)}", icon_url=footer_icon)
            return embed

        # Send in batches of 10 to respect Discord's 25 field limit
        for i in range(0, len(docs), 10):
            await ctx.send(embed=make_embed(docs[i:i+10]))

    @commands.command(name="clearmutes")
    @commands.has_permissions(administrator=True)
    async def clearmutes(self, ctx: commands.Context, days: Optional[int] = 30):
        # Validate days parameter
        if days <= 0:
            return await ctx.send("<:alert:1426440385269338164> Days must be a positive number.")
        if days > 3650:  # 10 years max
            return await ctx.send("<:alert:1426440385269338164> Days cannot exceed 3650 (10 years).")
        
        cutoff = utc_now() - timedelta(days=days)
        try:
            res = mutes_col.delete_many({"active": False, "muted_at": {"$lt": cutoff}})
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to delete mute records: {e}")
            return await ctx.send("<:alert:1426440385269338164> Failed to clear mute records. Check logs for details.")
        embed = discord.Embed(
            title="<:sukoon_statss:1427918633082032138> Database Cleanup Complete",
            description=f"Cleared old inactive mute records from the database.",
            color=Colors.SUCCESS,
            timestamp=utc_now()
        )
        embed.add_field(name="<:sukoon_statss:1427918633082032138> Records Deleted", value=f"**{res.deleted_count}** records", inline=True)
        embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Older Than", value=f"**{days}** days", inline=True)
        embed.set_footer(text=f"Cleanup by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="jailhistory")
    @commands.guild_only()
    async def jailhistory(self, ctx: commands.Context, user: discord.User, limit: Optional[int] = 10):
        """Moderator-only: fetch recent messages for a muted user from jail (default last 10)."""
        # Validate limit
        if limit <= 0:
            return await ctx.send("<:alert:1426440385269338164> Limit must be a positive number.")
        if limit > 50:
            limit = 50
            await ctx.send("<:alert:1426440385269338164> Limit capped at 50 messages.")
        
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        # permission check: admin or mod role
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = ctx.guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        try:
            docs = list(jail_messages.find({"guild_id": ctx.guild.id, "user_id": user.id}).sort("created_at", -1).limit(min(50, max(1, limit))))
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to fetch jail messages: {e}")
            return await ctx.send("<:alert:1426440385269338164> Failed to retrieve jail history. Try again later.")
        if not docs:
            embed = discord.Embed(
                title="<:ogs_info:1427918257226121288> No Messages Found",
                description=f"No jail messages found for {user.mention} in the last 7 days.",
                color=Colors.WARNING,
                timestamp=utc_now()
            )
            embed.set_footer(text="Messages are automatically deleted after 7 days")
            return await ctx.send(embed=embed)
        
        embed = discord.Embed(
            title=f"<:FairyBadg:1426484412870295714> Jail History for {user.name}",
            description=f"Showing last **{min(len(docs), limit)}** messages from {user.mention}",
            color=Colors.INFO,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        
        # Discord has a 25 field limit
        max_messages = min(limit, 20)
        for i, d in enumerate(docs[:max_messages], 1):
            ts = d.get("created_at")
            content = d.get("content", "")[:200]
            if isinstance(ts, datetime):
                try:
                    time_str = f"<t:{safe_timestamp(ts)}:R>"
                except (AttributeError, ValueError, OSError):
                    time_str = str(ts)
            else:
                time_str = str(ts)
            # Escape backticks in content to prevent breaking code block
            if content:
                content = content.replace("```", "'''")
                # Also escape other potential markdown issues
                content = content.replace("\n", " ")  # Replace newlines with spaces
            embed.add_field(
                name=f"Message #{i} — {time_str}",
                value=f"```{content}```" if content else "*Empty message*",
                inline=False
            )
        
        embed.set_footer(text=f"Requested by {ctx.author} | Messages auto-delete after 7 days", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="case")
    @commands.guild_only()
    async def case(self, ctx: commands.Context, case_id: int):
        """Show details for a case id (moderator-only)."""
        # Validate case_id
        if case_id <= 0:
            return await ctx.send("<:alert:1426440385269338164> Case ID must be a positive number.")
        
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        # permission check
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = ctx.guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        try:
            doc = mutes_col.find_one({"guild_id": ctx.guild.id, "case_id": case_id})
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to fetch case: {e}")
            return await ctx.send("<:alert:1426440385269338164> Failed to retrieve case information. Try again later.")
        
        if not doc:
            return await ctx.send(f"No case found for Case #{case_id}.")
        member_repr = f"<@{doc.get('user_id')}>"
        muted_by_repr = f"<@{doc.get('muted_by_id')}>"
        reason = doc.get("reason", "No reason")
        muted_at = doc.get("muted_at")
        muted_at_s = muted_at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(muted_at, datetime) else str(muted_at)
        active = doc.get("active", False)
        status_emoji = "<a:reddot:1427539828521697282>" if active else "<a:originals_accep:1426484148884865190>"
        status_text = "Active" if active else "Resolved"
        embed = discord.Embed(
            title=f"<:FairyBadg:1426484412870295714> Case #{case_id} — {status_text}",
            description=f"Detailed information for mute case **#{case_id}**",
            color=Colors.MUTE if active else Colors.UNMUTE,
            timestamp=utc_now()
        )
        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=member_repr, inline=True)
        embed.add_field(name="<:original_vc_mo:1427922033211342878> Muted By", value=muted_by_repr, inline=True)
        embed.add_field(name="<:sukoon_statss:1427918633082032138> Status", value=f"{status_emoji} **{status_text}**", inline=True)
        embed.add_field(name="<:ogs_bell:1427918360401940552> Reason", value=f"```{reason[:1000]}```", inline=False)
        if isinstance(muted_at, datetime):
            try:
                timestamp = safe_timestamp(muted_at)
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Muted At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
            except (AttributeError, ValueError, OSError):
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Muted At", value=muted_at_s, inline=True)
        else:
            embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Muted At", value=muted_at_s, inline=True)
        if doc.get("expires_at"):
            exp_ts = doc["expires_at"]
            if isinstance(exp_ts, datetime):
                try:
                    timestamp = safe_timestamp(exp_ts)
                    embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Expires At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
                except (AttributeError, ValueError, OSError):
                    embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Duration", value="<:alert:1426440385269338164> Manual unmute", inline=True)
            else:
                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Duration", value="<:alert:1426440385269338164> Manual unmute", inline=True)
        else:
            embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Duration", value="<:alert:1426440385269338164> Manual unmute", inline=True)
        if doc.get("unmuted_at"):
            unmute_ts = doc["unmuted_at"]
            if isinstance(unmute_ts, datetime):
                try:
                    timestamp = safe_timestamp(unmute_ts)
                    embed.add_field(name="<a:white_tick:1426439810733572136> Unmuted At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
                except (AttributeError, ValueError, OSError):
                    pass
            if doc.get("unmuted_by_id"):
                embed.add_field(name="<:original_vc_mo:1427922033211342878> Unmuted By", value=f"<@{doc['unmuted_by_id']}>", inline=True)
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        embed.set_footer(text=f"Case ID: {case_id}", icon_url=footer_icon)
        await ctx.send(embed=embed)

    # ---------------------------
    # Background auto-unmute loop (temporary mutes) with tasks.loop
    # ---------------------------
    @tasks.loop(seconds=20)
    async def _auto_unmute_loop(self):
        try:
            now = utc_now()
            docs = list(mutes_col.find({"active": True, "expires_at": {"$lte": now}}))
            for doc in docs:
                try:
                    guild = self.bot.get_guild(doc["guild_id"])
                    if not guild:
                        mutes_col.update_one({"_id": doc["_id"]}, {"$set": {"active": False}})
                        continue
                    cfg = guild_configs.find_one({"guild_id": guild.id})
                    if not cfg:
                        mutes_col.update_one({"_id": doc["_id"]}, {"$set": {"active": False}})
                        continue
                    muted_role = guild.get_role(cfg.get("muted_role_id"))
                    log_ch = guild.get_channel(cfg.get("log_channel_id"))

                    member = guild.get_member(doc["user_id"])
                    if member is None:
                        try:
                            member = await guild.fetch_member(doc["user_id"])
                        except Exception:
                            member = None

                    if member and muted_role and guild.me and guild.me.guild_permissions.manage_roles:
                        try:
                            await member.remove_roles(muted_role, reason="Temporary mute expired")
                        except discord.Forbidden:
                            if self.logger:
                                self.logger.warning(f"Auto-unmute: Failed to remove role from {member.id} - Forbidden")
                        except discord.HTTPException:
                            # Rate limited or other HTTP error, will retry next loop
                            continue
                        except Exception as e:
                            if self.logger:
                                self.logger.error(f"Auto-unmute: Unexpected error removing role: {e}")

                    # atomic update so we only log once
                    result = mutes_col.update_one(
                        {"_id": doc["_id"], "active": True},
                        {"$set": {"active": False, "unmuted_at": utc_now(), "unmuted_by_id": None}}
                    )
                    if result.modified_count > 0 and log_ch:
                        case_id = doc.get("case_id", "N/A")
                        embed = discord.Embed(
                            title=f"<a:heartspark_ogs:1427918324066422834> Auto-Unmute — Case #{case_id}",
                            description=f"Temporary mute has expired and been automatically removed.",
                            color=Colors.UNMUTE,
                            timestamp=utc_now()
                        )
                        uid = doc.get("user_id")
                        embed.add_field(name="<:Ogs_member:1427922355879022672> Member", value=f"<@{uid}>", inline=True)
                        embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Reason", value="<:sukoon_blackdot:1427918583136260136> Temporary mute expired", inline=True)
                        embed.add_field(name="<:ogs_info:1427918257226121288> Action", value="Automatic", inline=True)
                        muted_at = doc.get("muted_at")
                        if muted_at and isinstance(muted_at, datetime):
                            try:
                                timestamp = safe_timestamp(muted_at)
                                embed.add_field(name="<:sukoon_blackdot:1427918583136260136> Originally Muted", value=f"<t:{timestamp}:R>", inline=False)
                            except (AttributeError, ValueError, OSError):
                                pass
                        footer_icon = None
                        try:
                            if guild and guild.icon:
                                footer_icon = guild.icon.url
                        except Exception:
                            pass
                        embed.set_footer(text="✅ Automatic unmute completed", icon_url=footer_icon)
                        try:
                            await log_ch.send(embed=embed)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    # Re-raise immediately to stop the loop gracefully
                    raise
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Error in auto-unmute for doc {doc.get('_id')}: {e}")
        except asyncio.CancelledError:
            # Task was cancelled during reload/shutdown - this is expected
            if self.logger:
                self.logger.debug("Auto-unmute loop cancelled (cog reload/shutdown)")
            raise  # Re-raise to properly stop the task
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error in auto-unmute loop: {e}")

    @_auto_unmute_loop.before_loop
    async def _before_auto_unmute_loop(self):
        await self.bot.wait_until_ready()

    # ---------------------------
    # Listener: jail message logging and mention enforcement
    # ---------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            return
        cfg = guild_configs.find_one({"guild_id": message.guild.id})
        if not cfg:
            return
        muted_role_id = cfg.get("muted_role_id")
        jail_id = cfg.get("jail_channel_id")
        if not muted_role_id or not jail_id:
            return
        if message.channel.id != jail_id:
            return
        member = message.author
        if muted_role_id not in [r.id for r in member.roles]:
            return
        # log message (store content, id, timestamp)
        try:
            jail_messages.insert_one({
                "guild_id": message.guild.id,
                "user_id": member.id,
                "message_id": message.id,
                "content": message.content,
                "created_at": utc_now()
            })
        except Exception:
            pass
        # enforce no mentions
        has_mentions = bool(message.mentions or message.role_mentions or message.mention_everyone)
        if has_mentions:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                warn = await message.channel.send(f"{member.mention} — you may not mention anyone in this channel. Your message was removed.")
                await asyncio.sleep(12)
                try:
                    await warn.delete()
                except Exception:
                    pass
            except Exception:
                pass

    # ---------------------------
    # Errors & unload
    # ---------------------------
    @qmute.error
    async def _on_qmute_error(self, ctx: commands.Context, error):
        # Unwrap the error if it's wrapped in CommandInvokeError
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        
        if isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Missing Argument",
                description="You need to specify a member to mute.",
                color=Colors.ERROR
            )
            embed.add_field(name="Usage", value="`!qmute @user [duration] [reason]`", inline=False)
            embed.add_field(name="Examples", value="`!qmute @user` - Permanent mute\n`!qmute @user spam` - Permanent with reason\n`!qmute @user 10m spamming` - 10 minute mute\n`!qmute @user 1h repeated warnings` - 1 hour mute", inline=False)
            embed.add_field(name="Duration Formats", value="30s (seconds), 10m (minutes), 2h (hours), 1d (days)", inline=False)
            await ctx.send(embed=embed)
        elif isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Member Not Found",
                description="Couldn't find that member. Make sure they're in this server.",
                color=Colors.ERROR
            )
            embed.add_field(name="Tip", value="Mention them like `@username` or use their user ID", inline=False)
            await ctx.send(embed=embed)
        else:
            # Only log unexpected errors, not user errors
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Command Error",
                description="An unexpected error occurred. Please try again.",
                color=Colors.ERROR
            )
            await ctx.send(embed=embed)
            if self.logger:
                self.logger.error(f"Unexpected qmute error: {type(error).__name__}: {error}")

    @qunmute.error
    async def _on_qunmute_error(self, ctx: commands.Context, error):
        # Unwrap the error if it's wrapped in CommandInvokeError
        if isinstance(error, commands.CommandInvokeError):
            error = error.original
        
        if isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Missing Argument",
                description="You need to specify a member to unmute.",
                color=Colors.ERROR
            )
            embed.add_field(name="Usage", value="`!qunmute @user [reason]`", inline=False)
            embed.add_field(name="Examples", value="`!qunmute @user` - Simple unmute\n`!qunmute @user appeal approved` - Unmute with reason", inline=False)
            await ctx.send(embed=embed)
        elif isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Member Not Found",
                description="Couldn't find that member. Make sure they're in this server.",
                color=Colors.ERROR
            )
            embed.add_field(name="Tip", value="Mention them like `@username` or use their user ID", inline=False)
            await ctx.send(embed=embed)
        else:
            # Only log unexpected errors, not user errors
            embed = discord.Embed(
                title="<:alert:1426440385269338164> Command Error",
                description="An unexpected error occurred. Please try again.",
                color=Colors.ERROR
            )
            await ctx.send(embed=embed)
            if self.logger:
                self.logger.error(f"Unexpected qunmute error: {type(error).__name__}: {error}")

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        try:
            self._startup_task.cancel()
        except Exception:
            pass
        try:
            self._auto_unmute_loop.cancel()
        except Exception:
            pass
        # Clear lock dictionary to prevent memory leaks
        try:
            self._overwrite_locks.clear()
        except Exception:
            pass
        # Note: MongoDB connection is shared, don't close it here

async def setup(bot: commands.Bot):
    await bot.add_cog(ImprovedMuteCog(bot))
