"""
Purge cog for Discord bot - Bulk message deletion functionality.
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
import asyncio
import logging

logger = logging.getLogger(__name__)


class PurgeCog(commands.Cog):
    """Cog for bulk message deletion with filtering options."""
    
    def __init__(self, bot):
        """Initialize the PurgeCog.
        
        Args:
            bot: The Discord bot instance
        """
        self.bot = bot
        self.PURGE_LIMIT = 100
        self.MESSAGE_DELETE_DELAY = 3
    
    async def _check_permissions(self, ctx):
        """Check if the user and bot have required permissions.
        
        Args:
            ctx: The command context
            
        Returns:
            bool: True if all permission checks pass, False otherwise
        """
        # Check if user has administrator permissions
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("<a:no_originals:1429054644654571580> You need administrator permissions to use this command.", delete_after=self.MESSAGE_DELETE_DELAY)
            return False
        
        # Get bot member object
        bot_member = ctx.guild.me
        
        # Check if bot has manage_messages permission
        if not bot_member.guild_permissions.manage_messages:
            await ctx.send("<a:no_originals:1429054644654571580> I need the `Manage Messages` permission to purge messages.", delete_after=self.MESSAGE_DELETE_DELAY)
            return False
        
        # Check if bot has read_message_history permission
        if not bot_member.guild_permissions.read_message_history:
            await ctx.send("<a:no_originals:1429054644654571580> I need the `Read Message History` permission to purge messages.", delete_after=self.MESSAGE_DELETE_DELAY)
            return False
        
        return True
    
    async def _parse_arguments(self, ctx, args):
        """Parse command arguments for count, filter type, and target user.
        
        Args:
            ctx: The command context
            args: List of command arguments
            
        Returns:
            dict: Parsed arguments with keys 'count', 'filter_type', 'target_user'
                  Returns None if parsing fails
        """
        if not args:
            await ctx.send("<a:no_originals:1429054644654571580> Usage: `.purge <count>` or `.purge <count> <bots|me|@user>`", delete_after=self.MESSAGE_DELETE_DELAY)
            return None
        
        count = None
        filter_type = None
        target_user = None
        
        # Parse arguments
        for arg in args:
            # Try to parse as integer (count)
            if arg.isdigit():
                parsed_count = int(arg)
                if parsed_count < 1 or parsed_count > self.PURGE_LIMIT:
                    await ctx.send(f"<a:no_originals:1429054644654571580> Count must be between 1 and {self.PURGE_LIMIT}.", delete_after=self.MESSAGE_DELETE_DELAY)
                    return None
                count = parsed_count
            # Check for filter keywords
            elif arg.lower() == 'bots':
                if filter_type:
                    await ctx.send("<a:no_originals:1429054644654571580> Cannot specify multiple filters.", delete_after=self.MESSAGE_DELETE_DELAY)
                    return None
                filter_type = 'bots'
            elif arg.lower() == 'me':
                if filter_type:
                    await ctx.send("<a:no_originals:1429054644654571580> Cannot specify multiple filters.", delete_after=self.MESSAGE_DELETE_DELAY)
                    return None
                filter_type = 'me'
            # Check for user mention
            elif arg.startswith('<@') and arg.endswith('>'):
                if filter_type:
                    await ctx.send("<a:no_originals:1429054644654571580> Cannot specify multiple filters.", delete_after=self.MESSAGE_DELETE_DELAY)
                    return None
                # Extract user ID from mention
                user_id_str = arg.strip('<@!>').strip('<@>')
                try:
                    user_id = int(user_id_str)
                    target_user = ctx.guild.get_member(user_id)
                    if not target_user:
                        await ctx.send("<a:no_originals:1429054644654571580> User not found in this server.", delete_after=self.MESSAGE_DELETE_DELAY)
                        return None
                    filter_type = 'user'
                except ValueError:
                    await ctx.send("<a:no_originals:1429054644654571580> Invalid user mention.", delete_after=self.MESSAGE_DELETE_DELAY)
                    return None
            else:
                await ctx.send(f"<a:no_originals:1429054644654571580> Invalid argument: `{arg}`", delete_after=self.MESSAGE_DELETE_DELAY)
                return None
        
        # Set default count if not specified
        if count is None:
            count = self.PURGE_LIMIT
        
        return {
            'count': count,
            'filter_type': filter_type,
            'target_user': target_user
        }
    
    async def _fetch_and_filter_messages(self, ctx, count, filter_type, target_user):
        """Fetch and filter messages based on specified criteria.
        
        Args:
            ctx: The command context
            count: Number of messages to delete
            filter_type: Type of filter ('bots', 'me', 'user', or None)
            target_user: Target user for 'user' filter
            
        Returns:
            tuple: (messages_to_delete, skipped_old_count) - List of messages and count of skipped old messages
        """
        # Fetch messages with buffer to account for filtering and old messages
        fetch_limit = count + 100 if filter_type else count + 50
        
        # Calculate 14-day cutoff (Discord's bulk delete limitation)
        fourteen_days_ago = datetime.now(timezone.utc) - timedelta(days=14)
        
        messages_to_delete = []
        skipped_old_messages = 0
        
        # Fetch messages from channel history
        async for message in ctx.channel.history(limit=fetch_limit):
            # Track messages older than 14 days but don't stop fetching
            if message.created_at < fourteen_days_ago:
                skipped_old_messages += 1
                continue
            
            # Apply filters
            if filter_type == 'bots':
                if not message.author.bot:
                    continue
            elif filter_type == 'me':
                if message.author.id != ctx.author.id:
                    continue
            elif filter_type == 'user':
                if message.author.id != target_user.id:
                    continue
            
            messages_to_delete.append(message)
            
            # Stop when we have enough messages
            if len(messages_to_delete) >= count:
                break
        
        return messages_to_delete, skipped_old_messages
    
    async def _perform_bulk_delete(self, ctx, messages):
        """Perform bulk deletion of messages.
        
        Args:
            ctx: The command context
            messages: List of messages to delete
            
        Returns:
            int: Number of successfully deleted messages, or None if error occurred
        """
        if not messages:
            return 0
        
        try:
            await ctx.channel.delete_messages(messages)
            return len(messages)
        except discord.Forbidden:
            await ctx.send("<a:no_originals:1429054644654571580> I don't have permission to delete messages.", delete_after=self.MESSAGE_DELETE_DELAY)
            return None
        except discord.HTTPException as e:
            if "older than 14 days" in str(e).lower():
                await ctx.send("<:ogs_bell:1427918360401940552> Some messages are older than 14 days and cannot be deleted.", delete_after=self.MESSAGE_DELETE_DELAY)
            else:
                await ctx.send(f"<a:no_originals:1429054644654571580> An error occurred: {str(e)}", delete_after=self.MESSAGE_DELETE_DELAY)
            return None
        except discord.NotFound:
            await ctx.send("<:ogs_bell:1427918360401940552> Some messages were already deleted.", delete_after=self.MESSAGE_DELETE_DELAY)
            return None
    
    async def _send_feedback(self, ctx, deleted_count, filter_type, target_user):
        """Send feedback message about deletion results.
        
        Args:
            ctx: The command context
            deleted_count: Number of messages deleted
            filter_type: Type of filter used ('bots', 'me', 'user', or None)
            target_user: Target user for 'user' filter
        """
        # Build feedback message
        if deleted_count == 0:
            message_text = "<:ogs_bell:1427918360401940552> No messages found to delete."
        else:
            message_text = f"<a:white_tick:1426439810733572136> Successfully deleted {deleted_count} message{'s' if deleted_count != 1 else ''}"
            
            # Add filter information
            if filter_type == 'bots':
                message_text += " from bots"
            elif filter_type == 'me':
                message_text += f" from {ctx.author.mention}"
            elif filter_type == 'user' and target_user:
                message_text += f" from {target_user.mention}"
            
            message_text += "."
        
        # Send feedback message
        feedback_msg = await ctx.send(message_text)
        
        # Auto-delete after delay
        try:
            await asyncio.sleep(self.MESSAGE_DELETE_DELAY)
            await feedback_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Silently handle deletion errors
            pass
    
    @commands.command(name='purge')
    @commands.guild_only()
    async def purge(self, ctx, *args):
        """Bulk delete messages with optional filtering.
        
        Usage:
            .purge <count> - Delete specified number of messages
            .purge <count> bots - Delete bot messages only
            .purge <count> me - Delete your own messages only
            .purge <count> @user - Delete messages from specific user
            
        Args:
            ctx: The command context
            *args: Variable command arguments
        """
        try:
            # Delete command message immediately
            try:
                await ctx.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            
            # Check permissions
            if not await self._check_permissions(ctx):
                return
            
            # Parse arguments
            parsed_args = await self._parse_arguments(ctx, args)
            if parsed_args is None:
                return
            
            count = parsed_args['count']
            filter_type = parsed_args['filter_type']
            target_user = parsed_args['target_user']
            
            # Fetch and filter messages
            messages, skipped_old = await self._fetch_and_filter_messages(ctx, count, filter_type, target_user)
            
            # Handle no messages found
            if not messages:
                if skipped_old > 0:
                    await ctx.send(f"<:ogs_bell:1427918360401940552> No messages found to delete. {skipped_old} message{'s' if skipped_old != 1 else ''} skipped (older than 14 days).", delete_after=self.MESSAGE_DELETE_DELAY)
                else:
                    await self._send_feedback(ctx, 0, filter_type, target_user)
                return
            
            # Perform bulk delete
            deleted_count = await self._perform_bulk_delete(ctx, messages)
            
            # Send feedback if deletion was successful
            if deleted_count is not None:
                # Add note about skipped old messages if any
                if skipped_old > 0:
                    feedback_msg = f"<a:white_tick:1426439810733572136> Successfully deleted {deleted_count} message{'s' if deleted_count != 1 else ''}"
                    if filter_type == 'bots':
                        feedback_msg += " from bots"
                    elif filter_type == 'me':
                        feedback_msg += f" from {ctx.author.mention}"
                    elif filter_type == 'user' and target_user:
                        feedback_msg += f" from {target_user.mention}"
                    feedback_msg += f". ({skipped_old} older message{'s' if skipped_old != 1 else ''} skipped)"
                    
                    msg = await ctx.send(feedback_msg)
                    try:
                        await asyncio.sleep(self.MESSAGE_DELETE_DELAY)
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                else:
                    await self._send_feedback(ctx, deleted_count, filter_type, target_user)
                
        except Exception as e:
            # Log unexpected errors
            logger.error(f"Unexpected error in purge command: {str(e)}", exc_info=True)
            # Handle unexpected errors
            try:
                await ctx.send(f"<a:no_originals:1429054644654571580> An unexpected error occurred: {str(e)}", delete_after=self.MESSAGE_DELETE_DELAY)
            except:
                pass


async def setup(bot):
    """Load the PurgeCog.
    
    Args:
        bot: The Discord bot instance
    """
    await bot.add_cog(PurgeCog(bot))
