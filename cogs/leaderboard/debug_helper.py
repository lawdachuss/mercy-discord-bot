"""
Debug Helper for Leaderboard System
====================================
Provides debugging utilities and data validation for tracking issues.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import discord
from discord.ext import commands

logger = logging.getLogger('discord.bot.leaderboard.debug')


class DataTrackingValidator:
    """Validate data tracking operations"""
    
    @staticmethod
    async def validate_chat_increment(db, guild_id: int, user_id: int, before_stats: Dict = None) -> bool:
        """
        Validate that chat increment worked correctly.
        Returns True if validation passes.
        """
        try:
            # Get current stats
            after_stats = await db.user_stats.find_one({
                'guild_id': guild_id, 
                'user_id': user_id
            })
            
            if not after_stats:
                logger.error(f"No stats found after increment for user {user_id} in guild {guild_id}")
                return False
            
            if before_stats:
                # Check all counters increased by 1
                for field in ['chat_daily', 'chat_weekly', 'chat_monthly', 'chat_alltime']:
                    before_val = before_stats.get(field, 0)
                    after_val = after_stats.get(field, 0)
                    if after_val != before_val + 1:
                        logger.error(
                            f"Chat increment failed for {field}: "
                            f"before={before_val}, after={after_val}, expected={before_val + 1}"
                        )
                        return False
            
            logger.debug(f"Chat increment validated for user {user_id}: all counters increased correctly")
            return True
            
        except Exception as e:
            logger.error(f"Error validating chat increment: {e}")
            return False
    
    @staticmethod
    async def validate_voice_time(db, guild_id: int, user_id: int, 
                                 minutes_added: float, before_stats: Dict = None) -> bool:
        """
        Validate that voice time was added correctly.
        Returns True if validation passes.
        """
        try:
            # Get current stats
            after_stats = await db.user_stats.find_one({
                'guild_id': guild_id,
                'user_id': user_id
            })
            
            if not after_stats:
                logger.error(f"No stats found after voice time update for user {user_id}")
                return False
            
            if before_stats:
                # Check all voice counters increased by expected amount
                for field in ['voice_daily', 'voice_weekly', 'voice_monthly', 'voice_alltime']:
                    before_val = before_stats.get(field, 0)
                    after_val = after_stats.get(field, 0)
                    expected = before_val + minutes_added
                    
                    # Allow small tolerance for float precision
                    if abs(after_val - expected) > 0.1:
                        logger.error(
                            f"Voice time increment failed for {field}: "
                            f"before={before_val}, after={after_val}, "
                            f"expected={expected}, added={minutes_added}"
                        )
                        return False
            
            logger.debug(f"Voice time validated for user {user_id}: {minutes_added:.2f} minutes added correctly")
            return True
            
        except Exception as e:
            logger.error(f"Error validating voice time: {e}")
            return False
    
    @staticmethod
    async def check_data_integrity(db, guild_id: int) -> Dict:
        """
        Check overall data integrity for a guild.
        Returns a report of any issues found.
        """
        issues = []
        
        try:
            # Check for negative values
            negative_stats = await db.user_stats.find({
                'guild_id': guild_id,
                '$or': [
                    {'chat_daily': {'$lt': 0}},
                    {'chat_weekly': {'$lt': 0}},
                    {'chat_monthly': {'$lt': 0}},
                    {'voice_daily': {'$lt': 0}},
                    {'voice_weekly': {'$lt': 0}},
                    {'voice_monthly': {'$lt': 0}}
                ]
            }).to_list(length=100)
            
            if negative_stats:
                issues.append(f"Found {len(negative_stats)} users with negative stats")
                for stat in negative_stats[:5]:  # Show first 5
                    issues.append(f"  User {stat['user_id']}: chat_daily={stat.get('chat_daily', 0)}, voice_daily={stat.get('voice_daily', 0)}")
            
            # Check for inconsistent values (daily > weekly or weekly > monthly)
            inconsistent = await db.user_stats.find({
                'guild_id': guild_id,
                '$or': [
                    {'$expr': {'$gt': ['$chat_daily', '$chat_weekly']}},
                    {'$expr': {'$gt': ['$chat_weekly', '$chat_monthly']}},
                    {'$expr': {'$gt': ['$voice_daily', '$voice_weekly']}},
                    {'$expr': {'$gt': ['$voice_weekly', '$voice_monthly']}}
                ]
            }).to_list(length=100)
            
            if inconsistent:
                issues.append(f"Found {len(inconsistent)} users with inconsistent time periods")
            
            # Check for extremely high values (possible corruption)
            extreme_values = await db.user_stats.find({
                'guild_id': guild_id,
                '$or': [
                    {'chat_daily': {'$gt': 10000}},
                    {'voice_daily': {'$gt': 1440}}  # More than 24 hours in a day
                ]
            }).to_list(length=100)
            
            if extreme_values:
                issues.append(f"Found {len(extreme_values)} users with extreme values")
            
            return {
                'guild_id': guild_id,
                'issues': issues,
                'healthy': len(issues) == 0,
                'timestamp': datetime.utcnow()
            }
            
        except Exception as e:
            logger.error(f"Error checking data integrity: {e}")
            return {
                'guild_id': guild_id,
                'issues': [f"Error during check: {e}"],
                'healthy': False,
                'timestamp': datetime.utcnow()
            }


class DebugCommands(commands.Cog):
    """Debug commands for administrators"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = None
        self.logger = logging.getLogger('discord.bot.leaderboard.debug.commands')
    
    async def cog_load(self):
        """Initialize database connection"""
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.db = self.bot.mongo_client['poison_bot']
    
    @commands.command(name='check_tracking')
    @commands.has_permissions(administrator=True)
    async def check_tracking(self, ctx: commands.Context):
        """Check if data tracking is working correctly"""
        if self.db is None:
            await ctx.send("❌ Database not connected")
            return
        
        # Get before stats
        before_stats = await self.db.user_stats.find_one({
            'guild_id': ctx.guild.id,
            'user_id': ctx.author.id
        })
        
        # Send a test message to track
        await ctx.send("📊 Testing tracking... Say something!")
        
        # Wait a bit
        await asyncio.sleep(2)
        
        # Validate
        validator = DataTrackingValidator()
        chat_valid = await validator.validate_chat_increment(
            self.db, ctx.guild.id, ctx.author.id, before_stats
        )
        
        if chat_valid:
            await ctx.send("✅ Chat tracking is working correctly!")
        else:
            await ctx.send("❌ Chat tracking has issues - check logs")
    
    @commands.command(name='check_integrity')
    @commands.has_permissions(administrator=True)
    async def check_integrity(self, ctx: commands.Context):
        """Check data integrity for this guild"""
        if self.db is None:
            await ctx.send("❌ Database not connected")
            return
        
        await ctx.send("🔍 Checking data integrity...")
        
        validator = DataTrackingValidator()
        report = await validator.check_data_integrity(self.db, ctx.guild.id)
        
        if report['healthy']:
            await ctx.send("✅ **Data integrity check passed!** No issues found.")
        else:
            issues_text = "\n".join(report['issues'][:10])  # Show first 10 issues
            await ctx.send(f"⚠️ **Data integrity issues found:**\n```\n{issues_text}\n```")
    
    @commands.command(name='force_star_now')
    @commands.has_permissions(administrator=True)
    async def force_star_now(self, ctx: commands.Context):
        """Force Star of the Week selection immediately"""
        # Find the star cog
        star_cog = self.bot.get_cog('StarOfTheWeekCog')
        if not star_cog:
            await ctx.send("❌ Star of the Week cog not loaded")
            return
        
        await ctx.send("⏳ Forcing Star of the Week selection...")
        
        try:
            await star_cog._process_star_selection(ctx.guild)
            await ctx.send("✅ Star of the Week selection completed!")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.command(name='reset_tracking')
    @commands.has_permissions(administrator=True)
    async def reset_tracking(self, ctx: commands.Context, period: str = "daily"):
        """Reset tracking for a specific period (daily/weekly/monthly)"""
        if self.db is None:
            await ctx.send("❌ Database not connected")
            return
        
        if period not in ['daily', 'weekly', 'monthly']:
            await ctx.send("❌ Period must be: daily, weekly, or monthly")
            return
        
        # Confirmation
        await ctx.send(f"⚠️ This will reset all {period} stats for this guild. Type 'confirm' to proceed.")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == 'confirm'
        
        try:
            await self.bot.wait_for('message', check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("❌ Reset cancelled (timeout)")
            return
        
        # Reset stats
        update_fields = {
            f'chat_{period}': 0,
            f'voice_{period}': 0
        }
        
        result = await self.db.user_stats.update_many(
            {'guild_id': ctx.guild.id},
            {'$set': update_fields}
        )
        
        await ctx.send(f"✅ Reset {period} stats for {result.modified_count} users")
    
    @commands.command(name='force_recreate_leaderboards')
    @commands.has_permissions(administrator=True)
    async def force_recreate_leaderboards(self, ctx: commands.Context, leaderboard_type: str = "all"):
        """Force recreation of leaderboard embeds (chat/voice/all)"""
        if self.db is None:
            await ctx.send("❌ Database not connected")
            return
        
        if leaderboard_type not in ['chat', 'voice', 'all']:
            await ctx.send("❌ Type must be: chat, voice, or all")
            return
        
        await ctx.send(f"🔄 Forcing recreation of {leaderboard_type} leaderboard(s)...")
        
        try:
            # Clear message IDs from database to force recreation
            if leaderboard_type in ['chat', 'all']:
                result = await self.db.leaderboard_messages.delete_one({
                    'guild_id': ctx.guild.id,
                    'type': 'chat'
                })
                if result.deleted_count > 0:
                    await ctx.send("✅ Cleared chat leaderboard message IDs")
                else:
                    await ctx.send("ℹ️ No chat leaderboard message IDs found")
            
            if leaderboard_type in ['voice', 'all']:
                result = await self.db.leaderboard_messages.delete_one({
                    'guild_id': ctx.guild.id,
                    'type': 'voice'
                })
                if result.deleted_count > 0:
                    await ctx.send("✅ Cleared voice leaderboard message IDs")
                else:
                    await ctx.send("ℹ️ No voice leaderboard message IDs found")
            
            await ctx.send("✅ Leaderboards will be recreated in the next update cycle (within 5 minutes)")
            
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            self.logger.error(f"Error forcing leaderboard recreation: {e}", exc_info=True)
    
    @commands.command(name='show_stats')
    @commands.has_permissions(administrator=True)
    async def show_stats(self, ctx: commands.Context, user: discord.Member = None, period: str = "daily"):
        """Show raw database stats for a user or top 5 users"""
        if self.db is None:
            await ctx.send("❌ Database not connected")
            return
        
        if period not in ['daily', 'weekly', 'monthly', 'alltime']:
            await ctx.send("❌ Period must be: daily, weekly, monthly, or alltime")
            return
        
        try:
            if user:
                # Show specific user stats
                stat = await self.db.user_stats.find_one({
                    'guild_id': ctx.guild.id,
                    'user_id': user.id
                })
                
                if not stat:
                    await ctx.send(f"❌ No stats found for {user.mention}")
                    return
                
                embed = discord.Embed(
                    title=f"📊 Stats for {user.display_name}",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="Chat Stats",
                    value=f"Daily: {stat.get('chat_daily', 0)}\n"
                          f"Weekly: {stat.get('chat_weekly', 0)}\n"
                          f"Monthly: {stat.get('chat_monthly', 0)}\n"
                          f"All-time: {stat.get('chat_alltime', 0)}",
                    inline=True
                )
                embed.add_field(
                    name="Voice Stats (minutes)",
                    value=f"Daily: {stat.get('voice_daily', 0):.1f}\n"
                          f"Weekly: {stat.get('voice_weekly', 0):.1f}\n"
                          f"Monthly: {stat.get('voice_monthly', 0):.1f}\n"
                          f"All-time: {stat.get('voice_alltime', 0):.1f}",
                    inline=True
                )
                await ctx.send(embed=embed)
            else:
                # Show top 5 users for the period
                chat_field = f'chat_{period}'
                voice_field = f'voice_{period}'
                
                # Get top chat users
                chat_cursor = self.db.user_stats.find({
                    'guild_id': ctx.guild.id,
                    chat_field: {'$gt': 0}
                }).sort(chat_field, -1).limit(5)
                chat_stats = await chat_cursor.to_list(length=5)
                
                # Get top voice users
                voice_cursor = self.db.user_stats.find({
                    'guild_id': ctx.guild.id,
                    voice_field: {'$gt': 0}
                }).sort(voice_field, -1).limit(5)
                voice_stats = await voice_cursor.to_list(length=5)
                
                embed = discord.Embed(
                    title=f"📊 Top 5 Users - {period.capitalize()}",
                    color=discord.Color.blue()
                )
                
                # Chat leaderboard
                chat_lines = []
                for i, stat in enumerate(chat_stats, 1):
                    user_id = stat['user_id']
                    count = stat.get(chat_field, 0)
                    try:
                        member = await ctx.guild.fetch_member(user_id)
                        name = member.display_name[:15]
                    except:
                        name = f"User {user_id}"
                    chat_lines.append(f"{i}. {name}: {count:,} msgs")
                
                embed.add_field(
                    name="💬 Chat",
                    value="\n".join(chat_lines) if chat_lines else "No data",
                    inline=True
                )
                
                # Voice leaderboard
                voice_lines = []
                for i, stat in enumerate(voice_stats, 1):
                    user_id = stat['user_id']
                    minutes = stat.get(voice_field, 0)
                    hours = int(minutes // 60)
                    mins = int(minutes % 60)
                    try:
                        member = await ctx.guild.fetch_member(user_id)
                        name = member.display_name[:15]
                    except:
                        name = f"User {user_id}"
                    voice_lines.append(f"{i}. {name}: {hours}h {mins}m")
                
                embed.add_field(
                    name="🎤 Voice",
                    value="\n".join(voice_lines) if voice_lines else "No data",
                    inline=True
                )
                
                await ctx.send(embed=embed)
                
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            self.logger.error(f"Error showing stats: {e}", exc_info=True)


# Import asyncio for sleep
import asyncio


async def setup(bot: commands.Bot):
    """Load debug commands"""
    await bot.add_cog(DebugCommands(bot))
