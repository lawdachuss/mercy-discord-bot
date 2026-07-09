import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import asyncio
import datetime
import time
import logging
from collections import defaultdict
from typing import List, Optional

# Set up logging - errors only
logger = logging.getLogger('always-vc')
logger.setLevel(logging.ERROR)

# Suppress Discord voice connection errors (WebSocket 1006 errors)
discord_logger = logging.getLogger('discord.voice_state')
discord_logger.setLevel(logging.CRITICAL)
discord_gateway_logger = logging.getLogger('discord.gateway')
discord_gateway_logger.setLevel(logging.WARNING)

class ConnectionManager:
    """Handle connection attempts with rate limiting."""
    def __init__(self):
        self.connection_attempts = defaultdict(int)
        self.last_attempt = defaultdict(float)
        self._connection_locks = defaultdict(asyncio.Lock)
        self._voice_state_locks = defaultdict(asyncio.Lock)

    async def attempt_connection(self, guild_id: str, channel: discord.VoiceChannel) -> bool:
        """Attempt to connect to a voice channel with rate limiting."""
        current_time = time.time()
        
        async with self._connection_locks[guild_id]:
            # Check cooldown
            if current_time - self.last_attempt[guild_id] < 30:
                return False
                
            # Check if channel still exists
            if not channel or not channel.guild:
                logger.error(f"Channel {channel.id if channel else 'None'} no longer exists")
                return False
                
            # Check if we have permission to join
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.connect or not permissions.speak:
                logger.error(f"Missing permissions to join channel {channel.id} in guild {guild_id}")
                return False
            
            # CRITICAL: Check if already connected to the target channel - return early
            if channel.guild.voice_client:
                if channel.guild.voice_client.is_connected():
                    if channel.guild.voice_client.channel and channel.guild.voice_client.channel.id == channel.id:
                        # Already connected to the correct channel, nothing to do
                        logger.debug(f"Already connected to channel {channel.id} in guild {guild_id}")
                        return True
                    # Connected to wrong channel - need to disconnect first
                    logger.info(f"Disconnecting from wrong channel in guild {guild_id}")
                    try:
                        await channel.guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(2)
                    except Exception as e:
                        logger.error(f"Error disconnecting existing voice client: {str(e)}")
                        # Force cleanup via voice state
                        try:
                            await channel.guild.change_voice_state(channel=None)
                            await asyncio.sleep(3)
                        except:
                            pass
                else:
                    # Voice client exists but not connected - clean it up
                    logger.warning(f"Found disconnected voice client in guild {guild_id}, cleaning up")
                    try:
                        await channel.guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(1)
                    except:
                        pass
                    
            self.last_attempt[guild_id] = current_time
            try:
                # Disconnect from any existing voice connection first (if not already done)
                if channel.guild.voice_client and channel.guild.voice_client.is_connected():
                    try:
                        await channel.guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(2)
                    except Exception as e:
                        logger.error(f"Error disconnecting existing voice client: {str(e)}")
                        # Force cleanup via voice state
                        try:
                            await channel.guild.change_voice_state(channel=None)
                            await asyncio.sleep(3)
                        except:
                            pass

                # Double-check we're actually disconnected before attempting to connect
                if channel.guild.voice_client and channel.guild.voice_client.is_connected():
                    logger.error(f"Failed to disconnect existing voice client in guild {guild_id}")
                    return False

                # Final check before connecting
                if channel.guild.voice_client and channel.guild.voice_client.is_connected():
                    logger.error(f"Voice client still connected after disconnect attempts in guild {guild_id}")
                    return False

                # Try to connect
                try:
                    voice_client = await channel.connect(
                        timeout=45.0,
                        self_mute=True,
                        self_deaf=True,
                        reconnect=True
                    )
                    
                    # Wait and verify connection
                    await asyncio.sleep(2)
                    if voice_client and voice_client.is_connected():
                        voice_client.self_mute = True
                        voice_client.self_deaf = True
                        self.connection_attempts[guild_id] = 0
                        return True
                    
                except discord.ClientException as e:
                    # "Already connected to a voice channel" - try to recover
                    if "Already connected" in str(e):
                        logger.debug(f"Already connected error in guild {guild_id}, attempting recovery")
                        try:
                            # First check if we're already in the right channel
                            if channel.guild.voice_client and channel.guild.voice_client.is_connected():
                                if channel.guild.voice_client.channel and channel.guild.voice_client.channel.id == channel.id:
                                    logger.debug(f"Already in correct channel {channel.id} in guild {guild_id}")
                                    return True
                            
                            # Force disconnect and cleanup
                            await channel.guild.change_voice_state(channel=None)
                            await asyncio.sleep(3)
                            
                            # Try one more time after cleanup
                            try:
                                voice_client = await channel.connect(
                                    timeout=45.0,
                                    self_mute=True,
                                    self_deaf=True,
                                    reconnect=True
                                )
                                await asyncio.sleep(2)
                                if voice_client and voice_client.is_connected():
                                    voice_client.self_mute = True
                                    voice_client.self_deaf = True
                                    self.connection_attempts[guild_id] = 0
                                    logger.info(f"Successfully recovered connection in guild {guild_id}")
                                    return True
                            except discord.ClientException as retry_e:
                                if "Already connected" in str(retry_e):
                                    # Still getting this error, likely already connected
                                    logger.debug(f"Still connected after recovery attempt in guild {guild_id}")
                                    return True  # Treat as success since we're connected
                                logger.error(f"Retry connection failed: {str(retry_e)}")
                            except Exception as retry_e:
                                logger.error(f"Retry connection failed: {str(retry_e)}")
                        except Exception as cleanup_e:
                            logger.error(f"Cleanup failed: {str(cleanup_e)}")
                    else:
                        logger.error(f"ClientException during connection: {str(e)}")
                    return False
                except Exception as e:
                    logger.error(f"Connection failed: {str(e)}")
                    return False
                        
                return False
                    
            except asyncio.TimeoutError:
                logger.error(f"Connection attempt timed out for guild {guild_id}")
                self.connection_attempts[guild_id] += 1
                return False
            except Exception as e:
                logger.error(f"Error connecting to channel {channel.id} in guild {guild_id}: {str(e)}")
                self.connection_attempts[guild_id] += 1
                return False

class AlwaysVC(commands.Cog):
    """
    Always-VC System - Keeps bot in a designated voice channel
    
    Behavior:
    - If always-vc is CONFIGURED: Bot will rejoin the channel when disconnected
    - If always-vc is NOT CONFIGURED: Bot stays disconnected (no auto-rejoin)
    - Smart disconnect: Leaves when all humans exit (configurable)
    - Manual disconnect respect: Waits 60s before rejoining (configurable)
    
    Commands:
    - /always-vc - Setup or disable always-vc
    - /always-stats - View current configuration
    - /always-config - Configure behavior settings
    - /vc-pause - Temporarily pause auto-rejoin
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.db_folder = 'database'
        self.db_file = os.path.join(self.db_folder, 'vc_data.json')
        self.guild_configs = {}
        self.connection_manager = ConnectionManager()
        self.load_data()
        self._guild_locks = defaultdict(asyncio.Lock)
        self._rejoin_cooldown = defaultdict(float)
        self._ready = False
        
        # Start health check when bot is ready
        self.bot.loop.create_task(self._start_health_check())
        
    async def _start_health_check(self):
        await self.bot.wait_until_ready()
        if not self.health_check.is_running():
            self.health_check.start()

    def load_data(self):
        """Load guild configurations from file."""
        try:
            if not os.path.exists(self.db_folder):
                os.makedirs(self.db_folder)
                
            if os.path.isfile(self.db_file):
                with open(self.db_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.guild_configs = data.get('guild_configs', {})
            else:
                self.guild_configs = {}
                
        except Exception as e:
            logger.error(f"Error loading data: {str(e)}")
            self.guild_configs = {}

    def save_data(self):
        """Save guild configurations to file with backup."""
        try:
            # Create backup of current file if it exists
            if os.path.isfile(self.db_file):
                backup_path = f"{self.db_file}.backup"
                os.replace(self.db_file, backup_path)
            
            # Write new data
            with open(self.db_file, 'w', encoding='utf-8') as f:
                json.dump({'guild_configs': self.guild_configs}, f, indent=2)
            
        except Exception as e:
            logger.error(f"Error saving data: {str(e)}")
            # If we have a backup, restore it
            backup_path = f"{self.db_file}.backup"
            if os.path.isfile(backup_path):
                os.replace(backup_path, self.db_file)

    async def join_vc(self, guild, attempt=1, force_join=False):
        """Join a voice channel in the specified guild.
        
        Args:
            guild: The guild to join
            attempt: Current attempt number (for retry logic)
            force_join: If True, bypass the "humans in channel" check (used on startup)
        """
        guild_id = str(guild.id)
        max_attempts = 5
        
        try:
            # Get configuration
            config = self.guild_configs.get(guild_id)
            if not config:
                return
                
            vc_channel_id = config.get('vc_channel_id')
            if not vc_channel_id:
                return
            
            # EARLY EXIT: Already connected to the correct channel
            if guild.voice_client:
                if guild.voice_client.is_connected() and guild.voice_client.channel and guild.voice_client.channel.id == vc_channel_id:
                    # Already in the right place, nothing to do
                    return
                # If connected to wrong channel, disconnect first
                elif guild.voice_client.is_connected():
                    try:
                        await guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(2)
                    except:
                        pass
                
            # Add cooldown check to prevent spam (reduced to 10 seconds for faster reconnection)
            now = time.time()
            if now - self._rejoin_cooldown.get(guild_id, 0) < 10:
                return
            self._rejoin_cooldown[guild_id] = now

            vc_channel = self.bot.get_channel(vc_channel_id)
            if not vc_channel:
                config['vc_channel_id'] = None
                self.save_data()
                return
            
            # ALWAYS join the always-vc channel regardless of whether humans are present
            # The bot should NEVER leave the always-vc channel on its own

            # Check permissions first
            permissions = vc_channel.permissions_for(guild.me)
            if not permissions.connect or not permissions.speak:
                logger.error(f"Missing required permissions for channel {vc_channel_id} in guild {guild_id}")
                return

            async with self._guild_locks[guild_id]:
                # Check if we're already in the right channel
                if guild.voice_client and guild.voice_client.is_connected():
                    if guild.voice_client.channel and guild.voice_client.channel.id == vc_channel.id:
                        # Verify self mute and deafen state
                        if config.get('mute_on_join', False) and (not guild.voice_client.self_mute or not guild.voice_client.self_deaf):
                            guild.voice_client.self_mute = True
                            guild.voice_client.self_deaf = True
                        return

                # Apply configured join delay
                join_delay = config.get('join_delay', 3)
                if join_delay > 0:
                    await asyncio.sleep(join_delay)
                    
                # Attempt connection
                connected = await self.connection_manager.attempt_connection(guild_id, vc_channel)
                if connected:
                    if config.get('mute_on_join', False):
                        await asyncio.sleep(1)
                        if guild.voice_client:
                            guild.voice_client.self_mute = True
                            guild.voice_client.self_deaf = True
                else:
                    if attempt < max_attempts:
                        delay = (2 ** attempt)
                        await asyncio.sleep(delay)
                        await self.join_vc(guild, attempt + 1)
                    
        except discord.Forbidden as e:
            logger.error(f"Forbidden error in guild {guild_id}: {str(e)}")
        except discord.HTTPException as e:
            logger.error(f"HTTP error in guild {guild_id}: {str(e)}")
            if attempt < max_attempts:
                delay = (2 ** attempt)
                await asyncio.sleep(delay)
                await self.join_vc(guild, attempt + 1)
        except Exception as e:
            logger.error(f"Unexpected error in guild {guild_id}: {str(e)}")
            if attempt < max_attempts:
                delay = (2 ** attempt)
                await asyncio.sleep(delay)
                await self.join_vc(guild, attempt + 1)

    @commands.Cog.listener()
    async def on_ready(self):
        """Join configured always-vc channels on bot startup."""
        await asyncio.sleep(5)
        
        # Attempt to join configured voice channels
        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            config = self.guild_configs.get(guild_id, {})
            
            # Only join if:
            # 1. vc_channel_id is configured
            # 2. auto_rejoin is enabled (default: True)
            if config.get('vc_channel_id') and config.get('auto_rejoin', True):
                logger.debug(f"Joining always-vc for guild {guild.id} on startup")
                # Use force_join=True on startup to join even if channel is empty
                await self.join_vc(guild, force_join=True)
            else:
                logger.debug(f"Skipping guild {guild.id} - no always-vc configured or auto-rejoin disabled")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Handle bot's own voice state changes
        if member == self.bot.user:
            guild_id = str(before.channel.guild.id if before.channel else after.channel.guild.id)
            guild = before.channel.guild if before.channel else after.channel.guild
            config = self.guild_configs.get(guild_id)
            
            # If no config or auto-rejoin is disabled, don't do anything
            if not config or not config.get('auto_rejoin', True):
                return
                
            # If we're disconnected (moved from a channel to no channel)
            if before.channel and not after.channel:
                # Check if always-vc is actually configured
                vc_channel_id = config.get('vc_channel_id')
                if not vc_channel_id:
                    # No always-vc configured, stay disconnected
                    logger.debug(f"Bot disconnected in guild {guild_id}, no always-vc configured - staying disconnected")
                    return
                
                logger.debug(f"Bot disconnected in guild {guild_id} - Always-VC configured, will attempt rejoin")
                
                now = time.time()
                last_time = self._rejoin_cooldown.get(guild_id, 0)
                
                # Longer cooldown to prevent immediate rejoin after manual disconnect
                # This gives admins time to keep bot disconnected if they want
                if now - last_time < 30:  # Increased from 10 to 30 seconds
                    return
                    
                self._rejoin_cooldown[guild_id] = now
                
                # Wait before rejoining (manual disconnect)
                if config.get('respect_manual_disconnect', True):
                    logger.debug(f"Manual disconnect in guild {guild_id} - waiting 60 seconds before rejoin")
                    await asyncio.sleep(60)
                
                # Rejoin the configured always-vc channel
                await self.smart_rejoin(guild)
                
            # If we're moved to a different channel and we should be in a specific one
            elif after.channel and config.get('vc_channel_id'):
                if after.channel.id != config['vc_channel_id']:
                    await self.join_vc(guild)
            return
        
        # Handle when other members leave/join
        # Bot should NEVER leave the always-vc channel - it stays there permanently
        if not member.bot:
            guild_id = str(member.guild.id)
            config = self.guild_configs.get(guild_id)
            
            if not config or not config.get('auto_rejoin', True):
                return
            
            # Check if bot is in a voice channel
            if member.guild.voice_client and member.guild.voice_client.channel:
                vc = member.guild.voice_client.channel
                
                # Get the configured always-vc channel
                vc_channel_id = config.get('vc_channel_id')
                
                # If bot is in the ALWAYS-VC channel → ALWAYS STAY (never leave, even if alone)
                if vc_channel_id and vc.id == vc_channel_id:
                    logger.debug(f"Bot in always-vc channel in guild {guild_id} - staying permanently")
                    return
                
                # Count humans in the channel (only matters if bot is NOT in always-vc channel)
                human_members = [m for m in vc.members if not m.bot]
                
                # If no humans left and bot is in a DIFFERENT channel → Return to always-vc
                if not human_members and vc_channel_id:
                    logger.debug(f"Bot alone in non-always-vc channel in guild {guild_id} - returning to always-vc")
                    always_vc_channel = self.bot.get_channel(vc_channel_id)
                    if always_vc_channel:
                        try:
                            await member.guild.voice_client.move_to(always_vc_channel)
                        except:
                            # If move fails, disconnect and let health check rejoin
                            try:
                                await member.guild.voice_client.disconnect(force=True)
                            except:
                                pass

    async def smart_rejoin(self, guild, attempt=1):
        """Smart rejoin with exponential backoff - only rejoins if always-vc is configured."""
        max_attempts = 5
        if attempt > max_attempts:
            logger.debug(f"Max rejoin attempts reached for guild {guild.id}")
            return
        
        # Check if always-vc is still configured
        guild_id = str(guild.id)
        config = self.guild_configs.get(guild_id)
        if not config or not config.get('vc_channel_id'):
            logger.debug(f"No always-vc configured for guild {guild.id}, aborting rejoin")
            return
        
        await asyncio.sleep(2 ** attempt)
        await self.join_vc(guild, attempt)

    @tasks.loop(seconds=15)
    async def health_check(self):
        """Periodic check to ensure voice connections are maintained - runs every 15 seconds."""
        try:
            for guild_id, config in list(self.guild_configs.items()):
                if not config.get('auto_rejoin', True):
                    continue
                    
                try:
                    guild = self.bot.get_guild(int(guild_id))
                    if not guild:
                        continue
                        
                    vc_channel_id = config.get('vc_channel_id')
                    if not vc_channel_id:
                        continue
                        
                    channel = guild.get_channel(vc_channel_id)
                    if not channel:
                        config['vc_channel_id'] = None
                        self.save_data()
                        continue
                    
                    # Already connected to the correct channel - nothing to do
                    if guild.voice_client:
                        if guild.voice_client.is_connected() and guild.voice_client.channel and guild.voice_client.channel.id == vc_channel_id:
                            # Just verify mute/deaf state if needed
                            if config.get('mute_on_join', False):
                                if hasattr(guild.voice_client, 'self_mute'):
                                    if not guild.voice_client.self_mute or not guild.voice_client.self_deaf:
                                        guild.voice_client.self_mute = True
                                        guild.voice_client.self_deaf = True
                            continue
                        
                    # Not connected or in wrong channel - attempt to join
                    await self.join_vc(guild, force_join=True)
                        
                except Exception as e:
                    logger.error(f"Error in health check for guild {guild_id}: {str(e)}")
                    
        except Exception as e:
            logger.error(f"Error in health check: {str(e)}")

    async def cog_unload(self):
        """Cleanup when cog is unloaded."""
        try:
            self.health_check.cancel()
            
            for guild in self.bot.guilds:
                if guild.voice_client:
                    await guild.voice_client.disconnect(force=True)
            
            self.save_data()
        except Exception as e:
            logger.error(f"Error during cog unload: {str(e)}")

    async def cog_load(self):
        """Setup when cog is loaded."""
        try:
            os.makedirs(self.db_folder, exist_ok=True)
            self.load_data()
        except Exception as e:
            logger.error(f"Error during cog load: {str(e)}")
            raise

    @app_commands.command(name='always-vc', description='Setup or stop the bot staying in a VC')
    @app_commands.describe(channel="Voice channel to always stay in")
    @app_commands.default_permissions(administrator=True)
    async def always_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer()
        
        try:
            guild_id = str(interaction.guild.id)
            config = self.guild_configs.get(guild_id, {})
            current_channel_id = config.get('vc_channel_id')
            
            # Verify permissions first
            permissions = channel.permissions_for(interaction.guild.me)
            if not permissions.connect or not permissions.speak:
                await interaction.followup.send(f"❌ I don't have permission to join **{channel.name}**!")
                logger.error(f"Missing permissions for channel {channel.id} in guild {guild_id}")
                return

            # If we're already set to this channel, stop staying in it (toggle off)
            if current_channel_id == channel.id:
                config['vc_channel_id'] = None
                config['auto_rejoin'] = False
                self.guild_configs[guild_id] = config
                self.save_data()
                
                # Disconnect if currently connected
                if interaction.guild.voice_client:
                    await interaction.guild.voice_client.disconnect(force=True)
                    await interaction.followup.send(
                        f"✅ Disabled always-vc and disconnected from **{channel.name}**\n"
                        f"-# Bot will no longer auto-rejoin when disconnected"
                    )
                else:
                    await interaction.followup.send(
                        f"✅ Disabled always-vc for **{channel.name}**\n"
                        f"-# Bot will no longer auto-rejoin when disconnected"
                    )
                return

            # Set up new channel configuration
            config['vc_channel_id'] = channel.id
            config['auto_rejoin'] = True
            config.setdefault('mute_on_join', True)
            config.setdefault('join_delay', 5)
            self.guild_configs[guild_id] = config
            self.save_data()
            
            # Disconnect from current channel if in a different one
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect(force=True)
                await asyncio.sleep(2)
            
            await interaction.followup.send(f"🔄 Connecting to VC **{channel.name}**...")
            
            # Attempt connection with retry logic
            for attempt in range(3):
                try:
                    voice_client = await channel.connect(timeout=15.0, self_mute=True, self_deaf=True, reconnect=True)
                    if voice_client and voice_client.is_connected():
                        voice_client.self_mute = True
                        voice_client.self_deaf = True
                        await interaction.followup.send(f"✅ Successfully connected to VC **{channel.name}**!")
                        return
                except Exception as e:
                    logger.error(f"Connection attempt {attempt + 1} failed: {str(e)}")
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
            
            logger.error(f"All connection attempts failed for channel {channel.name} ({channel.id})")
            await interaction.followup.send(f"⚠️ Failed to connect to VC **{channel.name}**. Will keep trying...")
            
        except Exception as e:
            logger.error(f"Error in always-vc command: {str(e)}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")

    @app_commands.command(name='always-stats', description='Show voice channel statistics')
    @app_commands.default_permissions(administrator=True)
    async def vc_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            guild_id = str(interaction.guild.id)
            config = self.guild_configs.get(guild_id)
            if not config or not config.get('vc_channel_id'):
                await interaction.followup.send(
                    "❌ **No always-vc configured for this server**\n\n"
                    "**What this means:**\n"
                    "• Bot will NOT auto-rejoin when disconnected\n"
                    "• Bot will stay disconnected until manually moved\n\n"
                    "**To enable always-vc:**\n"
                    "Use `/always-vc channel:#your-channel`"
                )
                return
                
            embed = discord.Embed(title="🎤 Voice Channel Configuration", color=0x00ff00)
            chan_id = config['vc_channel_id']
            
            embed.add_field(name="📍 Current Channel", value=f"<#{chan_id}>", inline=False)
            embed.add_field(name="🔄 Auto-Rejoin", value="✅ Enabled" if config.get('auto_rejoin', True) else "❌ Disabled", inline=True)
            embed.add_field(name="🔇 Mute on Join", value="✅ Enabled" if config.get('mute_on_join', False) else "❌ Disabled", inline=True)
            embed.add_field(name="⏲️ Join Delay", value=f"{config.get('join_delay', 3)}s", inline=True)
            embed.add_field(name="👤 Stay When Alone", value="✅ Yes" if config.get('stay_when_alone', True) else "❌ No (leaves when empty)", inline=True)
            embed.add_field(name="🚪 Join When Empty", value="✅ Yes" if config.get('join_when_empty', False) else "❌ No (waits for humans)", inline=True)
            embed.add_field(name="✋ Respect Manual Disconnect", value="✅ Yes (waits 60s)" if config.get('respect_manual_disconnect', True) else "❌ No (rejoins quickly)", inline=True)
            
            # Show current status
            if interaction.guild.voice_client:
                embed.add_field(name="Current Status", value=f"🔊 Connected in {interaction.guild.voice_client.channel.mention}", inline=False)
            else:
                embed.add_field(name="Current Status", value="❌ Not connected", inline=False)
            
            embed.set_footer(text="Use /always-config to change settings")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in vc_stats: {str(e)}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")

    @app_commands.command(name='always-config', description='Configure bot settings')
    @app_commands.describe(
        setting="Setting to change",
        value="New value for the setting"
    )
    @app_commands.choices(setting=[
        app_commands.Choice(name="🔄 Auto Rejoin", value="auto_rejoin"),
        app_commands.Choice(name="🔇 Mute on Join", value="mute_on_join"),
        app_commands.Choice(name="⏲️ Join Delay", value="join_delay"),
        app_commands.Choice(name="👤 Stay When Alone", value="stay_when_alone"),
        app_commands.Choice(name="🚪 Join When Empty", value="join_when_empty"),
        app_commands.Choice(name="✋ Respect Manual Disconnect", value="respect_manual_disconnect")
    ])
    @app_commands.default_permissions(administrator=True)
    async def vc_config(self, interaction: discord.Interaction, setting: app_commands.Choice[str], value: str):
        await interaction.response.defer()
        
        try:
            guild_id = str(interaction.guild.id)
            setting_name = setting.value
            
            # Get or create config for this guild
            config = self.guild_configs.get(guild_id, {})
            
            if setting_name in ['auto_rejoin', 'mute_on_join', 'stay_when_alone', 'join_when_empty', 'respect_manual_disconnect']:
                # Handle boolean settings
                if value.lower() not in ['true', 'false', 'enabled', 'disabled', 'on', 'off', 'yes', 'no']:
                    await interaction.followup.send(f"❌ For {setting_name}, use: true/false, enabled/disabled, on/off, or yes/no")
                    return
                config[setting_name] = value.lower() in ['true', 'enabled', 'on', 'yes']
                display_value = "✅ Enabled" if config[setting_name] else "❌ Disabled"
                
            elif setting_name == 'join_delay':
                # Handle numeric setting
                try:
                    delay = int(value)
                    if not (0 <= delay <= 60):
                        await interaction.followup.send("❌ Join Delay must be between 0 and 60 seconds")
                        return
                    config[setting_name] = delay
                    display_value = f"{delay} seconds"
                except ValueError:
                    await interaction.followup.send("❌ Join Delay must be a number between 0 and 60")
                    return
            
            self.guild_configs[guild_id] = config
            self.save_data()
            
            await interaction.followup.send(f"✅ Updated **{setting.name}** to {display_value}")
            
        except Exception as e:
            logger.error(f"Error in vc_config: {str(e)}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")

    @app_commands.command(name='vc-backup', description='Create configuration backup')
    @app_commands.default_permissions(administrator=True)
    async def backup_config(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = os.path.join(self.db_folder, f'backup_{timestamp}.json')
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(self.guild_configs, f, indent=2)
            await interaction.followup.send("✅ Configuration backup created successfully!")
        except Exception as e:
            logger.error(f"Error creating backup: {str(e)}")
            await interaction.followup.send(f"❌ An error occurred while creating backup: {str(e)}")
    
    @app_commands.command(name='vc-pause', description='Temporarily pause auto-rejoin for a duration')
    @app_commands.describe(minutes="Minutes to pause (default: 5, max: 60)")
    @app_commands.default_permissions(administrator=True)
    async def pause_rejoin(self, interaction: discord.Interaction, minutes: int = 5):
        await interaction.response.defer()
        
        try:
            guild_id = str(interaction.guild.id)
            
            # Validate duration
            if minutes < 1 or minutes > 60:
                await interaction.followup.send("❌ Duration must be between 1 and 60 minutes")
                return
            
            # Set cooldown to prevent rejoin
            pause_duration = minutes * 60
            self._rejoin_cooldown[guild_id] = time.time() + pause_duration
            
            # Disconnect if currently connected
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect(force=True)
                await interaction.followup.send(
                    f"✅ Disconnected and paused auto-rejoin for **{minutes} minutes**\n"
                    f"-# Bot will not rejoin until <t:{int(time.time() + pause_duration)}:t>"
                )
            else:
                await interaction.followup.send(
                    f"✅ Paused auto-rejoin for **{minutes} minutes**\n"
                    f"-# Bot will not rejoin until <t:{int(time.time() + pause_duration)}:t>"
                )
                
        except Exception as e:
            logger.error(f"Error in vc-pause: {str(e)}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")

async def setup(bot):
    cog = AlwaysVC(bot)
    await bot.add_cog(cog)