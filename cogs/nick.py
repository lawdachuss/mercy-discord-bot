"""
Nick - server nickname management.

Admins configure a dedicated role that is allowed to rename other members.
Anyone may rename themselves. Every command exists twice - as a slash command
and as a prefix command:

    /nick  <member> <nickname>      ->  .nick <@member> <nickname>
    /nick  <nickname>                ->  .nick <nickname>        (yourself)
    /nicksetup                       ->  .nicksetup              (admin)

Discord does not let a bot change a user's *username* - only the server
nickname - which is all this cog manages.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

MAX_NICKNAME_LENGTH = 32
CONFIG_CACHE_TTL = 30.0
RESET_WORDS = {"clear", "reset", "remove", "none", "default", "-", "\u2014"}

# <@123>, <@!123> or a bare snowflake - used by the prefix command to decide
# whether the first word is a target member or part of the nickname.
MENTION_OR_ID = re.compile(r"^(?:<@!?(\d+)>|(\d{17,20}))$")
ROLE_MENTION = re.compile(r"^<@&(\d+)>$")
CHANNEL_MENTION = re.compile(r"^<#(\d+)>$")


class NickError(Exception):
    """A user-facing error. The message is shown to the invoker as-is."""


def _default_config(guild_id: int) -> Dict[str, Any]:
    return {
        "guild_id": guild_id,
        "nick_role_id": None,
        "log_channel_id": None,
        "updated_at": None,
    }


class NickCog(commands.Cog):
    """Role-gated nickname management, available as both slash and prefix commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._config_cache: Dict[int, tuple] = {}

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    @property
    def collection(self):
        client = getattr(self.bot, "mongo_client", None)
        if client is None:
            return None
        return client["discord_bot"]["nick_settings"]

    async def get_config(self, guild_id: int) -> Dict[str, Any]:
        """Guild configuration with a short TTL cache (defaults if Mongo is down)."""
        now = time.time()
        cached = self._config_cache.get(guild_id)
        if cached and now - cached[0] < CONFIG_CACHE_TTL:
            # Always hand out a copy so callers can safely edit it without
            # mutating the cached document.
            return dict(cached[1])

        config = _default_config(guild_id)
        collection = self.collection
        read_ok = collection is None
        if collection is not None:
            try:
                doc = await collection.find_one({"guild_id": guild_id})
                if doc:
                    doc.pop("_id", None)
                    config.update(doc)
                    config["guild_id"] = guild_id
            except Exception as e:
                # Do NOT cache a failed read: a transient Mongo error would
                # otherwise pin "all defaults" for the whole TTL and a save
                # right after could overwrite the real settings.
                read_ok = False
                logger.warning(f"[Nick] Could not read config for {guild_id}: {e}")

        if read_ok:
            self._config_cache[guild_id] = (now, config)
        return config

    async def save_config(self, config: Dict[str, Any],
                          changed_keys: Optional[List[str]] = None) -> bool:
        """Persist config.

        When ``changed_keys`` is given only those fields are written with
        ``$set`` - a stale or defaulted read can then never wipe the other
        stored settings, and two admins saving concurrently cannot clobber
        each other's changes. Without keys the full document is replaced
        (kept for compatibility with any caller that means to rewrite all).
        """
        collection = self.collection
        if collection is None:
            return False
        config["updated_at"] = discord.utils.utcnow().isoformat()
        try:
            if changed_keys:
                update = {k: config[k] for k in dict.fromkeys([*changed_keys, "updated_at"])
                          if k in config}
                await collection.update_one(
                    {"guild_id": config["guild_id"]}, {"$set": update}, upsert=True
                )
            else:
                await collection.replace_one(
                    {"guild_id": config["guild_id"]}, config, upsert=True
                )
        except Exception as e:
            logger.error(f"[Nick] Could not save config for {config['guild_id']}: {e}")
            return False
        self._config_cache[config["guild_id"]] = (time.time(), dict(config))
        return True

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    @staticmethod
    def _holds_nick_role(member: discord.Member, config: Dict[str, Any]) -> bool:
        role_id = config.get("nick_role_id")
        if not role_id:
            return False
        return any(role.id == role_id for role in member.roles)

    def _can_rename_others(self, member: discord.Member, config: Dict[str, Any]) -> bool:
        """Only the configured role (or a server administrator) may rename people."""
        if member.guild_permissions.administrator:
            return True
        return self._holds_nick_role(member, config)

    # ------------------------------------------------------------------
    # The actual change
    # ------------------------------------------------------------------
    async def change_nickname(self, guild: discord.Guild, invoker: discord.Member,
                              target: discord.Member,
                              nickname: Optional[str]) -> discord.Embed:
        """Validate and apply a nickname change, returning the embed describing it."""
        config = await self.get_config(guild.id)
        is_self = target.id == invoker.id

        if not is_self and not self._can_rename_others(invoker, config):
            role_id = config.get("nick_role_id")
            role = guild.get_role(role_id) if role_id else None
            requirement = (
                f"the {role.mention} role" if role else "an admin-configured role (not set up yet)"
            )
            raise NickError(
                f"You need {requirement} to change someone else's nickname.\n"
                f"Use `.nick <nickname>` to change your own instead."
            )

        # A non-admin may only rename members ranked below them.
        if not is_self and not invoker.guild_permissions.administrator:
            if target.top_role >= invoker.top_role:
                raise NickError(
                    f"{target.display_name} is ranked as high as or higher than you, "
                    f"so you cannot rename them."
                )

        bot_member = guild.me
        if bot_member is None:
            raise NickError("I cannot check my permissions right now - try again in a moment.")
        if not bot_member.guild_permissions.manage_nicknames:
            raise NickError("I need the **Manage Nicknames** permission to do that.")
        if target.id == guild.owner_id:
            raise NickError("I cannot rename the server owner.")
        if target.id == bot_member.id:
            raise NickError("I cannot rename myself this way.")
        if bot_member.top_role <= target.top_role:
            raise NickError(
                f"My highest role must be above {target.display_name}'s highest role - "
                f"move my role up in **Server Settings > Roles**."
            )

        if nickname is not None:
            nickname = nickname.strip()
            if not nickname:
                raise NickError("The nickname cannot be empty - use `clear` to reset it.")
            if len(nickname) > MAX_NICKNAME_LENGTH:
                raise NickError(
                    f"Nicknames are limited to {MAX_NICKNAME_LENGTH} characters "
                    f"(yours is {len(nickname)})."
                )

        old_nick = target.display_name
        try:
            updated = await target.edit(
                nick=nickname,
                reason=f"Nickname changed by {invoker} ({invoker.id})",
            )
        except discord.Forbidden:
            raise NickError(
                "Discord refused the change (403). Check that I have **Manage Nicknames** "
                "and that my role sits above theirs."
            )
        except discord.HTTPException as e:
            raise NickError(f"Discord rejected the change: HTTP {e.status} - {e.text}")

        # Member.edit() returns a NEW Member and never mutates `target`, so the
        # returned object is the only reliable source for the new display name.
        new_nick = (updated or target).display_name
        reset = nickname is None
        embed = discord.Embed(
            title="✅ Nickname changed" if not reset else "🔁 Nickname reset",
            color=discord.Color.green() if not reset else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Member", value=f"{target.mention} (`{target.id}`)", inline=True)
        embed.add_field(name="Changed by", value=f"{invoker.mention}", inline=True)
        embed.add_field(name="Before", value=f"`{old_nick}`", inline=True)
        embed.add_field(
            name="After",
            value=f"`{new_nick}`" + (" — shows their username now" if reset else ""),
            inline=True,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=guild.name)

        await self._send_log(guild, config, invoker, target, old_nick, new_nick, reset)
        return embed

    async def _send_log(self, guild: discord.Guild, config: Dict[str, Any],
                        invoker: discord.Member, target: discord.Member,
                        old_nick: str, new_nick: str, reset: bool) -> None:
        """Post an audit entry to the configured log channel (best effort)."""
        channel_id = config.get("log_channel_id")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"[Nick] Log channel {channel_id} is unreachable: {e}")
                return
        if not hasattr(channel, "send"):
            logger.warning(f"[Nick] Log channel {channel_id} cannot receive messages.")
            return

        embed = discord.Embed(
            title="🔁 Nickname reset" if reset else "👤 Nickname changed",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Member", value=f"{target.mention} (`{target.id}`)", inline=True)
        embed.add_field(name="Moderator", value=f"{invoker.mention} (`{invoker.id}`)", inline=True)
        embed.add_field(name="From", value=f"`{old_nick}`", inline=False)
        embed.add_field(name="To", value=f"`{new_nick}`", inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"Guild {guild.id}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"[Nick] Could not write to log channel {channel_id}: {e}")

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
    async def _send(self, destination: Any, *, embed: Optional[discord.Embed] = None,
                    content: Optional[str] = None, ephemeral: bool = False) -> None:
        """Send an embed, falling back to plain text if Embed Links is missing.

        For interactions this is defer-aware: once a response has been issued
        (e.g. via ``defer``) everything goes out through ``followup.send``, so
        commands can do their Mongo/API work before acknowledging.
        """
        # Error text can contain role mentions - never let them ping.
        mentions = discord.AllowedMentions(roles=False, users=True, everyone=False)

        def _is_interaction() -> bool:
            return isinstance(destination, discord.Interaction)

        def _responded() -> bool:
            return _is_interaction() and destination.response.is_done()

        if embed is not None:
            try:
                if _responded():
                    await destination.followup.send(embed=embed, ephemeral=ephemeral)
                elif _is_interaction():
                    await destination.response.send_message(embed=embed, ephemeral=ephemeral)
                else:
                    await destination.send(embed=embed)
                return
            except discord.Forbidden:
                parts = [embed.title or "", embed.description or ""]
                parts += [f"**{f.name}**: {f.value}" for f in embed.fields]
                content = "\n".join(p for p in parts if p)
                embed = None
            except Exception as e:
                logger.warning(f"[Nick] Could not send embed: {e}")
                return

        text = content or ""
        if _responded():
            await destination.followup.send(text, ephemeral=ephemeral, allowed_mentions=mentions)
        elif _is_interaction():
            await destination.response.send_message(text, ephemeral=ephemeral, allowed_mentions=mentions)
        else:
            await destination.send(text, allowed_mentions=mentions)

    @staticmethod
    def usage_embed() -> discord.Embed:
        embed = discord.Embed(
            title="👤 Nickname commands",
            color=discord.Color.blurple(),
            description=(
                "**Change your own nickname** (everyone)\n"
                "```\n.nick <nickname>\n.nick clear\n```\n"
                "**Rename someone else** (configured role or Administrator)\n"
                "```\n.nick <@member> <nickname>\n.nick <@member> clear\n```\n"
                "**Setup** (Administrator)\n"
                "```\n.nicksetup\n.nicksetup role <@role>\n.nicksetup log <#channel>\n```\n"
                "The same commands work as `/nick` and `/nicksetup`.\n\n"
                "*Bots can only change server nicknames, not Discord usernames.*"
            ),
        )
        return embed

    def config_embed(self, guild: discord.Guild, config: Dict[str, Any]) -> discord.Embed:
        role = guild.get_role(config["nick_role_id"]) if config.get("nick_role_id") else None
        channel = (
            guild.get_channel(config["log_channel_id"])
            if config.get("log_channel_id") else None
        )

        bot_member = guild.me
        can_rename = bool(
            bot_member and bot_member.guild_permissions.manage_nicknames
        )

        embed = discord.Embed(
            title="⚙️ Nickname setup",
            color=discord.Color.green() if can_rename else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🎭 Role allowed to rename others",
            value=(role.mention if role else "❌ Not configured") + (
                "" if role else "\nUse `.nicksetup role <@role>` or `/nicksetup role:`"
            ),
            inline=False,
        )
        embed.add_field(
            name="📜 Change log",
            value=channel.mention if channel else "❌ Off",
            inline=False,
        )
        embed.add_field(
            name="🪪 Rename permission",
            value=(
                "Server **Administrators** always can"
                + (f"; so does {role.mention}" if role else "; no role configured yet")
            ),
            inline=False,
        )
        embed.add_field(
            name="🔧 My permissions",
            value=("✅ Manage Nicknames" if can_rename else "❌ Missing **Manage Nicknames**"),
            inline=False,
        )
        embed.set_footer(text=".nicksetup role <@role> · .nicksetup log <#channel|off> · .nicksetup clear <role|log|all>")
        return embed

    # ------------------------------------------------------------------
    # Parsing helpers (prefix commands)
    # ------------------------------------------------------------------
    @staticmethod
    def _split_target(text: str) -> Optional[str]:
        """Return the raw member id when the first word targets someone."""
        first, _, rest = text.partition(" ")
        match = MENTION_OR_ID.match(first.strip())
        if not match:
            return None
        return match.group(1) or match.group(2)

    async def _resolve_member(self, guild: discord.Guild, raw_id: str) -> discord.Member:
        member = guild.get_member(int(raw_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(raw_id))
        except discord.NotFound:
            raise NickError("That member is not in this server.")
        except Exception as e:
            raise NickError(f"I could not look that member up: {type(e).__name__}")

    @staticmethod
    def _parse_nickname(raw: str) -> Optional[str]:
        """Map the reset words to ``None`` so the nickname goes back to the username."""
        return None if raw.strip().lower() in RESET_WORDS else raw

    # ------------------------------------------------------------------
    # .nick  (prefix)
    # ------------------------------------------------------------------
    @commands.command(name="nick", aliases=["nickname", "setnick"],
                      help="Change a server nickname: .nick <nickname> | .nick <@member> <nickname>")
    @commands.guild_only()
    async def nick_prefix(self, ctx: commands.Context, *, text: str = ""):
        text = text.strip()
        if not text:
            await self._send(ctx, embed=self.usage_embed())
            return

        try:
            raw_id = self._split_target(text)
            # ".nick 2026" (a bare number and nothing after it) is a nickname,
            # not a member id. Numbers are only treated as member ids when the
            # target + nickname form is used: ".nick <@id>/<id> <nickname>".
            if raw_id is not None and not text.isdigit():
                _, _, nickname = text.partition(" ")
                target = await self._resolve_member(ctx.guild, raw_id)
                if not nickname.strip():
                    raise NickError(
                        f"You mentioned {target.display_name} but gave no nickname. "
                        f"Usage: `.nick <@member> <nickname>`"
                    )
            else:
                target = ctx.author
                nickname = text

            embed = await self.change_nickname(
                ctx.guild, ctx.author, target, self._parse_nickname(nickname)
            )
        except NickError as e:
            await self._send(ctx, content=f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"[Nick] .nick failed: {type(e).__name__}: {e}", exc_info=True)
            await self._send(ctx, content="❌ Something went wrong changing that nickname.")
            return

        await self._send(ctx, embed=embed)

    # ------------------------------------------------------------------
    # /nick  (slash)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="nick",
        description="Change a server nickname - yours by default, or another member's",
    )
    @app_commands.describe(
        member="Member to rename (leave empty to rename yourself)",
        nickname="New nickname, up to 32 characters - type 'clear' to reset it",
    )
    async def nick_slash(self, interaction: discord.Interaction,
                         member: Optional[discord.Member] = None,
                         nickname: Optional[str] = None):
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if not nickname or not nickname.strip():
            await self._send(interaction, embed=self.usage_embed(), ephemeral=True)
            return

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message(
                "That user is not a member of this server.", ephemeral=True
            )
            return

        # change_nickname does a Mongo config read (and possibly a log send)
        # before we can reply - defer so the 3s interaction deadline cannot be
        # missed. The final answer goes out as a followup via _send.
        await interaction.response.defer(
            ephemeral=(target.id == interaction.user.id)
        )

        try:
            embed = await self.change_nickname(
                interaction.guild, interaction.user, target, self._parse_nickname(nickname)
            )
        except NickError as e:
            await self._send(interaction, content=f"❌ {e}", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Nick] /nick failed: {type(e).__name__}: {e}", exc_info=True)
            await self._send(
                interaction, content="❌ Something went wrong changing that nickname.",
                ephemeral=True,
            )
            return

        # Personal changes stay private; renaming someone else is shown publicly
        # so the server can see what happened.
        await self._send(interaction, embed=embed, ephemeral=(target.id == interaction.user.id))

    # ------------------------------------------------------------------
    # .nicksetup  (prefix)
    # ------------------------------------------------------------------
    @commands.command(name="nicksetup", aliases=["nickconfig", "nicksettings"],
                      help="Configure nickname management: .nicksetup [role|log|clear] [value]")
    @commands.guild_only()
    async def nicksetup_prefix(self, ctx: commands.Context, action: str = "show", *,
                               target: str = ""):
        if not ctx.author.guild_permissions.administrator:
            await self._send(ctx, content="❌ You need **Administrator** to set this up.")
            return

        action = action.lower()
        target = target.strip()
        config = await self.get_config(ctx.guild.id)
        changed = False
        changed_keys: List[str] = []

        try:
            if action in {"role", "setrole"}:
                role = self._resolve_role(ctx.guild, target)
                if role is None:
                    raise NickError(
                        "Give me a role: `.nicksetup role @Role` (mention, ID or exact name)."
                    )
                if role.is_default():
                    raise NickError(
                        "That is `@everyone` - pick a real role so only its members can rename people."
                    )
                config["nick_role_id"] = role.id
                changed_keys.append("nick_role_id")
                changed = True

            elif action in {"log", "channel", "logchannel"}:
                if target.lower() in {"off", "none", "disable", "0", "clear", "-"}:
                    config["log_channel_id"] = None
                else:
                    channel = self._resolve_channel(ctx.guild, target)
                    if channel is None:
                        raise NickError(
                            "Give me a channel: `.nicksetup log #audit` "
                            "(or `.nicksetup log off` to disable)."
                        )
                    config["log_channel_id"] = channel.id
                changed_keys.append("log_channel_id")
                changed = True

            elif action in {"clear", "unset"}:
                what = (target or "all").lower()
                if what not in {"role", "log", "channel", "logchannel", "all"}:
                    raise NickError("Usage: `.nicksetup clear <role|log|all>`")
                if what in {"role", "all"}:
                    config["nick_role_id"] = None
                    changed_keys.append("nick_role_id")
                if what in {"log", "channel", "logchannel", "all"}:
                    config["log_channel_id"] = None
                    changed_keys.append("log_channel_id")
                changed = True

            elif action in {"show", "view", "config", "help"}:
                pass
            else:
                raise NickError(
                    "Unknown option. Usage: `.nicksetup [role <@role>] [log <#channel>] "
                    "[clear <role|log|all>] [show]`"
                )

            if changed and not await self.save_config(config, changed_keys):
                raise NickError(
                    "I could not save that to the database (MongoDB is unavailable)."
                )
        except NickError as e:
            await self._send(ctx, content=f"❌ {e}")
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

    @staticmethod
    def _resolve_channel(guild: discord.Guild, raw: str) -> Optional[discord.abc.GuildChannel]:
        raw = raw.strip()
        if not raw:
            return None
        match = CHANNEL_MENTION.match(raw)
        if match:
            channel = guild.get_channel(int(match.group(1)))
            return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None
        if raw.isdigit() and len(raw) >= 17:
            channel = guild.get_channel(int(raw))
            return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None
        lowered = raw.lower()
        return next(
            (c for c in guild.text_channels if c.name.lower() == lowered),
            None,
        )

    # ------------------------------------------------------------------
    # /nicksetup  (slash)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="nicksetup",
        description="Configure who may rename members, and where changes are logged",
    )
    @app_commands.describe(
        role="Role allowed to rename other members",
        log_channel="Channel that records nickname changes",
        action="Show or clear the current configuration",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Show configuration", value="show"),
        app_commands.Choice(name="Clear the role", value="clear_role"),
        app_commands.Choice(name="Clear the log channel", value="clear_log"),
        app_commands.Choice(name="Clear everything", value="clear_all"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def nicksetup_slash(self, interaction: discord.Interaction,
                              role: Optional[discord.Role] = None,
                              log_channel: Optional[discord.TextChannel] = None,
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

        # Mongo read/write happen before the reply - defer first so the 3s
        # interaction deadline cannot be missed; responses go via followup.
        await interaction.response.defer(ephemeral=True)

        config = await self.get_config(interaction.guild.id)
        changed = False
        changed_keys: List[str] = []

        if action in {"clear_role", "clear_all"}:
            config["nick_role_id"] = None
            changed_keys.append("nick_role_id")
            changed = True
        if action in {"clear_log", "clear_all"}:
            config["log_channel_id"] = None
            changed_keys.append("log_channel_id")
            changed = True

        if role is not None:
            if role.is_default():
                await self._send(
                    interaction,
                    content="❌ That is `@everyone` - pick a real role so only its members can rename people.",
                    ephemeral=True,
                )
                return
            config["nick_role_id"] = role.id
            changed_keys.append("nick_role_id")
            changed = True
        if log_channel is not None:
            config["log_channel_id"] = log_channel.id
            changed_keys.append("log_channel_id")
            changed = True

        if changed and not await self.save_config(config, changed_keys):
            await self._send(
                interaction,
                content="❌ I could not save that to the database (MongoDB is unavailable).",
                ephemeral=True,
            )
            return

        await self._send(interaction, embed=self.config_embed(interaction.guild, config))

    # ------------------------------------------------------------------
    # Local error handlers - guild_only raises NoPrivateMessage in DMs, which
    # the global handler ignores silently, leaving the user with no reply.
    # ------------------------------------------------------------------
    @nick_prefix.error
    @nicksetup_prefix.error
    async def _nick_prefix_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NoPrivateMessage):
            try:
                await ctx.send("❌ Nickname commands only work in a server.")
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NickCog(bot))
