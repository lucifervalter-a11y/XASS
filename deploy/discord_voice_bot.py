"""XASS Discord Voice Bot: plays music from XASS library in voice channels.

Usage:
    python -m deploy.discord_voice_bot
    or run via systemd / background process.
Requires:
    DISCORD_BOT_TOKEN in .env or environment.
    ffmpeg installed on the host.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sqlite3
import sys

try:
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError:
    discord = None
    app_commands = None
    commands = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("xass.discord_voice")

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _load_env_file(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip("'\"")
    except OSError:
        pass
    return values


ENV = {**_load_env_file(ROOT_DIR / ".env"), **os.environ}
BOT_TOKEN = ENV.get("DISCORD_BOT_TOKEN") or ENV.get("DISCORD_TOKEN") or ""
MUSIC_ROOT = Path(ENV.get("MUSIC_ROOT") or (ROOT_DIR / "data" / "music-library")).resolve()
DB_URL = ENV.get("DATABASE_URL") or "sqlite+aiosqlite:///./data/serverredus.db"


def _resolve_sqlite_path(url: str) -> Path:
    raw = url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


DB_PATH = _resolve_sqlite_path(DB_URL)


def find_track(query: str) -> dict | None:
    """Find the best matching active track in XASS database."""
    if not DB_PATH.is_file():
        logger.warning("Database not found at %s", DB_PATH)
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        pattern = f"%{query.strip()}%"
        cursor.execute(
            """
            SELECT id, title, artist, album, storage_name, duration, size, mime
            FROM music_tracks
            WHERE deleted = 0 AND (title LIKE ? OR artist LIKE ? OR album LIKE ?)
            ORDER BY favorite DESC, id DESC
            LIMIT 1
            """,
            (pattern, pattern, pattern),
        )
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        audio_file = MUSIC_ROOT / data["storage_name"]
        if not audio_file.is_file():
            logger.warning("Track file not found: %s", audio_file)
            return None
        data["file_path"] = str(audio_file)
        return data
    except Exception as exc:
        logger.error("Database query failed: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def format_duration(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def create_bot() -> commands.Bot | None:
    if discord is None:
        logger.error("discord.py is not installed. Install with: pip install 'discord.py[voice]'")
        return None

    intents = discord.Intents.default()
    intents.voice_states = True
    bot = commands.Bot(command_prefix="!xass ", intents=intents)

    @bot.event
    async def on_ready():
        logger.info("XASS Discord Voice Bot logged in as %s (ID: %s)", bot.user, bot.user.id)
        try:
            synced = await bot.tree.sync()
            logger.info("Synced %d application command(s)", len(synced))
        except Exception as exc:
            logger.error("Command sync failed: %s", exc)

    @bot.tree.command(name="music", description="Включить музыку из медиатеки XASS в голосовом канале")
    @app_commands.describe(query="Название трека или исполнитель")
    async def music_cmd(interaction: discord.Interaction, query: str):
        # 1. Проверяем, в каком голосовом канале находится пользователь
        voice_state = interaction.user.voice if hasattr(interaction.user, "voice") else None
        if not voice_state or not voice_state.channel:
            await interaction.response.send_message(
                "❌ **Зайдите в голосовой канал**, чтобы бот мог включить для вас музыку!",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # 2. Ищем трек в медиатеке XASS
        track = find_track(query)
        if not track:
            await interaction.followup.send(
                f"🔍 Трек по запросу **«{query}»** не найден в медиатеке XASS.\n"
                "Проверьте правильность написания или загрузите файл через Mini App / сайт.",
                ephemeral=True,
            )
            return

        target_channel = voice_state.channel
        guild = interaction.guild
        voice_client: discord.VoiceClient | None = guild.voice_client

        # 3. Подключаемся к голосовому каналу пользователя
        try:
            if voice_client is None:
                voice_client = await target_channel.connect(timeout=15.0, reconnect=True)
            elif voice_client.channel != target_channel:
                await voice_client.move_to(target_channel)
        except Exception as exc:
            logger.error("Voice connection failed: %s", exc)
            await interaction.followup.send(f"❌ Не удалось подключиться к каналу **{target_channel.name}**: {exc}")
            return

        # 4. Запускаем воспроизведение через FFmpeg
        try:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            source = discord.FFmpegPCMAudio(
                track["file_path"],
                options="-vn -b:a 192k",
            )
            voice_client.play(source)

            embed = discord.Embed(
                title=f"🎶 {track.get('title') or 'Без названия'}",
                description=f"**Исполнитель:** {track.get('artist') or 'Не указан'}\n**Канал:** {target_channel.name}",
                color=0x376DFF,
            )
            album = track.get("album")
            if album:
                embed.add_field(name="Альбом", value=album, inline=True)
            embed.add_field(name="Длительность", value=format_duration(track.get("duration")), inline=True)
            embed.set_footer(text="XASS Voice Player")

            await interaction.followup.send(embed=embed)
        except Exception as exc:
            logger.error("Playback failed: %s", exc)
            await interaction.followup.send(f"❌ Ошибка запуска воспроизведения: {exc}")

    @bot.tree.command(name="stop", description="Остановить воспроизведение и выйти из голосового канала")
    async def stop_cmd(interaction: discord.Interaction):
        guild = interaction.guild
        voice_client: discord.VoiceClient | None = guild.voice_client if guild else None
        if voice_client and (voice_client.is_connected() or voice_client.is_playing()):
            await voice_client.disconnect(force=True)
            await interaction.response.send_message("⏹ Музыка остановлена, бот вышел из канала.")
        else:
            await interaction.response.send_message("Бот не находится в голосовом канале.", ephemeral=True)

    @bot.tree.command(name="pause", description="Поставить музыку на паузу")
    async def pause_cmd(interaction: discord.Interaction):
        voice_client: discord.VoiceClient | None = interaction.guild.voice_client if interaction.guild else None
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸ Пауза.")
        else:
            await interaction.response.send_message("Музыка не воспроизводится.", ephemeral=True)

    @bot.tree.command(name="resume", description="Продолжить воспроизведение")
    async def resume_cmd(interaction: discord.Interaction):
        voice_client: discord.VoiceClient | None = interaction.guild.voice_client if interaction.guild else None
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶ Возобновлено.")
        else:
            await interaction.response.send_message("Музыка не стоит на паузе.", ephemeral=True)

    return bot


def main():
    if not BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN is not configured in .env or environment.")
        sys.exit(1)

    bot = create_bot()
    if bot is None:
        sys.exit(1)

    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
