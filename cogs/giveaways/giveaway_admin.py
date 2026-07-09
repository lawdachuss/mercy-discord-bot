import discord
import random
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from discord.ext import commands, tasks

# Import from core and config
from cogs.giveaways.giveaway_core import (
    get_current_utc_timestamp, create_fake_participant_id, 
    parse_fake_participant_id, is_fake_participant
)
from cogs.giveaways.config import (
    DOT_EMOJI, RED_DOT_EMOJI, EMBED_COLOR, MIN_FAKE_REACTIONS, MAX_FAKE_REACTIONS, 
    MIN_FAKE_DURATION, MAX_FAKE_DURATION, PRIZE_EMOJI, WINNER_EMOJI, TIME_EMOJI, GIFT_EMOJI
)

class GiveawayAdminCog(commands.Cog):
    """Admin commands for managing giveaways."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger('GiveawayBot')
        self.active_fake_reaction_tasks: Dict[str, asyncio.Task] = {}
        self._ready = asyncio.Event()
        # Rate limiting: track last fill time per guild to prevent spam
        self._guild_fill_cooldowns: Dict[int, float] = {}
        self._fill_cooldown_seconds = 60  # 1 minute cooldown between fills per guild

    async def cog_load(self) -> None:
        """Called when cog is loaded."""
        self.process_fake_reactions.start()
        self._ready.set()
        self.logger.info("[OK] GiveawayAdminCog loaded (commands registered in GiveawayCog)")

    def cog_unload(self) -> None:
        """Called when cog is unloaded."""
        self.process_fake_reactions.cancel()
        for task in self.active_fake_reaction_tasks.values():
            task.cancel()

    @tasks.loop(minutes=1)
    async def process_fake_reactions(self) -> None:
        """Process and resume any active fake reaction plans."""
        await self._ready.wait()
        giveaway_cog = self.bot.get_cog("GiveawayCog")
        if (
            not giveaway_cog
            or not hasattr(giveaway_cog, "db")
            or not giveaway_cog.db.connected
        ):
            return

        try:
            plans = await giveaway_cog.db.fake_reactions.find({"status": "active"}).to_list(length=None)
            for plan in plans:
                mid = plan["message_id"]
                if mid in self.active_fake_reaction_tasks:
                    continue

                # Ensure giveaway still active
                gw = await giveaway_cog.db.giveaways.find_one({
                    "message_id": mid,
                    "status": "active"
                })
                if not gw:
                    # Cancel stale plan
                    await giveaway_cog.db.fake_reactions.update_one(
                        {"message_id": mid},
                        {"$set": {
                            "status": "cancelled",
                            "cancelled_at": get_current_utc_timestamp()
                        }}
                    )
                    continue

                channel = self.bot.get_channel(plan["channel_id"])
                if not channel or not isinstance(channel, discord.TextChannel):
                    # Cancel plan if channel not found
                    await giveaway_cog.db.fake_reactions.update_one(
                        {"message_id": mid},
                        {"$set": {
                            "status": "cancelled",
                            "cancelled_at": get_current_utc_timestamp(),
                            "error": "Channel not found"
                        }}
                    )
                    continue

                if not hasattr(channel, 'guild') or not channel.guild:
                    continue

                members = [str(m.id) for m in channel.guild.members if not m.bot]
                if not members:
                    continue

                remaining = plan["remaining_reactions"]
                end_time = plan["end_time"]
                if remaining > 0 and end_time > get_current_utc_timestamp():
                    task = asyncio.create_task(
                        self.add_fake_reactions(mid, members, plan["total_reactions"], end_time)
                    )
                    self.active_fake_reaction_tasks[mid] = task

        except Exception as e:
            self.logger.error(f"process_fake_reactions error: {e}")

    async def fill(
        self,
        interaction: discord.Interaction,
        message_id: str,
        total_fake_reactions: int,
        duration_in_minutes: int,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            # Rate limiting check
            guild_id = interaction.guild.id if interaction.guild else 0
            current_time = get_current_utc_timestamp()
            last_fill_time = self._guild_fill_cooldowns.get(guild_id, 0)
            
            if current_time - last_fill_time < self._fill_cooldown_seconds:
                remaining = int(self._fill_cooldown_seconds - (current_time - last_fill_time))
                return await interaction.followup.send(
                    f"⏳ Please wait {remaining} seconds before starting another fake fill in this server.",
                    ephemeral=True
                )
            
            # Validate message ID format
            try:
                int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ Invalid message ID format. Please provide a valid numeric message ID.",
                    ephemeral=True
                )
            
            if not (MIN_FAKE_REACTIONS <= total_fake_reactions <= MAX_FAKE_REACTIONS):
                raise ValueError(f"Total fake reactions must be {MIN_FAKE_REACTIONS}–{MAX_FAKE_REACTIONS}.")
            if not (MIN_FAKE_DURATION <= duration_in_minutes <= MAX_FAKE_DURATION):
                raise ValueError(f"Duration must be {MIN_FAKE_DURATION}–{MAX_FAKE_DURATION} minutes.")

            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            gw = await giveaway_cog.db.giveaways.find_one({
                "message_id": message_id,
                "status": "active"
            })
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            # Cancel existing fake fill
            if message_id in self.active_fake_reaction_tasks:
                self.active_fake_reaction_tasks[message_id].cancel()

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                return await interaction.followup.send(
                    "Could not find giveaway channel.", ephemeral=True
                )
            
            try:
                await channel.fetch_message(int(message_id))
            except:
                return await interaction.followup.send(
                    "Couldn't fetch giveaway message.", ephemeral=True
                )

            members = [str(m.id) for m in channel.guild.members if not m.bot]
            if not members:
                return await interaction.followup.send(
                    "No valid members.", ephemeral=True
                )

            end_time = get_current_utc_timestamp() + duration_in_minutes * 60
            await giveaway_cog.db.fake_reactions.update_one(
                {"message_id": message_id},
                {"$set": {
                    "channel_id": gw["channel_id"],
                    "total_reactions": total_fake_reactions,
                    "remaining_reactions": total_fake_reactions,
                    "end_time": end_time,
                    "created_by": interaction.user.id,
                    "created_at": get_current_utc_timestamp(),
                    "status": "active"
                }},
                upsert=True
            )

            task = asyncio.create_task(
                self.add_fake_reactions(
                    message_id, members, total_fake_reactions, end_time
                )
            )
            self.active_fake_reaction_tasks[message_id] = task
            
            # Update cooldown after successful start
            self._guild_fill_cooldowns[guild_id] = current_time

            await interaction.followup.send(
                f"✅ Started fake fill: {total_fake_reactions} reactions over {duration_in_minutes} minutes.",
                ephemeral=True,
            )

        except ValueError as ve:
            await interaction.followup.send(f"Error: {ve}", ephemeral=True)
        except Exception as e:
            self.logger.error(f"fill_giveaway error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    async def add_fake_reactions(
        self,
        message_id: str,
        member_ids: List[str],
        total_reactions: int,
        end_time: float,
    ) -> None:
        """Gradually add fake reactions to a giveaway message."""
        giveaway_cog = self.bot.get_cog("GiveawayCog")
        if (
            not giveaway_cog
            or not hasattr(giveaway_cog, "db")
            or not giveaway_cog.db.connected
        ):
            return

        try:
            gw = await giveaway_cog.db.giveaways.find_one({"message_id": message_id})
            if not gw:
                return

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                self.logger.error(f"Channel not found for giveaway {message_id}")
                return
            
            message = await channel.fetch_message(int(message_id))

            remaining = total_reactions
            while remaining > 0:
                task = asyncio.current_task()
                if task and task.cancelled():
                    raise asyncio.CancelledError()

                now = get_current_utc_timestamp()
                if now >= end_time:
                    break

                active = await giveaway_cog.db.giveaways.find_one({
                    "message_id": message_id,
                    "status": "active"
                })
                if not active:
                    break

                used = await giveaway_cog.db.participants.find({
                    "message_id": message_id,
                    "is_fake": 1
                }).to_list(length=None)
                used_ids = {row["original_user_id"] for row in used if row.get("original_user_id")}

                available = [uid for uid in member_ids if uid not in used_ids]
                if not available:
                    available = member_ids

                user_id = random.choice(available)
                sequence = total_reactions - remaining
                fake_id = create_fake_participant_id(user_id, sequence)

                # Check if this fake user already exists in the participants table
                existing = await giveaway_cog.db.participants.find_one({
                    "message_id": message_id,
                    "user_id": fake_id
                })
                
                # Only insert if they don't already exist
                if not existing:
                    try:
                        await giveaway_cog.db.participants.insert_one({
                            "message_id": message_id,
                            "user_id": fake_id,
                            "original_user_id": user_id,
                            "joined_at": now,
                            "is_fake": 1,
                            "is_forced": 0
                        })
                        # Update cache for fake participant
                        if hasattr(giveaway_cog, '_cache_lock') and hasattr(giveaway_cog, '_participant_cache'):
                            async with giveaway_cog._cache_lock:
                                current = giveaway_cog._participant_cache.get(message_id, 0)
                                giveaway_cog._participant_cache[message_id] = current + 1
                    except Exception as e:
                        # Handle duplicate key error (race condition)
                        if "duplicate key" not in str(e).lower():
                            self.logger.error(f"Error inserting fake participant: {e}")

                remaining -= 1
                await giveaway_cog.db.fake_reactions.update_one(
                    {"message_id": message_id},
                    {"$set": {"remaining_reactions": remaining}}
                )

                # Update embed periodically (every 10 reactions) to avoid rate limits
                # Don't update on every single reaction
                if remaining % 10 == 0 or remaining == 0:
                    if message.embeds:
                        try:
                            embed = message.embeds[0]
                            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
                            embed.description = (
                                f">>> {WINNER_EMOJI} **Winner:** {gw['winners_count']}\n"
                                f"{TIME_EMOJI} **Ends:** <t:{gw['end_time']}:R>\n"
                                f"{PRIZE_EMOJI} **Hosted by:** <@{gw['host_id']}>"
                            )
                            embed.timestamp = datetime.fromtimestamp(gw["end_time"], timezone.utc)
                            await message.edit(embed=embed)
                        except discord.HTTPException as e:
                            # Rate limited or other HTTP error, skip this update
                            self.logger.debug(f"Skipped embed update due to rate limit: {e}")

                # Spread reactions evenly/randomly
                time_left = max(end_time - now, 1)
                avg = max(time_left / max(1, remaining), 1)
                delay = random.uniform(avg * 0.5, avg * 1.5)
                if now + delay > end_time:
                    break
                await asyncio.sleep(min(delay, time_left))

            # On finish, record fake participants
            rows = await giveaway_cog.db.participants.find({
                "message_id": message_id,
                "is_fake": 1
            }).to_list(length=None)
            fake_list = [r["user_id"] for r in rows]
            await giveaway_cog.db.fake_reactions.update_one(
                {"message_id": message_id},
                {"$set": {
                    "status": "completed",
                    "completed_at": get_current_utc_timestamp(),
                    "remaining_reactions": 0,
                    "fake_participants": json.dumps(fake_list)
                }}
            )

        except asyncio.CancelledError:
            try:
                await giveaway_cog.db.fake_reactions.update_one(
                    {"message_id": message_id},
                    {"$set": {
                        "status": "cancelled",
                        "cancelled_at": get_current_utc_timestamp()
                    }}
                )
            except Exception as e:
                self.logger.error(f"Error updating cancelled status: {e}")
        except Exception as e:
            self.logger.error(f"add_fake_reactions error for {message_id}: {e}")
            try:
                await giveaway_cog.db.fake_reactions.update_one(
                    {"message_id": message_id},
                    {"$set": {
                        "status": "error",
                        "error": str(e)
                    }}
                )
            except Exception as db_error:
                self.logger.error(f"Error updating error status: {db_error}")
        finally:
            self.active_fake_reaction_tasks.pop(message_id, None)

    async def force_winner_cmd(
        self,
        interaction: discord.Interaction,
        message_id: str,
        users: str,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            # Validate message ID format
            try:
                int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ Invalid message ID format. Please provide a valid numeric message ID.",
                    ephemeral=True
                )
            
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            import re
            mention_ids = re.findall(r"<@!?(\d+)>", users)
            plain_ids = [
                uid.strip()
                for uid in re.sub(r"<@!?(\d+)>", "", users).split(",")
                if uid.strip().isdigit()
            ]
            user_id_list = list({*mention_ids, *plain_ids})

            if not user_id_list:
                return await interaction.followup.send(
                    "Please mention users or provide valid IDs.", ephemeral=True
                )

            gw = await giveaway_cog.db.giveaways.find_one({
                "message_id": message_id,
                "status": "active"
            })
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                return await interaction.followup.send(
                    "Could not find giveaway channel.", ephemeral=True
                )
            
            try:
                message = await channel.fetch_message(int(message_id))
            except:
                return await interaction.followup.send(
                    "Couldn't fetch giveaway message.", ephemeral=True
                )

            # Verify existence
            for uid in user_id_list:
                try:
                    await self.bot.fetch_user(int(uid))
                except discord.NotFound:
                    return await interaction.followup.send(
                        f"User ID not found: {uid}", ephemeral=True
                    )

            # Persist forced winners
            await giveaway_cog.db.giveaways.update_one(
                {"message_id": message_id},
                {"$set": {"forced_winner_ids": user_id_list}}
            )

            # Add each forced winner as a participant if they don't already exist
            for uid in user_id_list:
                # Check if this user already exists in the participants table
                existing = await giveaway_cog.db.participants.find_one({
                    "message_id": message_id,
                    "user_id": uid
                })
                
                # Only insert if they don't already exist
                if not existing:
                    try:
                        await giveaway_cog.db.participants.insert_one({
                            "message_id": message_id,
                            "user_id": uid,
                            "joined_at": get_current_utc_timestamp(),
                            "is_forced": 1,
                            "is_fake": 0,
                            "original_user_id": None
                        })
                        # Update cache for forced winner
                        if hasattr(giveaway_cog, '_cache_lock') and hasattr(giveaway_cog, '_participant_cache'):
                            async with giveaway_cog._cache_lock:
                                current = giveaway_cog._participant_cache.get(message_id, 0)
                                giveaway_cog._participant_cache[message_id] = current + 1
                    except Exception as e:
                        # Handle duplicate key error (race condition)
                        if "duplicate key" not in str(e).lower():
                            self.logger.error(f"Error inserting forced winner: {e}")
                else:
                    # Update existing entry to mark as forced
                    await giveaway_cog.db.participants.update_one(
                        {"message_id": message_id, "user_id": uid},
                        {"$set": {"is_forced": 1}}
                    )

            mentions = ", ".join(f"<@{uid}>" for uid in user_id_list)
            await interaction.followup.send(
                f"Forced winners set: {mentions}", ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"force_winner error: {e}")
            await interaction.followup.send(
                f"Error setting forced winners: {e}", ephemeral=True
            )

    async def extend(
        self,
        interaction: discord.Interaction,
        message_id: str,
        additional_time: str
    ):
        """Extend the duration of an active giveaway."""
        await interaction.response.defer(ephemeral=True)
        try:
            # Validate message ID format
            try:
                int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ Invalid message ID format. Please provide a valid numeric message ID.",
                    ephemeral=True
                )
            
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            gw = await giveaway_cog.db.giveaways.find_one({
                "message_id": message_id,
                "status": "active"
            })
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                return await interaction.followup.send(
                    "Could not find giveaway channel.", ephemeral=True
                )
            
            try:
                message = await channel.fetch_message(int(message_id))
            except Exception as e:
                return await interaction.followup.send(
                    f"Couldn't fetch giveaway message: {e}", ephemeral=True
                )

            # Parse additional time
            import re
            from cogs.giveaways.config import DURATION_UNITS
            pattern = r'(\d+)([smhdw])'
            matches = re.findall(pattern, additional_time.lower())
            if not matches:
                return await interaction.followup.send(
                    "Invalid time format. Use formats like: 30s, 1h, 1h30m, 2d5h30m, 1w",
                    ephemeral=True
                )
            
            additional_seconds = sum(int(n) * DURATION_UNITS[u] for n, u in matches)
            
            # Calculate new end time
            new_end_time = gw["end_time"] + additional_seconds
            
            # Validate new end time doesn't exceed max duration from creation
            from cogs.giveaways.config import MAX_GIVEAWAY_DURATION
            created_at = gw.get("created_at", get_current_utc_timestamp())
            total_duration = new_end_time - created_at
            if total_duration > MAX_GIVEAWAY_DURATION:
                return await interaction.followup.send(
                    f"Cannot extend: Total duration would exceed maximum allowed ({MAX_GIVEAWAY_DURATION}s).",
                    ephemeral=True
                )
            
            # Update database
            await giveaway_cog.db.giveaways.update_one(
                {"message_id": message_id},
                {"$set": {"end_time": new_end_time}}
            )

            # Format duration display
            def fmt_dur(sec):
                parts = []
                for unit_sec, label in [(86400,'d'),(3600,'h'),(60,'m')]:
                    if sec >= unit_sec:
                        cnt, sec = divmod(sec, unit_sec)
                        parts.append(f"{cnt}{label}")
                if sec:
                    parts.append(f"{sec}s")
                return " ".join(parts) or "0s"
            
            added_display = fmt_dur(additional_seconds)
            
            # Update embed
            if not message.embeds:
                return await interaction.followup.send(
                    "Giveaway message has no embed to update.", ephemeral=True
                )
            
            embed = message.embeds[0]
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed.description = (
                f">>> {WINNER_EMOJI} **Winner:** {gw['winners_count']}\n"
                f"{TIME_EMOJI} **Ends:** <t:{new_end_time}:R>\n"
                f"{PRIZE_EMOJI} **Hosted by:** <@{gw['host_id']}>"
            )
            embed.timestamp = datetime.fromtimestamp(new_end_time, timezone.utc)
            
            await message.edit(embed=embed)
            
            await interaction.followup.send(
                f"✅ Giveaway duration extended by {added_display}!\n"
                f"New end time: <t:{new_end_time}:F> (<t:{new_end_time}:R>)",
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"extend_giveaway error: {e}")
            await interaction.followup.send(
                f"Error extending giveaway: {e}", ephemeral=True
            )

    async def cancel(
        self,
        interaction: discord.Interaction,
        message_id: str,
        reason: Optional[str] = "Cancelled by administrator"
    ):
        """Cancel an active giveaway."""
        await interaction.response.defer(ephemeral=True)
        try:
            # Validate message ID format
            try:
                int(message_id)
            except ValueError:
                return await interaction.followup.send(
                    "❌ Invalid message ID format. Please provide a valid numeric message ID.",
                    ephemeral=True
                )
            
            giveaway_cog = self.bot.get_cog("GiveawayCog")
            if (
                not giveaway_cog
                or not hasattr(giveaway_cog, "db")
                or not giveaway_cog.db.connected
            ):
                return await interaction.followup.send(
                    "Giveaway system not available.", ephemeral=True
                )

            gw = await giveaway_cog.db.giveaways.find_one({
                "message_id": message_id,
                "status": "active"
            })
            if not gw:
                return await interaction.followup.send(
                    "Not an active giveaway.", ephemeral=True
                )

            channel = self.bot.get_channel(gw["channel_id"])
            if not channel:
                return await interaction.followup.send(
                    "Could not find giveaway channel.", ephemeral=True
                )
            
            try:
                message = await channel.fetch_message(int(message_id))
            except Exception as e:
                return await interaction.followup.send(
                    f"Couldn't fetch giveaway message: {e}", ephemeral=True
                )

            # Cancel any active fake reaction task
            if message_id in self.active_fake_reaction_tasks:
                self.active_fake_reaction_tasks[message_id].cancel()
                await giveaway_cog.db.fake_reactions.update_one(
                    {"message_id": message_id},
                    {"$set": {
                        "status": "cancelled",
                        "cancelled_at": get_current_utc_timestamp()
                    }}
                )

            # Update giveaway status
            now_ts = get_current_utc_timestamp()
            await giveaway_cog.db.giveaways.update_one(
                {"message_id": message_id},
                {"$set": {
                    "status": "cancelled",
                    "cancelled_at": now_ts,
                    "cancelled_by": interaction.user.id,
                    "error": reason
                }}
            )

            # Update embed
            icon = None
            if channel.guild and hasattr(channel.guild, 'icon') and channel.guild.icon:
                icon = channel.guild.icon.url
            prize_name = gw['prize'] if 'prize' in gw.keys() else 'Unknown'
            embed = discord.Embed(
                title=f"{GIFT_EMOJI} {prize_name}",
                description=(
                    f">>> {RED_DOT_EMOJI} **CANCELLED**\n"
                    f"{DOT_EMOJI} Reason: {reason}\n"
                    f"{DOT_EMOJI} Cancelled by: {interaction.user.mention}\n"
                    f"{DOT_EMOJI} Hosted by: <@{gw['host_id']}>"
                ),
                color=0xFF0000,  # Red color for cancelled
                timestamp=datetime.fromtimestamp(now_ts, timezone.utc)
            )
            if icon:
                embed.set_footer(text=channel.guild.name, icon_url=icon)
            
            await message.edit(embed=embed, view=None)
            
            # Clean up cache entry for cancelled giveaway
            if hasattr(giveaway_cog, '_cache_lock') and hasattr(giveaway_cog, '_participant_cache'):
                async with giveaway_cog._cache_lock:
                    giveaway_cog._participant_cache.pop(message_id, None)
            
            # Record to history
            if giveaway_cog.config.enable_statistics:
                try:
                    parts = await giveaway_cog.db.participants.find({"message_id": message_id}).to_list(length=None)
                    total_parts = len([p for p in parts if p['user_id'] != str(interaction.client.user.id)])
                    duration = now_ts - gw.get("created_at", now_ts)
                    await giveaway_cog._record_giveaway_history(
                        channel.guild.id,
                        "cancelled",
                        {
                            "message_id": message_id,
                            "prize": gw.get("prize", "Unknown"),
                            "participants": total_parts,
                            "winners_count": gw["winners_count"],
                            "actual_winners": 0,
                            "duration_seconds": duration
                        }
                    )
                except Exception as hist_error:
                    self.logger.error(f"Error recording cancellation history: {hist_error}")
            
            await interaction.followup.send(
                f"✅ Giveaway cancelled successfully.\nReason: {reason}",
                ephemeral=True
            )

        except Exception as e:
            self.logger.error(f"cancel_giveaway error: {e}")
            await interaction.followup.send(
                f"Error cancelling giveaway: {e}", ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(GiveawayAdminCog(bot))
