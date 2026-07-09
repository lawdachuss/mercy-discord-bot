import os
import asyncio
import json
import time
import re
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

import discord
from discord.ext import commands
from discord import app_commands
from discord import Embed, Colour
from dotenv import load_dotenv
import motor.motor_asyncio

# Load env
load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing from .env")

# ---------- Defaults & constants ----------
DEFAULT_SKULL_EMOJI = "💀"
DEFAULT_THRESHOLD = 2
EMBED_COLOR = 0x2f3136  # #2f3136
DB_NAME = os.getenv("MONGO_DB_NAME", "skullboard_db")
COLL_CONFIG = "configs"
COLL_MSGMAP = "message_maps"
COLL_STATS = "stats"
COLL_BLACKLIST = "blacklists"

ANTI_SPAM_SECONDS = 3  # ignore rapid toggles by same user within this
MILESTONE_LEVELS = [10, 25, 50]  # triggers for milestone announcement (can change)
THREAD_NAME_TEMPLATE = "💀 {author}'s Perfect Record"  # final thread name template

# 20 dark/edgy footer quotes (randomized)
FOOTER_QUOTES = [
    "The dead remember their favorites.",
    "Buried in skulls, but never forgotten.",
    "Legends live in bones.",
    "One more for the graveyard.",
    "Memento mori.",
    "A skull for a memory.",
    "From dust to glory.",
    "They rise in stories, not bones.",
    "A monument of memes and bones.",
    "The deeper the grave, the sharper the wit.",
    "Where skulls gather, legends form.",
    "Quiet now — the skulls are listening.",
    "A famous skull never fades.",
    "Collect the fallen; collect the fame.",
    "In death, memes become immortal.",
    "Bone by bone, a hall of fame.",
    "Crowned by reaction and rumor.",
    "Marked by skulls, remembered by names.",
    "A small skull for a giant laugh.",
    "Skulls whisper histories."
]

# ---------- Mongo client ----------
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo_client[DB_NAME]

# ensure indexes (run on import - idempotent)
async def ensure_indexes():
    await db[COLL_MSGMAP].create_index("guild_id")
    await db[COLL_MSGMAP].create_index("orig_message_id")
    await db[COLL_STATS].create_index("guild_id")
    await db[COLL_CONFIG].create_index("guild_id", unique=True)
    await db[COLL_BLACKLIST].create_index("guild_id")
# schedule ensure later from cog

# ---------- Helpers ----------
def normalize_emoji_input(emoji_str: str) -> str:
    """
    Normalize emoji input from admin into a canonical comparable string.
    Accepts unicode, <a:name:id>, <:name:id>, or raw name:id.
    """
    if not emoji_str:
        return DEFAULT_SKULL_EMOJI
    s = emoji_str.strip()
    if s.startswith("<") and s.endswith(">") and ":" in s:
        # <a:name:id> or <:name:id>
        parts = s.strip("<>").split(":")
        if len(parts) == 3:
            name = parts[1]
            eid = parts[2]
            return f"<:{name}:{eid}>"
    if ":" in s and s.count(":") == 1:
        name, eid = s.split(":")
        if eid.isdigit():
            return f"<:{name}:{eid}>"
    return s  # fallback, likely unicode

def pick_footer(guild_name: str) -> str:
    import random
    quote = random.choice(FOOTER_QUOTES)
    return quote  # Now only returns the quote without the guild name

def is_custom_emoji_form(s: str) -> bool:
    return s.startswith("<:") or s.startswith("<a:")

def emoji_matches_cfg(react_emoji: Any, cfg_emoji: str) -> bool:
    """
    Compare a discord reaction emoji (could be Emoji object or str) to stored cfg representation.
    """
    try:
        rep = str(react_emoji)
    except Exception:
        rep = None
    # react_emoji may be an object with id/name
    if getattr(react_emoji, "id", None):
        name = getattr(react_emoji, "name", None) or ""
        eid = getattr(react_emoji, "id", None)
        # forms
        if cfg_emoji in {f"<:{name}:{eid}>", f"<a:{name}:{eid}>", rep}:
            return True
    # fallback compare string forms
    return rep == cfg_emoji

def is_emoji_only_message(message: discord.Message) -> bool:
    """
    Check if a message contains only emojis (unicode or custom) and no other text.
    Returns True if the message should be ignored.
    Note: GIFs and other media are allowed and will NOT be ignored.
    """
    content = message.content.strip()
    
    # If there's no content but there are attachments (like GIFs), allow them
    if not content:
        if message.attachments:
            # Allow messages with attachments (GIFs, images, videos, etc.)
            return False
        # No content and no attachments - allow (could be embed-only message)
        return False
    
    # Remove custom Discord emojis (<:name:id> or <a:name:id>)
    content_no_custom = re.sub(r'<a?:[^:]+:\d+>', '', content)
    
    # Remove unicode emojis using regex pattern
    # This pattern matches most unicode emoji ranges
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FA6F"  # extended symbols
        "\U00002600-\U000026FF"  # misc symbols
        "\U00002700-\U000027BF"
        "]+", flags=re.UNICODE
    )
    content_no_emojis = emoji_pattern.sub('', content_no_custom)
    
    # Remove whitespace
    content_cleaned = content_no_emojis.strip()
    
    # If nothing remains, it was emoji-only (ignore these)
    # But if there are attachments with the emojis, allow it
    if len(content_cleaned) == 0:
        # If there are attachments along with emojis, allow the message
        if message.attachments:
            return False
        # Pure emoji-only text with no attachments - ignore
        return True
    
    return False

@dataclass
class SkullConfig:
    guild_id: int
    channel_id: int
    emoji: str = DEFAULT_SKULL_EMOJI
    threshold: int = DEFAULT_THRESHOLD
    autothread: bool = False
    milestones_enabled: bool = True
    style: str = "detailed"  # or 'compact'
    created_by: int = 0
    # blacklists stored elsewhere

    def to_doc(self):
        return {
            "guild_id": str(self.guild_id),
            "channel_id": str(self.channel_id),
            "emoji": self.emoji,
            "threshold": int(self.threshold),
            "autothread": bool(self.autothread),
            "milestones_enabled": bool(self.milestones_enabled),
            "style": self.style,
            "created_by": str(self.created_by)
        }

    @classmethod
    def from_doc(cls, doc: Dict[str, Any]):
        if not doc:
            return None
        return cls(
            guild_id=int(doc["guild_id"]),
            channel_id=int(doc["channel_id"]),
            emoji=doc.get("emoji", DEFAULT_SKULL_EMOJI),
            threshold=int(doc.get("threshold", DEFAULT_THRESHOLD)),
            autothread=bool(doc.get("autothread", False)),
            milestones_enabled=bool(doc.get("milestones_enabled", True)),
            style=doc.get("style", "detailed"),
            created_by=int(doc.get("created_by", 0))
        )

# ---------- Cog ----------
class Skullboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # locks per guild to avoid race conditions
        self._locks: Dict[int, asyncio.Lock] = {}
        # in-memory caches (simple)
        self._config_cache: Dict[int, SkullConfig] = {}
        # mapping cache: guild -> orig_msg_id -> skull_msg_id
        self._map_cache: Dict[int, Dict[int, int]] = {}
        # blacklist cache: guild -> {"users": [...], "channels": [...]}
        self._blacklist_cache: Dict[int, Dict[str, List[int]]] = {}
        # last action timestamps to prevent spam toggles: (guild, user, message) -> ts
        self._recent_actions: Dict[Tuple[int, int, int], float] = {}
        # Maximum items in caches
        self.MAX_CACHE_ITEMS = 1000
        # Cache TTL in seconds (5 minutes)
        self.CACHE_TTL = 300
        # Cache last update timestamps
        self._cache_timestamps: Dict[str, Dict[int, float]] = {
            "blacklist": {},
            "config": {},
            "mapping": {}
        }
        
        # run ensure indexes and start cleanup task
        bot.loop.create_task(ensure_indexes())
        bot.loop.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        """Periodically clean up caches to prevent memory leaks"""
        while True:
            await asyncio.sleep(300)  # Run every 5 minutes
            await self._cleanup_action_cache()
            await self._cleanup_caches()

    async def _cleanup_action_cache(self):
        """Clean up old entries from recent actions cache"""
        now = time.time()
        self._recent_actions = {
            k: v for k, v in self._recent_actions.items()
            if now - v < ANTI_SPAM_SECONDS * 2
        }

    async def _cleanup_caches(self):
        """Trim caches to prevent excessive memory usage"""
        for cache in [self._config_cache, self._map_cache]:
            if len(cache) > self.MAX_CACHE_ITEMS:
                # Remove oldest entries
                while len(cache) > self.MAX_CACHE_ITEMS * 0.8:  # Remove 20% when full
                    cache.pop(next(iter(cache)))

    def get_lock(self, guild_id:int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    # ----------------- DB helpers -----------------
    async def load_config(self, guild_id: int) -> Optional[SkullConfig]:
        if guild_id in self._config_cache:
            return self._config_cache[guild_id]
        doc = await db[COLL_CONFIG].find_one({"guild_id": str(guild_id)})
        if not doc:
            return None
        cfg = SkullConfig.from_doc(doc)
        self._config_cache[guild_id] = cfg
        return cfg

    async def save_config(self, cfg: SkullConfig):
        await db[COLL_CONFIG].update_one(
            {"guild_id": str(cfg.guild_id)},
            {"$set": cfg.to_doc()},
            upsert=True
        )
        self._config_cache[cfg.guild_id] = cfg

    async def delete_config(self, guild_id: int):
        await db[COLL_CONFIG].delete_one({"guild_id": str(guild_id)})
        self._config_cache.pop(guild_id, None)
        self._map_cache.pop(guild_id, None)
        await db[COLL_MSGMAP].delete_many({"guild_id": str(guild_id)})
        await db[COLL_STATS].delete_many({"guild_id": str(guild_id)})
        await db[COLL_BLACKLIST].delete_one({"guild_id": str(guild_id)})

    async def get_map(self, guild_id: int) -> Dict[int,int]:
        # load from cache if present
        if guild_id in self._map_cache:
            return self._map_cache[guild_id]
        cursor = db[COLL_MSGMAP].find({"guild_id": str(guild_id)})
        mapping = {}
        async for doc in cursor:
            orig = int(doc["orig_message_id"])
            skull = int(doc["skull_message_id"])
            mapping[orig] = skull
        self._map_cache[guild_id] = mapping
        return mapping

    async def set_mapping(self, guild_id:int, orig_msg_id:int, skull_msg_id:int):
        await db[COLL_MSGMAP].update_one(
            {"guild_id": str(guild_id), "orig_message_id": str(orig_msg_id)},
            {"$set": {"skull_message_id": str(skull_msg_id)}},
            upsert=True
        )
        # update cache
        m = await self.get_map(guild_id)
        m[orig_msg_id] = skull_msg_id

    async def remove_mapping(self, guild_id:int, orig_msg_id:int):
        await db[COLL_MSGMAP].delete_one({"guild_id": str(guild_id), "orig_message_id": str(orig_msg_id)})
        m = await self.get_map(guild_id)
        if orig_msg_id in m:
            del m[orig_msg_id]

    # stats: authors and reactors with validation
    async def incr_author_score(self, guild_id:int, author_id:int, by:int=1):
        if not isinstance(by, int) or by == 0:
            return
        try:
            # Ensure we're not going below 0
            current = await db[COLL_STATS].find_one(
                {"guild_id": str(guild_id), "type": "author", "user_id": str(author_id)}
            )
            current_score = int(current["score"]) if current else 0
            
            if current_score + by < 0:
                by = -current_score  # Prevent going negative
                
            if by != 0:
                await db[COLL_STATS].update_one(
                    {"guild_id": str(guild_id), "type": "author", "user_id": str(author_id)},
                    {"$inc": {"score": int(by)}},
                    upsert=True
                )
        except Exception as e:
            print(f"Error updating author score for {author_id} in guild {guild_id}: {e}")

    async def incr_reactor_score(self, guild_id:int, reactor_id:int, by:int=1):
        if not isinstance(by, int) or by == 0:
            return
        try:
            # Ensure we're not going below 0
            current = await db[COLL_STATS].find_one(
                {"guild_id": str(guild_id), "type": "reactor", "user_id": str(reactor_id)}
            )
            current_score = int(current["score"]) if current else 0
            
            if current_score + by < 0:
                by = -current_score  # Prevent going negative
                
            if by != 0:
                await db[COLL_STATS].update_one(
                    {"guild_id": str(guild_id), "type": "reactor", "user_id": str(reactor_id)},
                    {"$inc": {"score": int(by)}},
                    upsert=True
                )
        except Exception as e:
            print(f"Error updating reactor score for {reactor_id} in guild {guild_id}: {e}")

    async def get_top_stats(self, guild_id:int, stat_type:str, limit:int=10) -> List[Tuple[int,int]]:
        cursor = db[COLL_STATS].find({"guild_id": str(guild_id), "type": stat_type}).sort("score", -1).limit(limit)
        out = []
        async for d in cursor:
            out.append((int(d["user_id"]), int(d.get("score",0))))
        return out

    # blacklist CRUD (single document per guild with lists)
    async def get_blacklist(self, guild_id: int) -> Dict[str, List[int]]:
        # Check cache first
        now = time.time()
        last_update = self._cache_timestamps["blacklist"].get(guild_id, 0)
        
        if guild_id in self._blacklist_cache and now - last_update < self.CACHE_TTL:
            return self._blacklist_cache[guild_id]
            
        # Fetch from database
        doc = await db[COLL_BLACKLIST].find_one({"guild_id": str(guild_id)})
        result = {
            "channels": [int(c) for c in doc.get("channels", [])] if doc else [],
            "users": [int(u) for u in doc.get("users", [])] if doc else []
        }
        
        # Update cache
        self._blacklist_cache[guild_id] = result
        self._cache_timestamps["blacklist"][guild_id] = now
        return result

    async def toggle_blacklist_user(self, guild_id: int, user_id: int) -> bool:
        bl = await self.get_blacklist(guild_id)
        if user_id in bl["users"]:
            # remove
            await db[COLL_BLACKLIST].update_one(
                {"guild_id": str(guild_id)}, 
                {"$pull": {"users": str(user_id)}}
            )
            if guild_id in self._blacklist_cache:
                self._blacklist_cache[guild_id]["users"].remove(user_id)
            return False
        else:
            # add
            await db[COLL_BLACKLIST].update_one(
                {"guild_id": str(guild_id)}, 
                {"$addToSet": {"users": str(user_id)}},
                upsert=True
            )
            if guild_id in self._blacklist_cache:
                self._blacklist_cache[guild_id]["users"].append(user_id)
            return True

    async def toggle_blacklist_channel(self, guild_id: int, channel_id: int) -> bool:
        bl = await self.get_blacklist(guild_id)
        if channel_id in bl["channels"]:
            # remove
            await db[COLL_BLACKLIST].update_one(
                {"guild_id": str(guild_id)}, 
                {"$pull": {"channels": str(channel_id)}}
            )
            if guild_id in self._blacklist_cache:
                self._blacklist_cache[guild_id]["channels"].remove(channel_id)
            return False
        else:
            # add
            await db[COLL_BLACKLIST].update_one(
                {"guild_id": str(guild_id)}, 
                {"$addToSet": {"channels": str(channel_id)}},
                upsert=True
            )
            if guild_id in self._blacklist_cache:
                self._blacklist_cache[guild_id]["channels"].append(channel_id)
            return True

    # ----------------- Embed builder -----------------
    def build_skull_embed(self, message:discord.Message, count:int, guild:discord.Guild, qualified:bool, cfg:SkullConfig) -> Embed:
        author = message.author
        embed = Embed(
            colour=Colour(EMBED_COLOR),
            timestamp=message.created_at
        )
        embed.set_author(name=str(author), icon_url=author.display_avatar.url if author.display_avatar else None)
        
        # Handle attachments first to check for media
        has_media = False
        
        # Check for Tenor GIFs and other embeds first (like from links)
        if message.embeds:
            for msg_embed in message.embeds:
                # Check if it's a Tenor GIF or other media embed
                if msg_embed.type in ['gifv', 'image', 'video'] and not has_media:
                    # Try to get the media URL from various embed fields
                    media_url = None
                    if msg_embed.thumbnail and msg_embed.thumbnail.url:
                        media_url = msg_embed.thumbnail.url
                    elif msg_embed.image and msg_embed.image.url:
                        media_url = msg_embed.image.url
                    elif msg_embed.video and msg_embed.video.url:
                        media_url = msg_embed.video.url
                    
                    if media_url:
                        embed.set_image(url=media_url)
                        has_media = True
                        break
        
        atts = message.attachments
        if atts and not has_media:
            MAX_ATTACHMENTS = 5  # Limit number of attachment fields
            for idx, a in enumerate(atts[:MAX_ATTACHMENTS]):
                try:
                    # Check if it's a video - videos should be embedded first
                    if not has_media and a.content_type and a.content_type.startswith("video"):
                        # Videos are embedded using set_image (Discord handles video playback)
                        embed.set_image(url=a.url)
                        has_media = True
                    # Check by file extension for videos
                    elif not has_media and a.filename and a.filename.lower().endswith(('.mp4', '.mov', '.avi', '.webm', '.mkv')):
                        embed.set_image(url=a.url)
                        has_media = True
                    # Check if it's an image or GIF - both should be embedded
                    elif not has_media and a.content_type and (a.content_type.startswith("image") or "gif" in a.content_type.lower()):
                        embed.set_image(url=a.url)
                        has_media = True
                    # Also check by file extension for images/GIFs
                    elif not has_media and a.filename and a.filename.lower().endswith(('.gif', '.png', '.jpg', '.jpeg', '.webp')):
                        embed.set_image(url=a.url)
                        has_media = True
                    else:
                        # For non-media attachments, show as clickable link
                        embed.add_field(name=f"Attachment #{idx+1}", value=f"[{a.filename}]({a.url})", inline=False)
                except Exception as e:
                    print(f"Error processing attachment {a.filename}: {e}")
                    continue
            # Fallback: if no media was set but we have attachments, try to embed the first one
            if not has_media and atts:
                try:
                    embed.set_image(url=atts[0].url)
                    has_media = True
                except Exception as e:
                    print(f"Error setting default image: {e}")
            if len(atts) > MAX_ATTACHMENTS:
                embed.add_field(name="Note", value=f"+ {len(atts) - MAX_ATTACHMENTS} more attachments", inline=False)
        
        # Show message content in description (not title) so mentions render properly
        if message.content:
            # Put content in description so mentions/formatting render properly
            content_display = message.content[:2000] if len(message.content) > 2000 else message.content
            embed.description = f"{content_display}\n\n[Click to jump to message!]({message.jump_url})"
        elif has_media:
            # For media-only posts (no text), just show the jump link without "(no text)"
            embed.description = f"[Click to jump to message!]({message.jump_url})"
        else:
            # No content and no media - this shouldn't normally happen but handle it gracefully
            embed.description = f"[Click to jump to message!]({message.jump_url})"
        
        # footer
        footer_text = pick_footer(guild.name)
        embed.set_footer(text=footer_text)
        if not qualified:
            embed.add_field(name="Status", value="No longer qualifies (below threshold)", inline=False)
        return embed

    # ----------------- Core reaction + message handlers -----------------
    async def process_reaction_change(self, payload:discord.RawReactionActionEvent):
        """
        Called for both add/remove via raw events. Decide whether to create/update/delete.
        Includes rate limit and error handling.
        """
        # Add basic rate limiting per guild
        guild_id = payload.guild_id
        rate_key = f"rate_limit_{guild_id}"
        now = time.time()
        if hasattr(self, rate_key) and now - getattr(self, rate_key) < 1.0:  # 1 second cooldown per guild
            return
        setattr(self, rate_key, now)
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        cfg = await self.load_config(guild.id)
        if not cfg:
            return
        # check blacklist
        bl = await self.get_blacklist(guild.id)
        if payload.user_id in bl["users"]:
            return
        if payload.channel_id in bl["channels"]:
            return

        # check emoji match
        cfg_emoji = cfg.emoji
        # build possible forms from payload.emoji
        forms = set()
        try:
            forms.add(str(payload.emoji))
        except Exception:
            pass
        if getattr(payload.emoji, "id", None):
            nm = getattr(payload.emoji, "name", "")
            forms.add(f"<:{nm}:{payload.emoji.id}>")
            forms.add(f"<a:{nm}:{payload.emoji.id}>")
        if cfg_emoji not in forms:
            return

        # anti-spam: ignore if same user toggled on same message within ANTI_SPAM_SECONDS
        key = (guild.id, payload.user_id, payload.message_id)
        now = time.time()
        last = self._recent_actions.get(key, 0)
        if now - last < ANTI_SPAM_SECONDS:
            return
        self._recent_actions[key] = now

        # fetch message
        try:
            channel = guild.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            # If message isn't found, check if mapping exists and remove
            mapped = await self.get_map(guild.id)
            if payload.message_id in mapped:
                # likely deleted
                skull_ch = guild.get_channel(cfg.channel_id)
                if skull_ch:
                    try:
                        skull_msg = await skull_ch.fetch_message(mapped[payload.message_id])
                        await skull_msg.delete()
                    except Exception:
                        pass
                await self.remove_mapping(guild.id, payload.message_id)
            return

        # ignore bot messages
        if message.author and message.author.bot:
            return
        
        # ignore emoji-only and GIF-only messages
        if is_emoji_only_message(message):
            return

        async with self.get_lock(guild.id):
            # compute current count for the configured emoji on that message
            current_count = 0
            matching_reaction = None
            for r in message.reactions:
                try:
                    if emoji_matches_cfg(r.emoji, cfg_emoji):
                        matching_reaction = r
                        current_count = r.count
                        break
                except Exception:
                    continue
            mapping = await self.get_map(guild.id)
            skull_channel = guild.get_channel(cfg.channel_id)
            if not skull_channel:
                # configured channel missing -> skip
                return
            exists_skull_msg_id = mapping.get(message.id)

            if current_count >= cfg.threshold:
                # create or update skullboard post
                embed = self.build_skull_embed(message, current_count, guild, qualified=True, cfg=cfg)
                try:
                    if exists_skull_msg_id:
                        # update
                        try:
                            skull_msg = await skull_channel.fetch_message(exists_skull_msg_id)
                            count_msg = f"**{current_count}** {cfg.emoji} | {message.channel.mention}"
                            try:
                                await skull_msg.edit(content=count_msg, embed=embed)
                            except discord.HTTPException as e:
                                if e.status == 429:  # Rate limited
                                    print(f"Rate limited while updating skull message {exists_skull_msg_id}, will retry later")
                                    return
                                raise
                        except discord.NotFound:
                            # create new if missing
                            count_msg = f"**{current_count}** {cfg.emoji} | {message.channel.mention}"
                            sent = await skull_channel.send(content=count_msg, embed=embed)
                            # Add skull reaction to the new post
                            try:
                                await sent.add_reaction(cfg.emoji)
                            except Exception as e:
                                print(f"Failed to add skull reaction: {e}")
                            await self.set_mapping(guild.id, message.id, sent.id)
                            # increment author stat
                            await self.incr_author_score(guild.id, message.author.id, by=1)
                            # optional auto-thread
                            if cfg.autothread:
                                await self._maybe_create_thread(sent, message.author)
                            # check milestones
                            if cfg.milestones_enabled:
                                await self._maybe_announce_milestone(guild, cfg, message, current_count)
                        else:
                            # If the message was previously below threshold and now qualifies,
                            # we need to count the stats
                            previous_embed = skull_msg.embeds[0] if skull_msg.embeds else None
                            if previous_embed and "No longer qualifies" in previous_embed.to_dict().get("fields", [{}])[0].get("value", ""):
                                # Message was previously disqualified, now requalifies
                                await self.incr_author_score(guild.id, message.author.id, by=1)
                                if matching_reaction:
                                    try:
                                        async for u in matching_reaction.users():
                                            if not u.bot:
                                                await self.incr_reactor_score(guild.id, u.id, by=1)
                                    except Exception as e:
                                        print(f"Error counting reactors: {e}")
                    else:
                        # create new post with count and channel name first, then embed
                        count_msg = f"**{current_count}** {cfg.emoji} | {message.channel.mention}"
                        sent = await skull_channel.send(content=count_msg, embed=embed)
                        # Add skull reaction to the skullboard post
                        try:
                            await sent.add_reaction(cfg.emoji)
                        except Exception as e:
                            print(f"Failed to add skull reaction: {e}")
                        
                        await self.set_mapping(guild.id, message.id, sent.id)
                        # increment author stat
                        await self.incr_author_score(guild.id, message.author.id, by=1)
                        # count reactors: we add reactor stats by checking reaction users for the configured emoji
                        if matching_reaction:
                            # fetch users who reacted (may be many; limit)
                            try:
                                async for u in matching_reaction.users():
                                    if not u.bot:
                                        await self.incr_reactor_score(guild.id, u.id, by=1)
                            except Exception:
                                pass
                        # autothread
                        if cfg.autothread:
                            await self._maybe_create_thread(sent, message.author)
                        # milestones
                        if cfg.milestones_enabled:
                            await self._maybe_announce_milestone(guild, cfg, message, current_count)
                except Exception:
                    # log
                    print("Failed to create/update skullboard post", exc_info=True)
            else:
                # below threshold: if skullboard post exists, edit to reflect new count and mark as not qualified
                if exists_skull_msg_id:
                    try:
                        skull_msg = await skull_channel.fetch_message(exists_skull_msg_id)
                        embed = self.build_skull_embed(message, current_count, guild, qualified=False, cfg=cfg)
                        try:
                            await skull_msg.edit(embed=embed)
                        except discord.HTTPException as e:
                            if e.status == 429:  # Rate limited
                                print(f"Rate limited while marking skull message {exists_skull_msg_id} as unqualified")
                                return
                            raise
                    except discord.NotFound:
                        # mapping stale; remove
                        await self.remove_mapping(guild.id, message.id)
                    except Exception:
                        pass

    async def _maybe_create_thread(self, skull_msg:discord.Message, original_author:discord.User):
        """
        Create a public thread under the skullboard post (if possible).
        Includes proper error handling and duplicate checking.
        """
        try:
            # Check if message already has a thread
            if hasattr(skull_msg, 'thread') and skull_msg.thread:
                return

            # Check permissions
            channel_perms = skull_msg.channel.permissions_for(skull_msg.guild.me)
            if not channel_perms.create_public_threads:
                print(f"Missing create_public_threads permission in channel {skull_msg.channel.id}")
                return
            if not channel_perms.send_messages_in_threads:
                print(f"Missing send_messages_in_threads permission in channel {skull_msg.channel.id}")
                return

            thread_name = THREAD_NAME_TEMPLATE.format(author=original_author.display_name)
            # create thread (with auto-archive 1 day)
            await skull_msg.create_thread(name=thread_name, auto_archive_duration=1440)
        except discord.Forbidden as e:
            print(f"Forbidden error creating thread for message {skull_msg.id}: {e}")
        except discord.HTTPException as e:
            print(f"HTTP error creating thread for message {skull_msg.id}: {e}")
        except Exception as e:
            print(f"Unexpected error creating thread for message {skull_msg.id}: {e}")
            return None

    async def _rebuild_skullboard(self, guild: discord.Guild, cfg: SkullConfig) -> Tuple[int, int]:
        """
        Safely rebuild skullboard entries with proper error handling and progress tracking.
        Returns (updated_count, removed_count).
        """
        updated = 0
        removed = 0
        
        async with self.get_lock(guild.id):
            mapping = await self.get_map(guild.id)
            for orig_id, skull_id in list(mapping.items()):
                try:
                    # Find original message
                    orig_msg = None
                    for ch in guild.text_channels:
                        try:
                            orig_msg = await ch.fetch_message(orig_id)
                            if orig_msg:
                                break
                        except discord.NotFound:
                            continue
                        except Exception as e:
                            print(f"Error checking channel {ch.id} for message {orig_id}: {e}")
                            continue
                    
                    if not orig_msg:
                        # Clean up if original message is gone
                        skull_ch = guild.get_channel(cfg.channel_id)
                        if skull_ch:
                            try:
                                skull_msg = await skull_ch.fetch_message(skull_id)
                                await skull_msg.delete()
                            except discord.NotFound:
                                pass
                            except Exception as e:
                                print(f"Error deleting skull message {skull_id}: {e}")
                        await self.remove_mapping(guild.id, orig_id)
                        removed += 1
                        continue
                    
                    # Recount reactions
                    count = 0
                    for r in orig_msg.reactions:
                        try:
                            if emoji_matches_cfg(r.emoji, cfg.emoji):
                                count = r.count
                                break
                        except Exception:
                            continue
                    
                    # Update skull message
                    skull_ch = guild.get_channel(cfg.channel_id)
                    if skull_ch:
                        try:
                            skull_msg = await skull_ch.fetch_message(skull_id)
                            embed = self.build_skull_embed(orig_msg, count, guild, count >= cfg.threshold, cfg)
                            await skull_msg.edit(embed=embed)
                            updated += 1
                        except discord.NotFound:
                            # If skull message is gone but original exists and qualifies, recreate it
                            if count >= cfg.threshold:
                                new_skull_msg = await skull_ch.send(embed=embed)
                                await self.set_mapping(guild.id, orig_id, new_skull_msg.id)
                                updated += 1
                            else:
                                await self.remove_mapping(guild.id, orig_id)
                                removed += 1
                        except Exception as e:
                            print(f"Error updating skull message {skull_id}: {e}")
                            
                except Exception as e:
                    print(f"Error processing mapping {orig_id} -> {skull_id}: {e}")
                    continue
                    
        return updated, removed

    async def _maybe_announce_milestone(self, guild: discord.Guild, cfg: SkullConfig, message: discord.Message, current_count: int):
        if not cfg.milestones_enabled:
            return
            
        # Store milestone announcements in cache to prevent duplicates
        cache_key = f"milestone_{message.id}"
        last_milestone = self._config_cache.get(cache_key, 0)
        
        # Find the highest milestone level achieved
        achieved_milestone = None
        for level in sorted(MILESTONE_LEVELS):
            if current_count >= level > last_milestone:
                achieved_milestone = level
                
        if achieved_milestone:
            skull_channel = guild.get_channel(cfg.channel_id)
            if not skull_channel:
                return
                
            try:
                # build simple embed announcement
                ann = Embed(
                    title=f"💀 {message.author.display_name} reached {achieved_milestone} skulls!",
                    description=f"{message.content or '_(no text)_'}\n\n[Jump to message]({message.jump_url})",
                    colour=Colour(EMBED_COLOR),
                    timestamp=discord.utils.utcnow()
                )
                ann.set_footer(text=pick_footer(guild.name))
                
                # Send announcement
                await skull_channel.send(embed=ann)
                
                # Update cache to prevent duplicate announcements
                self._config_cache[cache_key] = achieved_milestone
                
            except discord.Forbidden:
                print(f"Missing permissions to send milestone announcement in channel {skull_channel.id}")
            except Exception as e:
                print(f"Error sending milestone announcement: {e}")
                
            finally:
                # Clean up old milestone cache entries periodically
                if len(self._config_cache) > self.MAX_CACHE_ITEMS:
                    cache_keys = [k for k in self._config_cache.keys() if k.startswith("milestone_")]
                    for k in cache_keys[:len(cache_keys)//2]:  # Remove half of the milestone entries
                        self._config_cache.pop(k, None)

    # ----------------- Event listeners -----------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload:discord.RawReactionActionEvent):
        await self.process_reaction_change(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload:discord.RawReactionActionEvent):
        await self.process_reaction_change(payload)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload:discord.RawMessageDeleteEvent):
        # remove mapping and delete skullboard post if exists
        if not getattr(payload, "guild_id", None):
            return
        guild_id = payload.guild_id
        cfg = await self.load_config(guild_id)
        if not cfg:
            return
        mapping = await self.get_map(guild_id)
        skull_msg_id = mapping.get(payload.message_id)
        if skull_msg_id:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return
            skull_channel = guild.get_channel(cfg.channel_id)
            if skull_channel:
                try:
                    skull_msg = await skull_channel.fetch_message(skull_msg_id)
                    await skull_msg.delete()
                except Exception:
                    pass
            await self.remove_mapping(guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Handle message edits with proper error handling and content change detection"""
        if not getattr(payload, "guild_id", None):
            return
        
        # Add rate limiting per guild (same as reaction handler)
        guild_id = payload.guild_id
        rate_key = f"edit_rate_limit_{guild_id}"
        now = time.time()
        if hasattr(self, rate_key) and now - getattr(self, rate_key) < 1.0:  # 1 second cooldown for edits
            return
        setattr(self, rate_key, now)
            
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
            
        cfg = await self.load_config(guild_id)
        if not cfg:
            return
            
        mapping = await self.get_map(guild_id)
        skull_msg_id = mapping.get(payload.message_id)
        if not skull_msg_id:
            return
            
        async with self.get_lock(guild.id):
            try:
                # Fetch channel and message
                channel = guild.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
                if not channel:
                    print(f"Could not find channel {payload.channel_id} for edited message")
                    return
                    
                message = await channel.fetch_message(payload.message_id)
                if not message:
                    print(f"Could not find edited message {payload.message_id}")
                    return
                    
                # Get current reaction count
                current_count = 0
                matching_reaction = None
                for r in message.reactions:
                    try:
                        if emoji_matches_cfg(r.emoji, cfg.emoji):
                            matching_reaction = r
                            current_count = r.count
                            break
                    except Exception as e:
                        print(f"Error checking reaction: {e}")
                        continue
                        
                skull_channel = guild.get_channel(cfg.channel_id)
                if not skull_channel:
                    print(f"Could not find skullboard channel {cfg.channel_id}")
                    return
                    
                try:
                    skull_msg = await skull_channel.fetch_message(skull_msg_id)
                    qualified = current_count >= cfg.threshold
                    
                    # Update the skull message with retry logic
                    embed = self.build_skull_embed(message, current_count, guild, qualified, cfg)
                    try:
                        await skull_msg.edit(embed=embed)
                    except discord.HTTPException as e:
                        if e.status == 429:  # Rate limited
                            print(f"Rate limited while editing skull message {skull_msg_id}, skipping update")
                            return
                        raise
                    
                except discord.NotFound:
                    # If skull message is missing but should exist, recreate it
                    if current_count >= cfg.threshold:
                        embed = self.build_skull_embed(message, current_count, guild, True, cfg)
                        new_skull_msg = await skull_channel.send(embed=embed)
                        await self.set_mapping(guild.id, message.id, new_skull_msg.id)
                    else:
                        # Remove mapping if message doesn't qualify
                        await self.remove_mapping(guild.id, message.id)
                        
                except discord.Forbidden:
                    print(f"Missing permissions to edit skull message in channel {skull_channel.id}")
                except Exception as e:
                    print(f"Error updating skull message: {e}")
                    
            except discord.NotFound:
                # Original message was deleted
                await self.remove_mapping(guild.id, payload.message_id)
            except discord.Forbidden:
                print(f"Missing permissions to fetch message in channel {payload.channel_id}")
            except Exception as e:
                print(f"Error processing message edit: {e}")

    # ----------------- Slash Commands -----------------
    @app_commands.command(name="setup-skullboard", description="Configure skullboard settings (admin only)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        target_channel="Channel where skullboard posts will appear (required for first setup)",
        emoji="Emoji to use as skull trigger (unicode or custom). Default is 💀",
        threshold="Number of reactions required to post (default 2)",
        style="Embed style: compact or detailed",
        autothread="Auto-create a discussion thread for each skullboard post",
        milestones="Enable/disable milestone announcements (10/25/50)"
    )
    async def skullboard(self,
                  interaction:discord.Interaction,
                  target_channel:Optional[discord.TextChannel]=None,
                  emoji:Optional[str]=None,
                  threshold:Optional[int]=None,
                  style:Optional[str]=None,
                  autothread:Optional[bool]=None,
                  milestones:Optional[bool]=None):
        """Configure skullboard settings for your server."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await interaction.followup.send("This command must be used in a server (guild).", ephemeral=True)

        # load existing config if any
        cfg = await self.load_config(guild.id)

        # Configuration / update path
        if target_channel or emoji or threshold or style is not None or autothread is not None or milestones is not None:
            # must choose channel from provided option or existing config
            if not target_channel and not cfg:
                return await interaction.followup.send("No skullboard configured yet; please provide `target_channel` to set one.", ephemeral=True)
            if not target_channel:
                target_channel = guild.get_channel(cfg.channel_id) if cfg else None
            # permission check
            if target_channel:
                perms = target_channel.permissions_for(guild.me)
                needed = []
                for perm in ("send_messages", "embed_links", "read_message_history"):
                    if not getattr(perms, perm, False):
                        needed.append(perm)
                if needed:
                    return await interaction.followup.send(f"I need the following permissions in {target_channel.mention}: {', '.join(needed)}", ephemeral=True)
            # prepare final config
            if not cfg:
                cfg = SkullConfig(guild_id=guild.id, channel_id=target_channel.id, created_by=interaction.user.id)
            else:
                if target_channel:
                    cfg.channel_id = target_channel.id
            if emoji:
                cfg.emoji = normalize_emoji_input(emoji)
            if threshold and threshold > 0:
                cfg.threshold = threshold
            if style:
                style = style.lower()
                if style in ("compact", "detailed"):
                    cfg.style = style
            if autothread is not None:
                cfg.autothread = autothread
            if milestones is not None:
                cfg.milestones_enabled = milestones
            await self.save_config(cfg)
            return await interaction.followup.send(
                f"✅ **Skullboard Configured!**\n\n"
                f"**Channel:** {target_channel.mention}\n"
                f"**Emoji:** {cfg.emoji}\n"
                f"**Threshold:** {cfg.threshold} reactions\n"
                f"**Style:** {cfg.style}\n"
                f"**Auto-thread:** {cfg.autothread}\n"
                f"**Milestones:** {cfg.milestones_enabled}\n\n"
                f"💡 Use `/skullboard-manage` for leaderboard, blacklist, and other management options.",
                ephemeral=True
            )

        # Show current config if no parameters provided
        if cfg:
            channel = guild.get_channel(cfg.channel_id)
            channel_mention = channel.mention if channel else f"<#{cfg.channel_id}> (deleted)"
            return await interaction.followup.send(
                f"📋 **Current Skullboard Configuration**\n\n"
                f"**Channel:** {channel_mention}\n"
                f"**Emoji:** {cfg.emoji}\n"
                f"**Threshold:** {cfg.threshold} reactions\n"
                f"**Style:** {cfg.style}\n"
                f"**Auto-thread:** {cfg.autothread}\n"
                f"**Milestones:** {cfg.milestones_enabled}\n\n"
                f"💡 Use `/skullboard-manage` for leaderboard, blacklist, rebuild, and reset options.",
                ephemeral=True
            )
        else:
            return await interaction.followup.send(
                f"❌ **No Skullboard Configured**\n\n"
                f"To set up skullboard, use:\n"
                f"`/skullboard target_channel:#your-channel`\n\n"
                f"**Optional parameters:**\n"
                f"• `emoji` - Reaction emoji to track (default: 💀)\n"
                f"• `threshold` - Reactions needed (default: 2)\n"
                f"• `style` - detailed or compact (default: detailed)\n"
                f"• `autothread` - Auto-create threads (default: false)\n"
                f"• `milestones` - Enable milestones (default: true)",
                ephemeral=True
            )

    @app_commands.command(name="skullboard-manage", description="Manage skullboard (leaderboard, blacklist, rebuild, reset)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        action="Choose an action to perform",
        user="User to blacklist/unblacklist (for blacklist action)",
        channel="Channel to blacklist/unblacklist (for blacklist action)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Show Leaderboard", value="leaderboard"),
        app_commands.Choice(name="Blacklist/Unblacklist User", value="blacklist_user"),
        app_commands.Choice(name="Blacklist/Unblacklist Channel", value="blacklist_channel"),
        app_commands.Choice(name="Rebuild/Resync Messages", value="rebuild"),
        app_commands.Choice(name="Reset Configuration", value="reset")
    ])
    async def skullboard_manage(self,
                               interaction: discord.Interaction,
                               action: app_commands.Choice[str],
                               user: Optional[discord.Member] = None,
                               channel: Optional[discord.TextChannel] = None):
        """Manage skullboard settings and data."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await interaction.followup.send("This command must be used in a server (guild).", ephemeral=True)

        cfg = await self.load_config(guild.id)
        
        # Handle reset
        if action.value == "reset":
            if not cfg:
                return await interaction.followup.send("❌ No skullboard configured to reset.", ephemeral=True)
            await self.delete_config(guild.id)
            return await interaction.followup.send("✅ Skullboard configuration and data have been deleted for this server.", ephemeral=True)
        
        # Handle blacklist user
        if action.value == "blacklist_user":
            if not user:
                return await interaction.followup.send("❌ Please provide a user to blacklist/unblacklist.", ephemeral=True)
            toggled_on = await self.toggle_blacklist_user(guild.id, user.id)
            state = "added to" if toggled_on else "removed from"
            return await interaction.followup.send(f"✅ {user.mention} {state} blacklist.", ephemeral=True)
        
        # Handle blacklist channel
        if action.value == "blacklist_channel":
            if not channel:
                return await interaction.followup.send("❌ Please provide a channel to blacklist/unblacklist.", ephemeral=True)
            toggled_on = await self.toggle_blacklist_channel(guild.id, channel.id)
            state = "added to" if toggled_on else "removed from"
            return await interaction.followup.send(f"✅ {channel.mention} {state} blacklist.", ephemeral=True)
        
        # Handle leaderboard
        if action.value == "leaderboard":
            if not cfg:
                return await interaction.followup.send("❌ No skullboard configured for this server.", ephemeral=True)
            top_authors = await self.get_top_stats(guild.id, "author", limit=10)
            top_reactors = await self.get_top_stats(guild.id, "reactor", limit=10)
            embed = Embed(title="💀 Skull Leaderboard", colour=Colour(EMBED_COLOR))
            if top_authors:
                lines = []
                for idx, (uid, score) in enumerate(top_authors, start=1):
                    member = guild.get_member(uid) or await self.bot.fetch_user(uid)
                    name = getattr(member, "display_name", getattr(member, "name", str(uid)))
                    lines.append(f"**{idx}.** {name} — {score} skullposts")
                embed.add_field(name="Top Skullboarded Authors", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="Top Skullboarded Authors", value="No data", inline=False)
            if top_reactors:
                lines = []
                for idx, (uid, score) in enumerate(top_reactors, start=1):
                    member = guild.get_member(uid) or await self.bot.fetch_user(uid)
                    name = getattr(member, "display_name", getattr(member, "name", str(uid)))
                    lines.append(f"**{idx}.** {name} — {score} skulls given")
                embed.add_field(name="Top Skull Reactors", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="Top Skull Reactors", value="No data", inline=False)
            embed.set_footer(text=pick_footer(guild.name))
            return await interaction.followup.send(embed=embed, ephemeral=False)
        
        # Handle rebuild
        if action.value == "rebuild":
            if not cfg:
                return await interaction.followup.send("No skullboard configured for this server to rebuild.", ephemeral=True)
            await interaction.followup.send("Resyncing mapped messages — this may take a moment...", ephemeral=True)
            # For each mapping, fetch original message and recount configured emoji reactions and update skull posts accordingly
            mapping = await self.get_map(guild.id)
            updated = 0
            removed = 0
            async with self.get_lock(guild.id):  # Prevent race conditions during rebuild
                for idx, (orig_id, skull_id) in enumerate(list(mapping.items())):
                    try:
                        # Add delay every 5 messages to avoid rate limits
                        if idx > 0 and idx % 5 == 0:
                            await asyncio.sleep(2)  # 2 second delay every 5 messages
                        
                        # we only attempt to fetch original message if channel exists
                        # We need channel id; the mapping doc doesn't contain it; we will try to search channels for the message
                        # For performance, try to fetch message from guild via channels (expensive) - we'll try known channels by iterating guild.text_channels
                        orig_msg = None
                        for ch in guild.text_channels:
                            try:
                                orig_msg = await ch.fetch_message(orig_id)
                                if orig_msg:
                                    break
                            except discord.NotFound:
                                continue
                            except discord.HTTPException as e:
                                if e.status == 429:  # Rate limited
                                    print(f"Rate limited while searching for message {orig_id}, waiting...")
                                    await asyncio.sleep(5)
                                    continue
                                print(f"HTTP error checking channel {ch.id}: {e}")
                                continue
                            except Exception as e:
                                print(f"Error checking channel {ch.id}: {e}")
                                continue
                                
                        if not orig_msg:
                            # original missing -> delete skull post
                            skull_ch = guild.get_channel(cfg.channel_id)
                            if skull_ch:
                                try:
                                    skull_msg = await skull_ch.fetch_message(skull_id)
                                    await skull_msg.delete()
                                    removed += 1
                                except Exception as e:
                                    print(f"Error deleting skull message {skull_id}: {e}")
                            await self.remove_mapping(guild.id, orig_id)
                            continue
                            
                        # recount reactions
                        count = 0
                        for r in orig_msg.reactions:
                            try:
                                if emoji_matches_cfg(r.emoji, cfg.emoji):
                                    count = r.count
                                    break
                            except Exception as e:
                                print(f"Error checking reaction: {e}")
                                continue
                                
                        # update skull message
                        skull_ch = guild.get_channel(cfg.channel_id)
                        if skull_ch:
                            try:
                                skull_msg = await skull_ch.fetch_message(skull_id)
                                embed = self.build_skull_embed(orig_msg, count, guild, count >= cfg.threshold, cfg)
                                try:
                                    await skull_msg.edit(embed=embed)
                                    updated += 1
                                except discord.HTTPException as e:
                                    if e.status == 429:  # Rate limited
                                        print(f"Rate limited while updating skull message {skull_id}, waiting...")
                                        await asyncio.sleep(5)
                                        # Retry once
                                        try:
                                            await skull_msg.edit(embed=embed)
                                            updated += 1
                                        except Exception:
                                            print(f"Failed to update after rate limit retry: {skull_id}")
                                    else:
                                        raise
                            except discord.NotFound:
                                if count >= cfg.threshold:
                                    # Recreate if message qualifies
                                    embed = self.build_skull_embed(orig_msg, count, guild, True, cfg)
                                    try:
                                        new_skull_msg = await skull_ch.send(embed=embed)
                                        await self.set_mapping(guild.id, orig_id, new_skull_msg.id)
                                        updated += 1
                                    except discord.HTTPException as e:
                                        if e.status == 429:
                                            print(f"Rate limited while recreating skull message for {orig_id}")
                                            await asyncio.sleep(5)
                                        else:
                                            raise
                                else:
                                    await self.remove_mapping(guild.id, orig_id)
                                    removed += 1
                            except Exception as e:
                                print(f"Error updating skull message {skull_id}: {e}")
                                
                    except Exception as e:
                        print(f"Error processing mapping {orig_id} -> {skull_id}: {e}")
                        continue
            return await interaction.followup.send(f"✅ Rebuild completed — updated {updated} skullboard posts, removed {removed}.", ephemeral=True)

# Setup function for loading as extension
async def setup(bot:commands.Bot):
    cog = Skullboard(bot)
    await bot.add_cog(cog)

