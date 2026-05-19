from __future__ import annotations

import asyncio
import functools
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

logger = logging.getLogger("MusicBot.music")

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass
class Song:
    title: str
    url: str
    stream_url: str
    duration: int
    thumbnail: Optional[str]
    requester: discord.Member

    @property
    def duration_str(self) -> str:
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class GuildPlayer:
    queue: deque[Song] = field(default_factory=deque)
    current: Optional[Song] = None
    volume: float = 0.5
    loop: bool = False


class Music(commands.Cog):
    """Music commands for playing YouTube audio in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer()
        return self.players[guild_id]

    async def extract_info(self, query: str) -> dict:
        loop = asyncio.get_event_loop()
        func = functools.partial(self.ytdl.extract_info, query, download=False)
        return await loop.run_in_executor(None, func)

    async def create_song(self, data: dict, requester: discord.Member) -> Song:
        if "entries" in data:
            data = data["entries"][0]
        return Song(
            title=data.get("title", "Unknown"),
            url=data.get("webpage_url", ""),
            stream_url=data.get("url", ""),
            duration=data.get("duration", 0) or 0,
            thumbnail=data.get("thumbnail"),
            requester=requester,
        )

    def play_next(self, guild: discord.Guild):
        player = self.get_player(guild.id)

        if player.loop and player.current:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(player.current.stream_url, **FFMPEG_OPTIONS),
                volume=player.volume,
            )
            guild.voice_client.play(source, after=lambda e: self.play_next(guild))
            return

        if not player.queue:
            player.current = None
            asyncio.run_coroutine_threadsafe(
                self.auto_disconnect(guild), self.bot.loop
            )
            return

        player.current = player.queue.popleft()
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(player.current.stream_url, **FFMPEG_OPTIONS),
            volume=player.volume,
        )
        guild.voice_client.play(source, after=lambda e: self.play_next(guild))

    async def auto_disconnect(self, guild: discord.Guild):
        """Disconnect after 3 minutes of inactivity."""
        await asyncio.sleep(180)
        player = self.get_player(guild.id)
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and player.current is None:
            await vc.disconnect()
            self.players.pop(guild.id, None)

    def create_now_playing_embed(self, song: Song) -> discord.Embed:
        embed = discord.Embed(
            title="Now Playing",
            description=f"[{song.title}]({song.url})",
            color=discord.Color.green(),
        )
        embed.add_field(name="Duration", value=song.duration_str, inline=True)
        embed.add_field(
            name="Requested by", value=song.requester.display_name, inline=True
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        return embed

    @staticmethod
    async def ensure_voice(
        interaction: discord.Interaction,
    ) -> Optional[discord.VoiceClient]:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You must be in a voice channel to use this command.",
                ephemeral=True,
            )
            return None

        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        return vc

    # ── Slash Commands ──────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song from YouTube (URL or search)")
    @app_commands.describe(query="YouTube URL or search keywords")
    async def play(self, interaction: discord.Interaction, query: str):
        vc = await self.ensure_voice(interaction)
        if vc is None:
            return

        await interaction.response.defer()

        try:
            data = await self.extract_info(query)
        except Exception:
            await interaction.followup.send("Could not find or process the requested song.")
            return

        song = await self.create_song(data, interaction.user)
        player = self.get_player(interaction.guild.id)

        if vc.is_playing() or vc.is_paused():
            player.queue.append(song)
            embed = discord.Embed(
                title="Added to Queue",
                description=f"[{song.title}]({song.url})",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Position", value=str(len(player.queue)), inline=True)
            embed.add_field(name="Duration", value=song.duration_str, inline=True)
            await interaction.followup.send(embed=embed)
        else:
            player.current = song
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.stream_url, **FFMPEG_OPTIONS),
                volume=player.volume,
            )
            vc.play(source, after=lambda e: self.play_next(interaction.guild))
            await interaction.followup.send(embed=self.create_now_playing_embed(song))

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            player = self.get_player(interaction.guild.id)
            player.loop = False
            vc.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            player = self.get_player(interaction.guild.id)
            player.queue.clear()
            player.current = None
            player.loop = False
            vc.stop()
            await vc.disconnect()
            self.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("Stopped and disconnected.")
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the current song queue")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)

        if not player.current and not player.queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        embed = discord.Embed(title="Music Queue", color=discord.Color.purple())

        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"[{player.current.title}]({player.current.url}) — {player.current.duration_str}",
                inline=False,
            )

        if player.queue:
            queue_list = []
            for i, song in enumerate(player.queue, start=1):
                queue_list.append(f"`{i}.` [{song.title}]({song.url}) — {song.duration_str}")
                if i >= 10:
                    remaining = len(player.queue) - 10
                    if remaining > 0:
                        queue_list.append(f"*...and {remaining} more*")
                    break
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)

        embed.set_footer(text=f"Total songs in queue: {len(player.queue)}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if player.current:
            await interaction.response.send_message(
                embed=self.create_now_playing_embed(player.current)
            )
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="volume", description="Set the playback volume (0-100)")
    @app_commands.describe(level="Volume level from 0 to 100")
    async def volume(self, interaction: discord.Interaction, level: int):
        if not 0 <= level <= 100:
            await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True
            )
            return

        player = self.get_player(interaction.guild.id)
        player.volume = level / 100.0

        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = player.volume

        await interaction.response.send_message(f"Volume set to **{level}%**.")

    @app_commands.command(name="loop", description="Toggle loop for the current song")
    async def loop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.loop = not player.loop
        state = "enabled" if player.loop else "disabled"
        await interaction.response.send_message(f"Loop **{state}**.")

    # ── Prefix Commands (fallback) ──────────────────────────────────

    @commands.command(name="play", aliases=["p"])
    async def play_prefix(self, ctx: commands.Context, *, query: str):
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You must be in a voice channel.")
            return

        channel = ctx.author.voice.channel
        vc = ctx.voice_client

        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        async with ctx.typing():
            try:
                data = await self.extract_info(query)
            except Exception:
                await ctx.send("Could not find or process the requested song.")
                return

            song = await self.create_song(data, ctx.author)
            player = self.get_player(ctx.guild.id)

            if vc.is_playing() or vc.is_paused():
                player.queue.append(song)
                embed = discord.Embed(
                    title="Added to Queue",
                    description=f"[{song.title}]({song.url})",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Position", value=str(len(player.queue)), inline=True)
                embed.add_field(name="Duration", value=song.duration_str, inline=True)
                await ctx.send(embed=embed)
            else:
                player.current = song
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(song.stream_url, **FFMPEG_OPTIONS),
                    volume=player.volume,
                )
                vc.play(source, after=lambda e: self.play_next(ctx.guild))
                await ctx.send(embed=self.create_now_playing_embed(song))

    @commands.command(name="pause")
    async def pause_prefix(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Paused.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.command(name="resume", aliases=["r"])
    async def resume_prefix(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.command(name="skip", aliases=["s"])
    async def skip_prefix(self, ctx: commands.Context):
        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            player = self.get_player(ctx.guild.id)
            player.loop = False
            ctx.voice_client.stop()
            await ctx.send("Skipped.")
        else:
            await ctx.send("Nothing to skip.")

    @commands.command(name="stop", aliases=["dc", "disconnect", "leave"])
    async def stop_prefix(self, ctx: commands.Context):
        if ctx.voice_client:
            player = self.get_player(ctx.guild.id)
            player.queue.clear()
            player.current = None
            player.loop = False
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            self.players.pop(ctx.guild.id, None)
            await ctx.send("Stopped and disconnected.")
        else:
            await ctx.send("Not connected.")

    @commands.command(name="queue", aliases=["q"])
    async def queue_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player.current and not player.queue:
            await ctx.send("The queue is empty.")
            return

        embed = discord.Embed(title="Music Queue", color=discord.Color.purple())
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"[{player.current.title}]({player.current.url}) — {player.current.duration_str}",
                inline=False,
            )
        if player.queue:
            queue_list = []
            for i, song in enumerate(player.queue, start=1):
                queue_list.append(f"`{i}.` [{song.title}]({song.url}) — {song.duration_str}")
                if i >= 10:
                    remaining = len(player.queue) - 10
                    if remaining > 0:
                        queue_list.append(f"*...and {remaining} more*")
                    break
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)

        embed.set_footer(text=f"Total songs in queue: {len(player.queue)}")
        await ctx.send(embed=embed)

    @commands.command(name="np", aliases=["nowplaying"])
    async def nowplaying_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.current:
            await ctx.send(embed=self.create_now_playing_embed(player.current))
        else:
            await ctx.send("Nothing is playing.")

    @commands.command(name="volume", aliases=["vol"])
    async def volume_prefix(self, ctx: commands.Context, level: int):
        if not 0 <= level <= 100:
            await ctx.send("Volume must be between 0 and 100.")
            return
        player = self.get_player(ctx.guild.id)
        player.volume = level / 100.0
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = player.volume
        await ctx.send(f"Volume set to **{level}%**.")

    @commands.command(name="loop")
    async def loop_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        player.loop = not player.loop
        state = "enabled" if player.loop else "disabled"
        await ctx.send(f"Loop **{state}**.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
