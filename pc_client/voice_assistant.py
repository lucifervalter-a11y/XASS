"""Local assistant policy. No account access, shell commands, or remote listener.

Planner output is untrusted: only five parameter-free actions can cross the
policy boundary. Paths and the Discord destination come from local settings,
never from the planner. The existing remote-command pipeline remains separate
until a remote approval protocol is designed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Protocol
import urllib.request
import webbrowser


CHANNEL_NAME = "Шаурма классическая"
MAX_TEXT = 512


class AssistantError(ValueError):
    """Locally authored, safe-to-display error."""


class Action(str, Enum):
    MINECRAFT = "launch_minecraft"
    YOUTUBE = "open_youtube"
    DISCORD = "open_discord"
    CHANNEL = "show_discord_channel"
    JOIN = "join_discord_voice"


LABELS = {
    Action.MINECRAFT: "Открыть TLauncher с текущим forgeai2 / Forge 1.20.1",
    Action.YOUTUBE: "Открыть YouTube",
    Action.DISCORD: "Открыть Discord",
    Action.CHANNEL: f"Открыть страницу канала «{CHANNEL_NAME}»",
    Action.JOIN: f"Подключиться к голосу «{CHANNEL_NAME}»",
}

PHRASES = {
    Action.MINECRAFT: ("запусти minecraft", "запусти майнкрафт", "открой майнкрафт", "открой tlauncher"),
    Action.YOUTUBE: ("открой youtube", "открой ютуб", "запусти ютуб"),
    Action.DISCORD: ("открой discord", "открой дискорд", "запусти дискорд"),
    Action.CHANNEL: ("перейди к каналу шаурма классическая", "открой канал шаурма классическая",
                     "перейди в канал шаурма классическая", "перейди к голосовому каналу шаурма классическая"),
    Action.JOIN: ("подключись к голосу шаурма классическая", "подключись к каналу шаурма классическая",
                  "подключись к голосовому каналу шаурма классическая",
                  "вступи в голосовой канал шаурма классическая"),
}


def normalize(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise AssistantError("Введите одну команду длиной до 512 символов.")
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise AssistantError("В команде есть недопустимые символы.")
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()
    value = value.rstrip(".!?")
    value = re.sub(r"^(?:джарвис|jarvis)[, ]+", "", value)
    return value


@dataclass(frozen=True)
class Plan:
    action: Action

    def to_dict(self) -> dict:
        return {"action": self.action.value, "parameters": {}}


def validate_plan(value: object, transcript: str) -> Plan:
    normalize(transcript)
    if not isinstance(value, dict) or set(value) != {"action", "parameters"} or value["parameters"] != {}:
        raise AssistantError("План отклонён: разрешено одно действие без произвольных параметров.")
    try:
        action = Action(value["action"])
    except (ValueError, TypeError):
        raise AssistantError("План отклонён: действие не разрешено.") from None
    # A model cannot upgrade 'show channel' to joining a call, or infer consent.
    if action == Action.JOIN and normalize(transcript) not in PHRASES[Action.JOIN]:
        raise AssistantError("Для входа в голос произнесите явную команду подключения с именем канала.")
    return Plan(action)


class Planner(Protocol):
    def plan(self, transcript: str) -> Plan: ...


class RulePlanner:
    def plan(self, transcript: str) -> Plan:
        text = normalize(transcript)
        for action, phrases in PHRASES.items():
            if text in phrases:
                return validate_plan(Plan(action).to_dict(), transcript)
        raise AssistantError("Команда не распознана. Используйте один из примеров; составные команды не выполняются.")


class OllamaPlanner:
    """Optional, explicitly selected local provider; never starts/pulls a model."""
    def __init__(self, model: str):
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,119}", model):
            raise AssistantError("Укажите имя уже установленной локальной модели Ollama.")
        self.model = model

    def plan(self, transcript: str) -> Plan:
        normalize(transcript)
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": [a.value for a in Action]},
            "parameters": {"type": "object", "additionalProperties": False}},
            "required": ["action", "parameters"], "additionalProperties": False}
        body = {"model": self.model, "stream": False, "format": schema,
                "options": {"temperature": 0, "num_predict": 96}, "messages": [
                    {"role": "system", "content": "Return one allowed action and empty parameters. "
                     "Use launch_minecraft for Minecraft/TLauncher, open_youtube for YouTube, "
                     "open_discord for Discord, show_discord_channel for viewing Шаурма классическая, "
                     "join_discord_voice only for an explicit request to join that voice channel. "
                     "For unsupported, negated or compound requests return {}. Never invent commands."},
                    {"role": "user", "content": transcript}]}
        request = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=45) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            content = json.loads(raw)["message"]["content"]
            proposed = json.loads(content)
        except Exception:
            raise AssistantError("Локальная модель Ollama недоступна или вернула неверный ответ. Выберите режим команд.") from None
        return validate_plan(proposed, transcript)


@dataclass(frozen=True)
class Settings:
    launcher_path: str = ""
    launcher_properties_path: str = ""
    minecraft_dir: str = ""
    channel_url: str = ""
    stt_model_path: str = ""
    planner: str = "rules"
    ollama_model: str = ""

    @classmethod
    def from_dict(cls, value: object) -> "Settings":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise AssistantError("Неверные настройки помощника.")
        if any(not isinstance(v, str) or len(v) > 2048 for v in value.values()):
            raise AssistantError("Неверный формат настроек помощника.")
        result = cls(**value)
        if result.planner not in {"rules", "ollama"}:
            raise AssistantError("Неизвестный планировщик.")
        return result

    def make_planner(self) -> Planner:
        return OllamaPlanner(self.ollama_model) if self.planner == "ollama" else RulePlanner()


def channel_ids(url: str) -> tuple[str, str]:
    match = re.fullmatch(r"https://discord\.com/channels/([1-9][0-9]{16,19})/([1-9][0-9]{16,19})/?", url)
    if not match:
        raise AssistantError("Добавьте точную ссылку https://discord.com/channels/сервер/канал для «Шаурма классическая».")
    return match.group(1), match.group(2)


class DiscordVoiceConnector(Protocol):
    def join(self, guild_id: str, channel_id: str) -> bool: ...


class AuthorizedDiscordRpc:
    """Adapter for an ALREADY OAuth-authorized RPC transport, supplied by host.

    No credential extraction or OAuth provisioning. The default UI deliberately
    does not construct this until an approved Discord integration is available.
    """
    def __init__(self, request: Callable[[str, dict], dict]):
        self.request = request

    def join(self, guild_id: str, channel_id: str) -> bool:
        channel = self.request("GET_CHANNEL", {"channel_id": channel_id})
        if (channel.get("id") != channel_id or channel.get("guild_id") != guild_id
                or channel.get("name") != CHANNEL_NAME or channel.get("type") != 2):
            raise AssistantError("Discord не подтвердил нужный голосовой канал.")
        selected = self.request("GET_SELECTED_VOICE_CHANNEL", {})
        if selected and selected.get("id") != channel_id:
            raise AssistantError("Вы уже в другом голосовом канале. Автоматическое переключение отключено.")
        if not selected:
            self.request("SELECT_VOICE_CHANNEL", {"channel_id": channel_id, "force": False})
        actual = self.request("GET_SELECTED_VOICE_CHANNEL", {})
        return bool(actual and actual.get("id") == channel_id)


def launcher_target(settings: Settings) -> Path:
    roaming = Path(os.environ.get("APPDATA", ""))
    default_exe = roaming / ".minecraft" / "TLauncher.exe"
    if not default_exe.is_file():
        default_exe = roaming / ".tlauncher" / "TLauncher.exe"
    exe = Path(settings.launcher_path) if settings.launcher_path else default_exe
    game = Path(settings.minecraft_dir) if settings.minecraft_dir else roaming / ".minecraft"
    properties_file = Path(settings.launcher_properties_path) if settings.launcher_properties_path else roaming / ".tlauncher" / "tlauncher-2.0.properties"
    if not exe.is_absolute() or exe.name.casefold() != "tlauncher.exe" or not exe.is_file():
        raise AssistantError("Укажите полный путь к установленному TLauncher.exe.")
    if not game.is_absolute():
        raise AssistantError("Укажите полный путь к существующей папке Minecraft.")
    # Read only the non-secret launcher properties and selected version metadata.
    try:
        if not properties_file.is_absolute() or properties_file.name != "tlauncher-2.0.properties" or properties_file.stat().st_size > 65536:
            raise ValueError()
        properties = properties_file.read_text(encoding="utf-8")
        selected = re.findall(r"^login\.version\.game=(.*)$", properties, re.MULTILINE)
        if len(selected) != 1 or selected[0].strip() != "forgeai2":
            raise AssistantError("В TLauncher выбран другой профиль. Выберите forgeai2 вручную; XASS не меняет профили.")
        game_dirs = re.findall(r"^minecraft\.gamedir=(.*)$", properties, re.MULTILINE)
        if len(game_dirs) != 1:
            raise ValueError()
        saved_game = re.sub(r"\\u([0-9a-fA-F]{4})|\\(.)", lambda m: chr(int(m[1], 16)) if m[1] else m[2], game_dirs[0].strip())
        if Path(saved_game).resolve() != game.resolve():
            raise AssistantError("Папка Minecraft отличается от текущей папки TLauncher. Проверьте настройку; файлы не изменены.")
        version_file = game / "versions" / "forgeai2" / "forgeai2.json"
        if version_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        version = json.loads(version_file.read_text(encoding="utf-8"))
        args = []
        for item in version.get("arguments", {}).get("game", []):
            if isinstance(item, str):
                args.append(item)
            elif isinstance(item, dict) and not item.get("rules"):
                args.extend(item.get("values", []))  # TLauncher serializes literal args this way.
        mc_index = args.index("--fml.mcVersion")
        is_forge = any(isinstance(v, dict) and str(v.get("name", "")).startswith(("net.minecraftforge:forge:1.20.1-", "net.minecraftforge:fmlloader:1.20.1-"))
                       for v in version.get("libraries", []))
        if version.get("id") != "forgeai2" or args[mc_index + 1] != "1.20.1" or not is_forge:
            raise AssistantError("Не удалось подтвердить Forge 1.20.1 для forgeai2. Настройки Minecraft не изменены.")
    except AssistantError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        raise AssistantError("Не удалось проверить текущий профиль TLauncher и Forge 1.20.1.") from None
    return exe.resolve()


@dataclass(frozen=True)
class Result:
    state: str
    message: str


class LocalExecutor:
    def __init__(self, *, open_url=None, launch=None, discord: DiscordVoiceConnector | None = None):
        self.open_url = open_url or webbrowser.open
        self.launch = launch or subprocess.Popen
        self.discord = discord

    def execute(self, plan: Plan, transcript: str, settings: Settings, *, confirmed: bool = False) -> Result:
        plan = validate_plan(plan.to_dict(), transcript)
        if plan.action == Action.JOIN:
            if confirmed is not True:
                return Result("needs_confirmation", "Подтвердите подключение отдельной кнопкой после просмотра команды.")
            guild, channel = channel_ids(settings.channel_url)
            if self.discord is None:
                return Result("blocked", "Вход в голос недоступен: для XASS ещё не настроен разрешённый Discord OAuth/RPC. Подключитесь вручную в Discord.")
            if self.discord.join(guild, channel):
                return Result("succeeded", f"Discord подтвердил подключение к «{CHANNEL_NAME}».")
            return Result("unverified", "Запрос отправлен, но Discord не подтвердил подключение.")
        if plan.action == Action.MINECRAFT:
            target = launcher_target(settings)
            self.launch([str(target)], cwd=str(target.parent), shell=False,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        close_fds=True)
            return Result("dispatched", "TLauncher запрошен с сохранённым forgeai2 / Forge 1.20.1. Запуск самой игры не подтверждён.")
        url = {Action.YOUTUBE: "https://www.youtube.com/", Action.DISCORD: "discord://-/channels/@me"}.get(plan.action)
        if plan.action == Action.CHANNEL:
            guild, channel = channel_ids(settings.channel_url)
            url = f"https://discord.com/channels/{guild}/{channel}"
        assert url is not None
        if not self.open_url(url):
            return Result("failed", "Windows не приняла запрос открытия. Проверьте браузер или установку Discord.")
        message = "Страница канала запрошена. Подключение к голосу не запрашивалось и не проверялось." if plan.action == Action.CHANNEL else "Windows приняла запрос открытия. Готовность приложения не проверялась."
        return Result("dispatched", message)
