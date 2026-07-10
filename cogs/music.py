import asyncio
import discord
from discord.ext import commands
import wavelink
from typing import cast, Optional
import re

URL_REGEX = re.compile(r"https?://(?:www\.)?.+")

PLATFORM_HELP = """
**Supported platforms** (auto-detected from URLs):
• YouTube (`ytsearch:`)
• YouTube Music (`ytmsearch:`)
• SoundCloud (`scsearch:`)
• Spotify (`spsearch:`)
• Apple Music (`amsearch:`)
• Deezer (`dzsearch:`)
• Amazon Music (`amzsearch:`)
• Tidal (`tdsearch:`)
• Qobuz (`qbsearch:`)
• Pandora (`pdsearch:`)
• Shazam (`szsearch:`)
• Yandex Music (`ymsearch:`)
• VK Music (`vksearch:`)
• JioSaavn (`jssearch:`)
• Audiomack (`admsearch:`)
• Gaana (`gnsearch:`)
• Bandcamp (`bcsearch:`)
• NicoNico (`ncsearch:`)
• Mixcloud (`mcsearch:`)
• Bilibili (`bilibili:`)
• Flowery TTS (`ftts:`)

For text searches, use a prefix like `spsearch:Bohemian Rhapsody` to search Spotify, or just type the name for YouTube.
"""


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.loop.create_task(self._connect_lavalink())

    async def _connect_lavalink(self) -> None:
        import os
        host = os.getenv("LAVALINK_HOST", "localhost")
        port = os.getenv("LAVALINK_PORT", "2333")
        password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
        nodes = [
            wavelink.Node(
                uri=f"http://{host}:{port}",
                password=password,
            )
        ]
        try:
            await asyncio.wait_for(
                wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100),
                timeout=20
            )
            print(f"Lavalink connected ({host}:{port})")
        except asyncio.TimeoutError:
            print(f"WARNING: Lavalink connection timed out ({host}:{port})")
        except Exception as e:
            print(f"WARNING: Lavalink connection failed: {e}")

    async def cog_unload(self) -> None:
        await wavelink.Pool.close()

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        print(f"Lavalink node {payload.node!r} connected")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not player:
            return
        track = payload.track
        embed = discord.Embed(
            title="Now Playing",
            description=f"**[{track.title}]({track.uri})** by `{track.author}`",
            color=discord.Color.green()
        )
        if track.artwork:
            embed.set_image(url=track.artwork)
        if track.album.name:
            embed.add_field(name="Album", value=track.album.name)
        embed.add_field(name="Source", value=track.source.capitalize(), inline=True)
        embed.add_field(name="Duration", value=format_duration(track.length), inline=True)
        home = getattr(player, "home", None)
        if home:
            await home.send(embed=embed)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if not player:
            return
        if not player.queue.is_empty and not player.playing:
            next_track = player.queue.get()
            await player.play(next_track)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if not player:
            return
        home = getattr(player, "home", None)
        msg = str(getattr(payload.exception, "message", payload.exception))[:200]
        if home:
            await home.send(f"Track error: {msg}")
        if not player.queue.is_empty:
            next_track = player.queue.get()
            await player.play(next_track)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        home = getattr(player, "home", None)
        if home:
            await home.send("Leaving voice channel due to inactivity.")
        await player.disconnect()

    async def get_player(self, ctx: commands.Context) -> Optional[wavelink.Player]:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send("You need to be in a voice channel first.")
                return None
            try:
                player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
            except discord.ClientException:
                await ctx.send("Could not join your voice channel.")
                return None
        if not hasattr(player, "home"):
            player.home = ctx.channel
        elif player.home != ctx.channel:
            await ctx.send(f"Music commands are locked to {player.home.mention}.")
            return None
        return player

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add it to the queue")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        await ctx.defer()
        player = await self.get_player(ctx)
        if not player:
            return

        player.autoplay = wavelink.AutoPlayMode.enabled

        prefixes = ("ytsearch:", "ytmsearch:", "spsearch:", "amsearch:", "dzsearch:", "amzsearch:", "tdsearch:", "qbsearch:", "pdsearch:", "szsearch:", "ymsearch:", "vksearch:", "jssearch:", "admsearch:", "gnsearch:", "bcsearch:", "ncsearch:", "mcsearch:", "bilibili:", "ftts:", "scsearch:")
        if not (URL_REGEX.match(query) or query.lower().startswith(prefixes)):
            query = f"ytsearch:{query}"

        tracks = await wavelink.Playable.search(query)
        if not tracks:
            await ctx.send(f"No results found for `{query}`.")
            return

        if isinstance(tracks, wavelink.Playlist):
            added = await player.queue.put_wait(tracks)
            await ctx.send(f"Added playlist **`{tracks.name}`** ({added} tracks) to the queue.")
        else:
            track = tracks[0]
            await player.queue.put_wait(track)
            await ctx.send(f"Added **`{track}`** to the queue.")

        if not player.playing:
            next_track = player.queue.get()
            await player.play(next_track, volume=30)

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current track")
    async def skip(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        await player.skip(force=True)
        await ctx.send("Skipped.")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        player.queue.clear()
        await player.stop()
        await ctx.send("Stopped and cleared queue.")

    @commands.hybrid_command(name="pause", description="Pause playback")
    async def pause(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        if player.paused:
            await ctx.send("Already paused.")
            return
        await player.pause(True)
        await ctx.send("Paused.")

    @commands.hybrid_command(name="resume", description="Resume playback")
    async def resume(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        if not player.paused:
            await ctx.send("Already playing.")
            return
        await player.pause(False)
        await ctx.send("Resumed.")

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Set the volume (0-100)")
    async def volume(self, ctx: commands.Context, value: int) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        if value < 0 or value > 100:
            await ctx.send("Volume must be between 0 and 100.")
            return
        await player.set_volume(value)
        await ctx.send(f"Volume set to {value}%.")

    @commands.hybrid_command(name="now", aliases=["np", "current"], description="Show the currently playing track")
    async def now(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player or not player.playing:
            await ctx.send("Nothing is playing right now.")
            return
        track = player.current
        embed = discord.Embed(
            title="Now Playing",
            description=f"**[{track.title}]({track.uri})** by `{track.author}`",
            color=discord.Color.green()
        )
        if track.artwork:
            embed.set_image(url=track.artwork)
        if track.album.name:
            embed.add_field(name="Album", value=track.album.name)
        embed.add_field(name="Source", value=track.source.capitalize(), inline=True)
        embed.add_field(name="Duration", value=format_duration(track.length), inline=True)
        embed.add_field(name="Position", value=format_duration(player.position), inline=True)
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)
        embed.add_field(name="Queue", value=player.queue.count, inline=True)
        embed.add_field(name="Loop", value="Enabled" if player.queue.loop else "Disabled", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the upcoming queue")
    async def queue(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        if player.queue.is_empty:
            await ctx.send("Queue is empty.")
            return
        entries = []
        for i, track in enumerate(player.queue, 1):
            entries.append(f"`{i}.` **{track.title}** - `{track.author}`")
        pages = []
        for i in range(0, len(entries), 10):
            chunk = entries[i:i+10]
            embed = discord.Embed(
                title=f"Queue ({player.queue.count} tracks)",
                description="\n".join(chunk),
                color=discord.Color.blue()
            )
            if player.current:
                embed.add_field(
                    name="Now Playing",
                    value=f"**{player.current.title}** - `{player.current.author}`",
                    inline=False
                )
            pages.append(embed)
        for embed in pages:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        player.queue.shuffle()
        await ctx.send("Queue shuffled.")

    @commands.hybrid_command(name="loop", description="Toggle loop for the current queue")
    async def loop(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        player.queue.loop = not player.queue.loop
        await ctx.send(f"Loop {'enabled' if player.queue.loop else 'disabled'}.")

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"], description="Disconnect from the voice channel")
    async def disconnect(self, ctx: commands.Context) -> None:
        player = cast(Optional[wavelink.Player], ctx.voice_client)
        if not player:
            await ctx.send("Not connected to a voice channel.")
            return
        await player.disconnect()
        await ctx.send("Disconnected.")

    @commands.hybrid_command(name="platforms", description="Show all supported music platforms")
    async def platforms(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Supported Music Platforms",
            description=PLATFORM_HELP,
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)


def format_duration(ms: int) -> str:
    seconds = ms // 1000
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
