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
    "age_limit": 100,
    "geo_bypass": True,
}

FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
    " -probesize 200k -analyzeduration 500k"
)
FFMPEG_OPTIONS = {
    "before_options": FFMPEG_BEFORE,
    "options": "-vn -b:a 128k -bufsize 256k -ac 2 -ar 48000",
}

DELETE_AFTER = 15


@dataclass
class Song:
    title: str
    url: str
    webpage_url: str
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
    history: list[Song] = field(default_factory=list)
    current: Optional[Song] = None
    volume: float = 0.5
    loop: bool = False


# ── Player Control Buttons ──────────────────────────────────────


class PlayerView(discord.ui.View):
    """Persistent buttons: Previous / Pause-Resume / Next / Stop / Vol− / Vol+"""

    def __init__(self, cog: Music):
        super().__init__(timeout=None)
        self.cog = cog

    def _player(self, guild_id: int) -> GuildPlayer:
        return self.cog.get_player(guild_id)

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, custom_id="music:prev")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction.guild.id)
        if not player.history:
            await interaction.response.send_message("No previous song.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        if player.current:
            player.queue.appendleft(player.current)
        prev_song = player.history.pop()
        player.current = prev_song
        player.loop = False
        vc.stop()
        await interaction.response.send_message(
            f"⏮ **{prev_song.title}**", ephemeral=True
        )

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.primary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, custom_id="music:next")
    async def next_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            player = self._player(interaction.guild.id)
            player.loop = False
            vc.stop()
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            player = self._player(interaction.guild.id)
            player.queue.clear()
            player.history.clear()
            player.current = None
            player.loop = False
            vc.stop()
            await vc.disconnect()
            self.cog.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("⏹ Stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="music:voldown")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction.guild.id)
        player.volume = max(0.0, player.volume - 0.1)
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = player.volume
        pct = int(player.volume * 100)
        await interaction.response.send_message(f"🔉 Volume: **{pct}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music:volup")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._player(interaction.guild.id)
        player.volume = min(1.0, player.volume + 0.1)
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = player.volume
        pct = int(player.volume * 100)
        await interaction.response.send_message(f"🔊 Volume: **{pct}%**", ephemeral=True)


# ── Music Cog ───────────────────────────────────────────────────


class Music(commands.Cog):
    """Music commands for playing YouTube audio in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        self.player_view = PlayerView(self)
        bot.add_view(self.player_view)

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer()
        return self.players[guild_id]

    async def extract_info(self, query: str) -> dict:
        loop = asyncio.get_event_loop()
        func = functools.partial(self.ytdl.extract_info, query, download=False)
        return await loop.run_in_executor(None, func)

    async def get_stream_url(self, song: Song) -> Optional[str]:
        """Re-extract to get a fresh stream URL right before playing."""
        try:
            data = await self.extract_info(song.webpage_url or song.url)
            if "entries" in data:
                data = data["entries"][0]
            return data.get("url")
        except Exception:
            logger.exception("Failed to get stream URL for: %s", song.title)
            return None

    async def create_song(self, data: dict, requester: discord.Member) -> Song:
        if "entries" in data:
            data = data["entries"][0]
        return Song(
            title=data.get("title", "Unknown"),
            url=data.get("url", ""),
            webpage_url=data.get("webpage_url", ""),
            duration=data.get("duration", 0) or 0,
            thumbnail=data.get("thumbnail"),
            requester=requester,
        )

    def play_next(self, guild: discord.Guild, error: Exception = None):
        if error:
            logger.error("Playback error: %s", error)

        player = self.get_player(guild.id)

        if player.loop and player.current:
            asyncio.run_coroutine_threadsafe(
                self._play_song(guild, player, player.current), self.bot.loop
            )
            return

        if player.current:
            player.history.append(player.current)
            if len(player.history) > 50:
                player.history.pop(0)

        if not player.queue:
            player.current = None
            asyncio.run_coroutine_threadsafe(
                self.auto_disconnect(guild), self.bot.loop
            )
            return

        player.current = player.queue.popleft()
        asyncio.run_coroutine_threadsafe(
            self._play_song(guild, player, player.current), self.bot.loop
        )

    async def _play_song(self, guild: discord.Guild, player: GuildPlayer, song: Song):
        """Get a fresh stream URL and start playing."""
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        stream_url = await self.get_stream_url(song)
        if not stream_url:
            logger.error("Could not get stream URL for: %s", song.title)
            self.play_next(guild)
            return

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
                volume=player.volume,
            )
            vc.play(source, after=lambda e: self.play_next(guild, e))
        except Exception:
            logger.exception("Failed to play: %s", song.title)
            self.play_next(guild)

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
            title="🎵 Now Playing",
            description=f"[{song.title}]({song.webpage_url})",
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

        await interaction.response.defer(ephemeral=True)

        try:
            data = await self.extract_info(query)
        except Exception:
            await interaction.followup.send(
                "Could not find or process the requested song.", ephemeral=True
            )
            return

        song = await self.create_song(data, interaction.user)
        player = self.get_player(interaction.guild.id)

        if vc.is_playing() or vc.is_paused():
            player.queue.append(song)
            embed = discord.Embed(
                title="📋 Added to Queue",
                description=f"[{song.title}]({song.webpage_url})",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Position", value=str(len(player.queue)), inline=True)
            embed.add_field(name="Duration", value=song.duration_str, inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            player.current = song
            stream_url = await self.get_stream_url(song)
            if not stream_url:
                await interaction.followup.send(
                    "Could not get audio stream. Try again.", ephemeral=True
                )
                return
            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
                    volume=player.volume,
                )
                vc.play(source, after=lambda e: self.play_next(interaction.guild, e))
                embed = self.create_now_playing_embed(song)
                await interaction.followup.send(
                    embed=embed, view=self.player_view, ephemeral=True
                )
            except Exception as exc:
                logger.exception("Failed to start playback")
                await interaction.followup.send(
                    f"Playback error: {exc}", ephemeral=True
                )

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            player = self.get_player(interaction.guild.id)
            player.loop = False
            vc.stop()
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            player = self.get_player(interaction.guild.id)
            player.queue.clear()
            player.history.clear()
            player.current = None
            player.loop = False
            vc.stop()
            await vc.disconnect()
            self.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("⏹ Stopped and disconnected.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the current song queue")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)

        if not player.current and not player.queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Music Queue", color=discord.Color.purple())

        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"[{player.current.title}]({player.current.webpage_url}) — {player.current.duration_str}",
                inline=False,
            )

        if player.queue:
            queue_list = []
            for i, song in enumerate(player.queue, start=1):
                queue_list.append(f"`{i}.` [{song.title}]({song.webpage_url}) — {song.duration_str}")
                if i >= 10:
                    remaining = len(player.queue) - 10
                    if remaining > 0:
                        queue_list.append(f"*...and {remaining} more*")
                    break
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)

        embed.set_footer(text=f"Total songs in queue: {len(player.queue)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if player.current:
            await interaction.response.send_message(
                embed=self.create_now_playing_embed(player.current),
                view=self.player_view,
                ephemeral=True,
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

        await interaction.response.send_message(
            f"🔊 Volume set to **{level}%**.", ephemeral=True
        )

    @app_commands.command(name="loop", description="Toggle loop for the current song")
    async def loop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.loop = not player.loop
        state = "enabled 🔁" if player.loop else "disabled"
        await interaction.response.send_message(
            f"Loop **{state}**.", ephemeral=True
        )

    @app_commands.command(name="previous", description="Play the previous song")
    async def previous(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.history:
            await interaction.response.send_message("No previous song.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        if player.current:
            player.queue.appendleft(player.current)
        prev_song = player.history.pop()
        player.current = prev_song
        player.loop = False
        vc.stop()
        await interaction.response.send_message(
            f"⏮ **{prev_song.title}**", ephemeral=True
        )

    # ── Prefix Commands (fallback) ──────────────────────────────────

    @commands.command(name="play", aliases=["p"])
    async def play_prefix(self, ctx: commands.Context, *, query: str):
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You must be in a voice channel.", delete_after=DELETE_AFTER)
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
                await ctx.send(
                    "Could not find or process the requested song.",
                    delete_after=DELETE_AFTER,
                )
                return

            song = await self.create_song(data, ctx.author)
            player = self.get_player(ctx.guild.id)

            if vc.is_playing() or vc.is_paused():
                player.queue.append(song)
                embed = discord.Embed(
                    title="📋 Added to Queue",
                    description=f"[{song.title}]({song.webpage_url})",
                    color=discord.Color.blue(),
                )
                embed.add_field(name="Position", value=str(len(player.queue)), inline=True)
                embed.add_field(name="Duration", value=song.duration_str, inline=True)
                await ctx.send(embed=embed, delete_after=DELETE_AFTER)
            else:
                player.current = song
                stream_url = await self.get_stream_url(song)
                if not stream_url:
                    await ctx.send(
                        "Could not get audio stream. Try again.",
                        delete_after=DELETE_AFTER,
                    )
                    return
                try:
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
                        volume=player.volume,
                    )
                    vc.play(source, after=lambda e: self.play_next(ctx.guild, e))
                    embed = self.create_now_playing_embed(song)
                    await ctx.send(
                        embed=embed, view=self.player_view, delete_after=DELETE_AFTER
                    )
                except Exception as exc:
                    logger.exception("Failed to start playback")
                    await ctx.send(
                        f"Playback error: {exc}", delete_after=DELETE_AFTER
                    )

    @commands.command(name="pause")
    async def pause_prefix(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸ Paused.", delete_after=DELETE_AFTER)
        else:
            await ctx.send("Nothing is playing.", delete_after=DELETE_AFTER)

    @commands.command(name="resume", aliases=["r"])
    async def resume_prefix(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶ Resumed.", delete_after=DELETE_AFTER)
        else:
            await ctx.send("Nothing is paused.", delete_after=DELETE_AFTER)

    @commands.command(name="skip", aliases=["s"])
    async def skip_prefix(self, ctx: commands.Context):
        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            player = self.get_player(ctx.guild.id)
            player.loop = False
            ctx.voice_client.stop()
            await ctx.send("⏭ Skipped.", delete_after=DELETE_AFTER)
        else:
            await ctx.send("Nothing to skip.", delete_after=DELETE_AFTER)

    @commands.command(name="stop", aliases=["dc", "disconnect", "leave"])
    async def stop_prefix(self, ctx: commands.Context):
        if ctx.voice_client:
            player = self.get_player(ctx.guild.id)
            player.queue.clear()
            player.history.clear()
            player.current = None
            player.loop = False
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            self.players.pop(ctx.guild.id, None)
            await ctx.send("⏹ Stopped and disconnected.", delete_after=DELETE_AFTER)
        else:
            await ctx.send("Not connected.", delete_after=DELETE_AFTER)

    @commands.command(name="queue", aliases=["q"])
    async def queue_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player.current and not player.queue:
            await ctx.send("The queue is empty.", delete_after=DELETE_AFTER)
            return

        embed = discord.Embed(title="📋 Music Queue", color=discord.Color.purple())
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"[{player.current.title}]({player.current.webpage_url}) — {player.current.duration_str}",
                inline=False,
            )
        if player.queue:
            queue_list = []
            for i, song in enumerate(player.queue, start=1):
                queue_list.append(f"`{i}.` [{song.title}]({song.webpage_url}) — {song.duration_str}")
                if i >= 10:
                    remaining = len(player.queue) - 10
                    if remaining > 0:
                        queue_list.append(f"*...and {remaining} more*")
                    break
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)

        embed.set_footer(text=f"Total songs in queue: {len(player.queue)}")
        await ctx.send(embed=embed, delete_after=DELETE_AFTER)

    @commands.command(name="np", aliases=["nowplaying"])
    async def nowplaying_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.current:
            await ctx.send(
                embed=self.create_now_playing_embed(player.current),
                view=self.player_view,
                delete_after=DELETE_AFTER,
            )
        else:
            await ctx.send("Nothing is playing.", delete_after=DELETE_AFTER)

    @commands.command(name="volume", aliases=["vol"])
    async def volume_prefix(self, ctx: commands.Context, level: int):
        if not 0 <= level <= 100:
            await ctx.send(
                "Volume must be between 0 and 100.", delete_after=DELETE_AFTER
            )
            return
        player = self.get_player(ctx.guild.id)
        player.volume = level / 100.0
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = player.volume
        await ctx.send(f"🔊 Volume set to **{level}%**.", delete_after=DELETE_AFTER)

    @commands.command(name="loop")
    async def loop_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        player.loop = not player.loop
        state = "enabled 🔁" if player.loop else "disabled"
        await ctx.send(f"Loop **{state}**.", delete_after=DELETE_AFTER)

    @commands.command(name="prev", aliases=["previous"])
    async def previous_prefix(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player.history:
            await ctx.send("No previous song.", delete_after=DELETE_AFTER)
            return

        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            await ctx.send("Not connected.", delete_after=DELETE_AFTER)
            return

        if player.current:
            player.queue.appendleft(player.current)
        prev_song = player.history.pop()
        player.current = prev_song
        player.loop = False
        vc.stop()
        await ctx.send(f"⏮ **{prev_song.title}**", delete_after=DELETE_AFTER)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
