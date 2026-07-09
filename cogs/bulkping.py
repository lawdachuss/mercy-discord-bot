import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Set
import logging
from pymongo import ReturnDocument

# Setup logging
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class BulkPingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_operations: Dict[int, asyncio.Task] = {}  # guild_id -> task
        self.operation_states: Dict[int, dict] = {}  # guild_id -> state
        self.dm_sent_operations: Set[int] = set()  # Track operations that already sent DM

        # MongoDB collections
        self.settings_col = self.bot.mongo_client['discord_bot']['bulkping_settings']
        self.operations_col = self.bot.mongo_client['discord_bot']['bulkping_operations']
        self.cooldowns_col = self.bot.mongo_client['discord_bot']['bulkping_cooldowns']
        self.counters_col = self.bot.mongo_client['discord_bot']['bulkping_counters']

        # Start cleanup task
        self.cleanup_task.start()

        # Resume unfinished operations on startup
        self.bot.loop.create_task(self.resume_operations())

    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.cleanup_task.cancel()
        for task in self.active_operations.values():
            task.cancel()

    async def get_user_cooldown(self, guild_id: int, user_id: int) -> Optional[datetime]:
        """Get user's last command usage time"""
        result = await self.cooldowns_col.find_one({
            "guild_id": guild_id,
            "user_id": user_id
        })
        if result and 'last_used' in result:
            return result['last_used']
        return None

    async def set_user_cooldown(self, guild_id: int, user_id: int):
        """Set user's cooldown"""
        await self.cooldowns_col.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$set": {"last_used": datetime.now()}},
            upsert=True
        )

    async def save_operation(self, guild_id: int, operation_data: dict) -> int:
        """Save operation to database and return operation ID"""
        counter = await self.counters_col.find_one_and_update(
            {"_id": f"{guild_id}_operation_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        operation_id = counter['seq']

        operation_data['guild_id'] = guild_id
        operation_data['id'] = operation_id

        await self.operations_col.insert_one(operation_data)
        return operation_id

    async def update_operation(self, guild_id: int, operation_id: int, updates: dict):
        """Update operation in database"""
        await self.operations_col.update_one(
            {"guild_id": guild_id, "id": operation_id},
            {"$set": updates}
        )

    async def get_unfinished_operations(self, guild_id: int) -> List[dict]:
        """Get unfinished operations for a guild"""
        cursor = self.operations_col.find({
            "guild_id": guild_id,
            "status": {"$in": ["running", "paused"]}
        })
        return await cursor.to_list(length=None)

    async def resume_operations(self):
        """Resume unfinished operations on bot startup"""
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            operations = await self.get_unfinished_operations(guild.id)
            for op in operations:
                # Skip if operation is already active
                if guild.id in self.active_operations:
                    continue
                task = asyncio.create_task(self.resume_bulk_ping_operation(guild, op))
                self.active_operations[guild.id] = task

    async def resume_bulk_ping_operation(self, guild: discord.Guild, operation_data: dict):
        """Resume a bulk ping operation"""
        try:
            # Reconstruct operation state
            channel = guild.get_channel(operation_data['channel_id'])
            role = guild.get_role(operation_data['role_id'])
            log_channel = guild.get_channel(operation_data['log_channel_id'])

            if not all([channel, role, log_channel]):
                logger.error(f"Could not resume operation {operation_data['id']}: Missing channels/roles")
                await self.update_operation(guild.id, operation_data['id'], {
                    'status': 'failed',
                    'completed_at': datetime.now().isoformat()
                })
                return

            # Get all members and filter out already pinged ones
            all_members = [m for m in role.members if not m.bot]
            pinged_member_ids = set(operation_data.get('pinged_member_ids', []))
            remaining_members = [m for m in all_members if m.id not in pinged_member_ids]

            if not remaining_members:
                await self.update_operation(guild.id, operation_data['id'], {
                    'status': 'completed',
                    'completed_at': datetime.now().isoformat()
                })
                return

            # Resume pinging with existing progress
            await self.execute_bulk_ping(
                guild, channel, role, operation_data['message'], 
                log_channel, operation_data['concurrent'],
                operation_data['id'], remaining_members,
                operation_data['pinged_members'], operation_data['failed_members'],
                list(pinged_member_ids)
            )

        except Exception as e:
            logger.error(f"Error resuming operation {operation_data['id']}: {e}")
            await self.update_operation(guild.id, operation_data['id'], {
                'status': 'failed',
                'completed_at': datetime.now().isoformat()
            })

    @app_commands.command(name="bulkping", description="Bulk ping members of a role")
    @app_commands.describe(
        channel="Channel where the bot will send pings",
        role="Role whose members will be pinged",
        message="Custom message content sent with each ping",
        log_channel="Channel for logging ping progress and analytics",
        concurrent="Number of members to ping concurrently per batch (1-10)"
    )
    async def bulkping(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        message: str,
        log_channel: discord.TextChannel,
        concurrent: Optional[int] = 1
    ):
        # Permission check
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need Administrator permission to use this command.",
                ephemeral=True
            )
            return

        # Cooldown check
        last_used = await self.get_user_cooldown(interaction.guild_id, interaction.user.id)
        if last_used and datetime.now() - last_used < timedelta(seconds=2):
            remaining = 2 - (datetime.now() - last_used).total_seconds()
            await interaction.response.send_message(
                f"⏰ Cooldown active. Try again in {remaining:.1f} seconds.",
                ephemeral=True
            )
            return

        # Validate concurrent parameter
        if concurrent < 1 or concurrent > 10:
            concurrent = 1

        # Check if there's already an active operation
        if interaction.guild_id in self.active_operations:
            await interaction.response.send_message(
                "❌ There's already an active bulk ping operation in this server.",
                ephemeral=True
            )
            return

        # Get members to ping (excluding bots)
        members_to_ping = [member for member in role.members if not member.bot]

        if not members_to_ping:
            await interaction.response.send_message(
                "❌ No non-bot members found in the specified role.",
                ephemeral=True
            )
            return

        # Create preview embed
        preview_embed = discord.Embed(
            title="🔔 Bulk Ping Preview",
            description=f"**Channel:** {channel.mention}\n"
                       f"**Role:** {role.mention}\n"
                       f"**Members to ping:** {len(members_to_ping)}\n"
                       f"**Concurrent batch size:** {concurrent}\n"
                       f"**Log channel:** {log_channel.mention}",
            color=0x3498db
        )

        # Show message preview
        sample_member = members_to_ping[0] if members_to_ping else interaction.user
        preview_message = f"{sample_member.mention} {message}"
        preview_embed.add_field(
            name="📝 Message Preview",
            value=f"`{preview_message}`",
            inline=False
        )

        preview_embed.add_field(
            name="⚠️ Important",
            value="• Messages will be deleted after 2 seconds\n"
                  "• Operation can be cancelled anytime\n"
                  "• Progress will be logged in the specified channel",
            inline=False
        )

        # Create confirmation view
        view = BulkPingConfirmView(self, interaction.guild, channel, role, message, log_channel, concurrent, members_to_ping)

        await interaction.response.send_message(
            embed=preview_embed,
            view=view,
            ephemeral=True
        )

        # Set cooldown
        await self.set_user_cooldown(interaction.guild_id, interaction.user.id)

    async def execute_bulk_ping(
        self, guild: discord.Guild, channel: discord.TextChannel, 
        role: discord.Role, message: str, log_channel: discord.TextChannel,
        concurrent: int, operation_id: int, members_to_ping: List[discord.Member],
        initial_pinged: int = 0, initial_failed: List[int] = None, 
        initial_pinged_ids: List[int] = None
    ):
        """Execute the bulk ping operation"""
        if initial_failed is None:
            initial_failed = []
        if initial_pinged_ids is None:
            initial_pinged_ids = []

        failed_members = initial_failed.copy()
        pinged_count = initial_pinged
        pinged_member_ids = set(initial_pinged_ids)
        start_time = time.time()

        # Calculate total members including already pinged
        total_members = len(members_to_ping) + len(initial_pinged_ids)

        # Create progress dashboard
        dashboard_embed = discord.Embed(
            title="🔄 Bulk Ping in Progress",
            color=0xf39c12
        )
        dashboard_embed.add_field(
            name="Progress", 
            value=f"{pinged_count}/{total_members}",
            inline=True
        )
        dashboard_embed.add_field(
            name="Current Batch", 
            value="1",
            inline=True
        )
        dashboard_embed.add_field(
            name="Estimated Time", 
            value="Calculating...",
            inline=True
        )

        cancel_view = BulkPingCancelView(self, guild.id, operation_id)

        # Send dashboard and cancel button as DM to the admin user
        operation_data = await self.get_operation(guild.id, operation_id)
        user = None
        if operation_data:
            user = guild.get_member(operation_data['user_id'])

        dashboard_message = None
        if user:
            try:
                dashboard_message = await user.send(embed=dashboard_embed, view=cancel_view)
            except discord.Forbidden:
                pass  # User has DMs disabled
            except Exception as e:
                logger.error(f"Failed to send bulk ping dashboard DM to user {user.id}: {e}")

        # Store operation state
        self.operation_states[guild.id] = {
            'cancelled': False,
            'dashboard_message': dashboard_message,
            'operation_id': operation_id
        }

        try:
            # Process members in batches
            total_batches = (len(members_to_ping) + concurrent - 1) // concurrent

            for batch_idx in range(0, len(members_to_ping), concurrent):
                # Check if cancelled
                if self.operation_states.get(guild.id, {}).get('cancelled', False):
                    break

                batch = members_to_ping[batch_idx:batch_idx + concurrent]
                batch_number = (batch_idx // concurrent) + 1

                # Update database with current batch and pinged member IDs
                await self.update_operation(guild.id, operation_id, {
                    'current_batch': batch_number,
                    'pinged_members': pinged_count,
                    'pinged_member_ids': list(pinged_member_ids)
                })

                # Send ping messages for this batch
                sent_messages = []
                batch_failures = []

                for member in batch:
                    try:
                        # Check if cancelled before each ping
                        if self.operation_states.get(guild.id, {}).get('cancelled', False):
                            break

                        # Skip if member was already pinged
                        if member.id in pinged_member_ids:
                            continue

                        ping_message = f"{member.mention} {message}"
                        sent_msg = await channel.send(ping_message)
                        sent_messages.append(sent_msg)

                        # Mark member as pinged
                        pinged_member_ids.add(member.id)
                        pinged_count += 1

                        # Log individual ping
                        log_embed = discord.Embed(
                            title="✅ Member Pinged",
                            description=f"**Member:** {member.mention}\n**Progress:** {pinged_count}/{total_members}",
                            color=0x2ecc71,
                            timestamp=datetime.now()
                        )
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                        await log_channel.send(embed=log_embed)

                    except discord.HTTPException as e:
                        logger.error(f"Failed to ping {member}: {e}")
                        batch_failures.append(member.id)
                        failed_members.append(member.id)
                    except Exception as e:
                        logger.error(f"Unexpected error pinging {member}: {e}")
                        batch_failures.append(member.id)
                        failed_members.append(member.id)

                # Check if cancelled before waiting
                if self.operation_states.get(guild.id, {}).get('cancelled', False):
                    break

                # Wait 2 seconds then delete batch messages
                await asyncio.sleep(2)

                for msg in sent_messages:
                    try:
                        await msg.delete()
                    except discord.NotFound:
                        pass  # Message already deleted
                    except discord.HTTPException as e:
                        logger.error(f"Failed to delete message: {e}")

                # Update progress dashboard
                elapsed_time = time.time() - start_time
                remaining_members = len(members_to_ping) - (batch_idx + len(batch))
                estimated_total_time = elapsed_time * total_members / max(pinged_count, 1)
                estimated_remaining = max(0, estimated_total_time - elapsed_time)

                dashboard_embed.clear_fields()
                dashboard_embed.add_field(
                    name="Progress", 
                    value=f"{pinged_count}/{total_members}",
                    inline=True
                )
                dashboard_embed.add_field(
                    name="Current Batch", 
                    value=f"{batch_number}/{total_batches}",
                    inline=True
                )
                dashboard_embed.add_field(
                    name="Est. Remaining", 
                    value=f"{int(estimated_remaining)}s",
                    inline=True
                )

                if dashboard_message:
                    try:
                        await dashboard_message.edit(embed=dashboard_embed, view=cancel_view)
                    except discord.NotFound:
                        pass  # Dashboard message was deleted
                    except Exception as e:
                        logger.error(f"Failed to update dashboard: {e}")

                # Wait cooldown between batches (respect rate limits)
                if batch_idx + concurrent < len(members_to_ping):
                    if not self.operation_states.get(guild.id, {}).get('cancelled', False):
                        await asyncio.sleep(2)

            # Handle retry for failed members (only those not already pinged)
            retry_failed = [mid for mid in failed_members if mid not in pinged_member_ids]
            if retry_failed and not self.operation_states.get(guild.id, {}).get('cancelled', False):
                pinged_count, retry_failures = await self.retry_failed_pings(
                    guild, channel, log_channel, message, retry_failed, 
                    operation_id, pinged_member_ids, pinged_count
                )
                failed_members = retry_failures

        except Exception as e:
            logger.error(f"Error in bulk ping operation: {e}")

        finally:
            # Clean up and send final analytics
            await self.finalize_operation(
                guild, log_channel, operation_id, pinged_count, 
                total_members, len(failed_members), 
                time.time() - start_time
            )

            # Clean up operation state
            if guild.id in self.active_operations:
                del self.active_operations[guild.id]
            if guild.id in self.operation_states:
                del self.operation_states[guild.id]

    async def retry_failed_pings(self, guild: discord.Guild, channel: discord.TextChannel, 
                               log_channel: discord.TextChannel, message: str, 
                               failed_member_ids: List[int], operation_id: int,
                               pinged_member_ids: Set[int], pinged_count: int) -> tuple:
        """Retry pinging failed members and return updated counts"""
        if not failed_member_ids:
            return pinged_count, []

        retry_embed = discord.Embed(
            title="🔄 Retrying Failed Pings",
            description=f"Attempting to retry {len(failed_member_ids)} failed pings...",
            color=0xf39c12
        )
        await log_channel.send(embed=retry_embed)

        retry_failures = []

        for member_id in failed_member_ids:
            if self.operation_states.get(guild.id, {}).get('cancelled', False):
                break

            # Skip if already pinged
            if member_id in pinged_member_ids:
                continue

            member = guild.get_member(member_id)
            if not member:
                retry_failures.append(member_id)
                continue

            try:
                ping_message = f"{member.mention} {message}"
                sent_msg = await channel.send(ping_message)

                # Mark as pinged and increment count
                pinged_member_ids.add(member_id)
                pinged_count += 1

                # Delete after 2 seconds
                await asyncio.sleep(2)
                try:
                    await sent_msg.delete()
                except discord.NotFound:
                    pass

                # Log retry success
                log_embed = discord.Embed(
                    title="✅ Retry Successful",
                    description=f"Successfully pinged {member.mention} on retry",
                    color=0x2ecc71
                )
                await log_channel.send(embed=log_embed)

            except discord.HTTPException as e:
                logger.error(f"Retry failed for {member}: {e}")
                retry_failures.append(member_id)
            except Exception as e:
                logger.error(f"Unexpected error retrying {member}: {e}")
                retry_failures.append(member_id)

            await asyncio.sleep(1)  # Slower pace for retries

        # Update operation with final data
        await self.update_operation(guild.id, operation_id, {
            'failed_members': retry_failures,
            'pinged_members': pinged_count,
            'pinged_member_ids': list(pinged_member_ids)
        })

        return pinged_count, retry_failures

    async def finalize_operation(self, guild: discord.Guild, log_channel: discord.TextChannel,
                               operation_id: int, pinged_count: int, total_members: int,
                               failed_count: int, duration: float):
        """Send final analytics and update operation status"""

        # Determine final status
        cancelled = self.operation_states.get(guild.id, {}).get('cancelled', False)
        status = 'cancelled' if cancelled else 'completed'

        # Update operation in database
        await self.update_operation(guild.id, operation_id, {
            'status': status,
            'completed_at': datetime.now().isoformat(),
            'pinged_members': pinged_count
        })

        # Create analytics embed
        analytics_embed = discord.Embed(
            title="📊 Bulk Ping Analytics" + (" (Cancelled)" if cancelled else " (Completed)"),
            color=0xe74c3c if cancelled else 0x2ecc71,
            timestamp=datetime.now()
        )

        analytics_embed.add_field(name="Total Members Targeted", value=str(total_members), inline=True)
        analytics_embed.add_field(name="Successfully Pinged", value=str(pinged_count), inline=True)
        analytics_embed.add_field(name="Failed Attempts", value=str(failed_count), inline=True)
        analytics_embed.add_field(name="Success Rate", value=f"{(pinged_count/max(total_members,1)*100):.1f}%", inline=True)
        analytics_embed.add_field(name="Duration", value=f"{duration:.1f}s", inline=True)
        analytics_embed.add_field(name="Status", value=status.title(), inline=True)

        try:
            await log_channel.send(embed=analytics_embed)
        except Exception as e:
            logger.error(f"Failed to send analytics: {e}")

        # Send DM only once per operation
        if operation_id not in self.dm_sent_operations:
            self.dm_sent_operations.add(operation_id)
            try:
                operation_data = await self.get_operation(guild.id, operation_id)
                if operation_data:
                    user = guild.get_member(operation_data['user_id'])
                    if user:
                        await user.send(embed=analytics_embed)
            except discord.Forbidden:
                pass  # User has DMs disabled
            except Exception as e:
                logger.error(f"Failed to DM analytics: {e}")

    async def get_operation(self, guild_id: int, operation_id: int) -> Optional[dict]:
        """Get operation data from database"""
        return await self.operations_col.find_one({
            "guild_id": guild_id,
            "id": operation_id
        })

    def cancel_operation(self, guild_id: int):
        """Cancel an active operation"""
        if guild_id in self.operation_states:
            self.operation_states[guild_id]['cancelled'] = True

        if guild_id in self.active_operations:
            self.active_operations[guild_id].cancel()

    @tasks.loop(minutes=5)
    async def cleanup_task(self):
        """Periodic cleanup of completed operations"""
        for guild_id in list(self.active_operations.keys()):
            task = self.active_operations[guild_id]
            if task.done():
                del self.active_operations[guild_id]

        # Clean up old DM tracking (keep only recent operations)
        if len(self.dm_sent_operations) > 1000:
            self.dm_sent_operations.clear()

class BulkPingConfirmView(discord.ui.View):
    def __init__(self, cog: BulkPingCog, guild: discord.Guild, channel: discord.TextChannel,
                 role: discord.Role, message: str, log_channel: discord.TextChannel,
                 concurrent: int, members_to_ping: List[discord.Member]):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild = guild
        self.channel = channel
        self.role = role
        self.message = message
        self.log_channel = log_channel
        self.concurrent = concurrent
        self.members_to_ping = members_to_ping

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green, emoji="▶️")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✅ **Bulk ping operation started!** Check the log channel for progress updates.",
            embed=None,
            view=None
        )

        # Save operation to database
        operation_data = {
            'user_id': interaction.user.id,
            'channel_id': self.channel.id,
            'role_id': self.role.id,
            'message': self.message,
            'log_channel_id': self.log_channel.id,
            'concurrent': self.concurrent,
            'status': 'running',
            'created_at': datetime.now().isoformat(),
            'total_members': len(self.members_to_ping),
            'pinged_members': 0,
            'failed_members': [],
            'current_batch': 0,
            'pinged_member_ids': []
        }

        operation_id = await self.cog.save_operation(self.guild.id, operation_data)

        # Start the bulk ping operation
        task = asyncio.create_task(
            self.cog.execute_bulk_ping(
                self.guild, self.channel, self.role, self.message,
                self.log_channel, self.concurrent, operation_id, self.members_to_ping
            )
        )
        self.cog.active_operations[self.guild.id] = task

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ **Bulk ping operation cancelled.**",
            embed=None,
            view=None
        )

    async def on_timeout(self):
        # Disable all buttons when view times out
        for item in self.children:
            item.disabled = True

class BulkPingCancelView(discord.ui.View):
    def __init__(self, cog: BulkPingCog, guild_id: int, operation_id: int):
        super().__init__(timeout=None)  # No timeout for cancel button
        self.cog = cog
        self.guild_id = guild_id
        self.operation_id = operation_id

    @discord.ui.button(label="Cancel Operation", style=discord.ButtonStyle.red, emoji="🛑")
    async def cancel_operation(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has admin permissions by fetching member from guild
        guild = self.cog.bot.get_guild(self.guild_id)
        member = guild.get_member(interaction.user.id) if guild else None
        if not member or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need Administrator permission to cancel this operation.",
                ephemeral=True
            )
            return

        self.cog.cancel_operation(self.guild_id)

        await interaction.response.send_message(
            "🛑 **Bulk ping operation cancelled!** Final analytics will be posted shortly.",
            ephemeral=True
        )

        # Disable the button
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass  # Message was deleted

async def setup(bot):
    await bot.add_cog(BulkPingCog(bot))
