import discord
from discord.ext import commands, tasks
import asyncio
import logging
import os
from datetime import datetime

# Ensure the logs folder exists
logs_dir = "logs"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

# Configure logging for errors only
logging.basicConfig(
    filename=os.path.join(logs_dir, "bot.log"),
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

class StatusCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_index = 0

    def format_status(self, text: str) -> str:
        """Replace placeholders with actual values."""
        try:
            total_members = sum(g.member_count or 0 for g in self.bot.guilds)
            total_servers = len(self.bot.guilds)
            total_channels = sum(len(g.channels) for g in self.bot.guilds)
            total_voice = sum(len(g.voice_channels) for g in self.bot.guilds)
            total_text = sum(len(g.text_channels) for g in self.bot.guilds)
            total_commands = len(self.bot.tree.get_commands())
            total_cogs = len(self.bot.cogs)
            latency = round(self.bot.latency * 1000)
            uptime = "24/7"
            
            replacements = {
                "{members}": f"{total_members:,}",
                "{servers}": str(total_servers),
                "{guilds}": str(total_servers),
                "{channels}": str(total_channels),
                "{voice_channels}": str(total_voice),
                "{text_channels}": str(total_text),
                "{commands}": str(total_commands),
                "{cogs}": str(total_cogs),
                "{latency}": f"{latency}ms",
                "{ping}": f"{latency}ms",
                "{uptime}": uptime,
                "{date}": datetime.now().strftime("%d/%m/%Y"),
                "{time}": datetime.now().strftime("%H:%M"),
                "{bot}": self.bot.user.name if self.bot.user else "Nescafe",
            }
            
            for placeholder, value in replacements.items():
                text = text.replace(placeholder, value)
            
            return text
        except Exception:
            return text

    @tasks.loop(seconds=30)
    async def status_cycle(self):
        """Cycles through streaming status messages from text.txt."""
        try:
            if not os.path.exists("text.txt"):
                return

            with open("text.txt", "r", encoding="utf-8") as file:
                lines = [line.strip() for line in file.readlines() if line.strip()]

            if not lines:
                return

            # Get current status and move to next
            status_text = lines[self.current_index % len(lines)]
            self.current_index = (self.current_index + 1) % len(lines)
            
            # Format placeholders
            status_text = self.format_status(status_text)
            
            await self.change_status(status_text)

        except Exception as e:
            logging.error(f"Error in status_cycle: {e}")

    async def change_status(self, message):
        """Changes the bot's streaming status with purple indicator."""
        try:
            if self.bot.is_closed():
                return
            
            # Use Streaming activity for purple indicator
            activity = discord.Streaming(
                name=message,
                url="https://twitch.tv/discord"
            )
            await self.bot.change_presence(activity=activity)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limit hit
                retry_after = int(e.response.headers.get('Retry-After', 5))
                await asyncio.sleep(retry_after)
                if not self.bot.is_closed():  # Check again before retry
                    await self.change_status(message)  # Retry after waiting
        except (ConnectionError, OSError) as e:
            # Ignore connection errors during shutdown
            error_msg = str(e).lower()
            if not self.bot.is_closed() and 'closing transport' not in error_msg:
                logging.error(f"Connection error in change_status: {e}")
        except Exception as e:
            # Ignore errors during shutdown or transport closing
            error_msg = str(e).lower()
            if not self.bot.is_closed() and 'closing transport' not in error_msg:
                logging.error(f"Error in change_status: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        """Starts the status cycling when the bot is ready."""
        try:
            if not self.status_cycle.is_running():
                self.status_cycle.start()
        except Exception as e:
            logging.error(f"Error in on_ready: {e}")
    
    async def cog_unload(self):
        """Stop the status cycle task when cog is unloaded."""
        if self.status_cycle.is_running():
            self.status_cycle.cancel()

# Setup function to load the cog
async def setup(bot):
    await bot.add_cog(StatusCog(bot))
