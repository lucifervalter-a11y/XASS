"""Detect Discord activity and running game via process inspection."""
from __future__ import annotations

import time
from typing import Any

# Mapping: lowercase exe name (without .exe, spaces, dashes, underscores) -> display name
# None means "skip" (not a game we want to report)
KNOWN_GAME_PROCESSES: dict[str, str | None] = {
    "dota2": "Dota 2",
    "csgo": "CS:GO",
    "cs2": "CS2",
    "eldenring": "Elden Ring",
    "witcher3": "The Witcher 3",
    "gta5": "GTA V",
    "gtav": "GTA V",
    "gta5launcher": "GTA V",
    "minecraft": "Minecraft",
    "minecraftlauncher": None,
    "javaw": None,  # might be minecraft but too generic
    "valorant": "Valorant",
    "valorantlauncher": None,
    "fornite": "Fortnite",
    "fortniteclient": "Fortnite",
    "leagueoflegends": "League of Legends",
    "rocketleague": "Rocket League",
    "apexlegends": "Apex Legends",
    "r5apex": "Apex Legends",
    "overwatch": "Overwatch",
    "overwatch2": "Overwatch 2",
    "destiny2": "Destiny 2",
    "pathofexile": "Path of Exile",
    "pathofexile2": "Path of Exile 2",
    "warframe": "Warframe",
    "cyberpunk2077": "Cyberpunk 2077",
    "cyberpunk": "Cyberpunk 2077",
    "baldursgate3": "Baldur's Gate 3",
    "bg3": "Baldur's Gate 3",
    "hollowright": None,
    "hollow": "Hollow Knight",
    "hollowknight": "Hollow Knight",
    "terraria": "Terraria",
    "rustclient": "Rust",
    "rust": "Rust",
    "pubg": "PUBG",
    "tslgame": "PUBG",
    "battlefieldbadcompany2": "Battlefield",
    "bf1": "Battlefield 1",
    "bf2042": "Battlefield 2042",
    "arma3": "Arma 3",
    "escapefromtarkov": "Escape from Tarkov",
    "battlestate": "Escape from Tarkov",
    "hearthstone": "Hearthstone",
    "diablo4": "Diablo IV",
    "diablo4launcher": None,
    "worldofwarcraft": "World of Warcraft",
    "wow": "World of Warcraft",
    "totalwar": "Total War",
    "civilization6": "Civilization VI",
    "civ6": "Civilization VI",
    "civilization7": "Civilization VII",
    "civ7": "Civilization VII",
    "starcraftii": "StarCraft II",
    "sc2": "StarCraft II",
}

DISCORD_PROCESS_NAMES = frozenset({
    "discord.exe",
    "discordptb.exe",
    "discordcanary.exe",
    "discorddevelopment.exe",
})


def _normalize_proc_name(raw: str) -> str:
    """Strip .exe, lowercase, remove spaces/dashes/underscores."""
    name = raw.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name.replace(" ", "").replace("-", "").replace("_", "")


def get_discord_activity() -> dict[str, Any] | None:
    """
    Returns None if Discord is not running.

    Returns a dict with:
      - is_online: bool  (True if Discord process found)
      - elapsed_sec: int  (Discord process uptime in seconds)
      - game: str | None  (detected game name from process list)
      - in_voice: bool  (always False — can't detect without RPC)

    Gracefully handles ImportError (psutil not installed) and all
    psutil exceptions.
    """
    try:
        import psutil  # type: ignore[import]
    except ImportError:
        return None

    discord_info: dict[str, Any] | None = None
    game_name: str | None = None

    try:
        for proc in psutil.process_iter(["name", "status", "create_time"]):
            try:
                raw_name: str = proc.info.get("name") or ""
                lower_name = raw_name.lower()

                # Check if it's a Discord process
                if lower_name in DISCORD_PROCESS_NAMES:
                    if discord_info is None:
                        discord_info = dict(proc.info)

                # Check if it's a known game
                if game_name is None:
                    normalized = _normalize_proc_name(raw_name)
                    if normalized in KNOWN_GAME_PROCESSES:
                        mapped = KNOWN_GAME_PROCESSES[normalized]
                        if mapped is not None:
                            game_name = mapped

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    except Exception:
        return None

    if discord_info is None:
        return None

    create_time = discord_info.get("create_time")
    if isinstance(create_time, (int, float)):
        elapsed = max(0, int(time.time() - create_time))
    else:
        elapsed = 0

    return {
        "is_online": True,
        "elapsed_sec": elapsed,
        "game": game_name,
        "in_voice": False,
    }
