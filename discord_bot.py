#!/usr/bin/env python3
"""Discord request bot for yt-vlc."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import discord
from discord.ext import commands

import yt_vlc


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
DEFAULT_LOG_FILE = APP_DIR / "logs" / "discord_bot.log"
COMMAND_PREFIX = "!"
MAX_DISCORD_TEXT = 1_000
MAX_QUEUE_EMBED_DESCRIPTION = 4_000
MAX_TEMP_COOKIE_BYTES = 2 * 1024 * 1024
COOKIE_UPLOAD_TIMEOUT = 60.0
VLC_START_TIMEOUT = 30.0
VLC_START_ATTEMPTS = 2
VLC_START_POLL_INTERVAL = 0.2
VLC_SHUTDOWN_TIMEOUT = 5.0
VLC_PLAYBACK_TIMEOUT = 30.0
VLC_POLL_INTERVAL = 0.5
VLC_STALL_TIMEOUT = 12.0
DEFAULT_VLC_AUDIO_OUTPUT = "directsound"
VLC_AUDIO_OUTPUTS = {"automatic", "directsound", "mmdevice", "waveout"}
QUEUE_PREFETCH_SECONDS = 8.0
STABILITY_FALLBACK_FORMAT = (
    "b[height>=720][height<=720]/"
    "bv*[height<=720]+ba/b[height<=720]/b"
)
FORMAT_AVAILABILITY_FALLBACK = (
    "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
)
ACTIVE_VLC_STATES = {"opening", "buffering", "playing", "paused"}
SENSITIVE_LINK_DOMAINS = (
    "alldebrid.com",
    "alldebrid.fr",
    "debrid-link.com",
    "easy-debrid.com",
    "easynews.com",
    "linksnappy.com",
    "offcloud.com",
    "premiumize.me",
    "real-debrid.com",
    "tb-cdn.io",
    "torbox.app",
)
SENSITIVE_HOST_MARKERS = (
    "debrid",
    "easynews",
    "linksnappy",
    "offcloud",
    "premiumize",
    "tb-cdn",
    "torbox",
)
COOKIE_TARGET_DOMAINS: dict[str, tuple[str, ...]] = {
    "youtube": (
        "youtube.com",
        "youtu.be",
        "google.com",
        "googlevideo.com",
        "ytimg.com",
    ),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com", "fbcdn.net"),
    "tiktok": ("tiktok.com", "tiktokv.com", "tiktokcdn.com"),
    "twitter": ("x.com", "twitter.com", "twimg.com"),
}
COOKIE_AUTH_PATTERNS = (
    "accounts/login",
    "authentication",
    "authorization",
    "confirm you are not a bot",
    "cookie",
    "forbidden",
    "http error 401",
    "http error 403",
    "login page",
    "login required",
    "log in",
    "members only",
    "members-only",
    "not a bot",
    "private video",
    "redirect to login",
    "sign in to confirm",
    "sign in to verify",
)
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
REDACTED_LINK = "[sensitive debrid link redacted]"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOGGER = logging.getLogger("yt_vlc.discord_bot")
LOGGER.addHandler(logging.NullHandler())
TOOLS_LOCK = threading.Lock()


@dataclass(slots=True)
class MediaRequest:
    url: str
    requester: discord.User | discord.Member
    status_message: discord.Message
    bot: commands.Bot | None = field(default=None, repr=False)
    cookie_data: bytes | None = field(default=None, repr=False)


@dataclass(slots=True)
class PreparedMedia:
    vlc: str
    streams: list[str]
    info: dict[str, object]


@dataclass(slots=True)
class PrefetchedRequest:
    request: MediaRequest
    task: asyncio.Task[PreparedMedia]


class PlaybackStalled(RuntimeError):
    def __init__(self, position_seconds: float) -> None:
        self.position_seconds = position_seconds
        super().__init__(
            f"Playback stopped progressing near {yt_vlc.human_duration(position_seconds)}"
        )


class CookieFormatError(ValueError):
    pass


def status_number(status: dict[str, object], key: str) -> float | None:
    try:
        value = float(status[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def configured_vlc_audio_output() -> str | None:
    """Return the validated Windows audio module used for Discord capture."""
    value = os.environ.get(
        "VLC_AUDIO_OUTPUT",
        DEFAULT_VLC_AUDIO_OUTPUT,
    ).strip().lower()
    if value not in VLC_AUDIO_OUTPUTS:
        choices = ", ".join(sorted(VLC_AUDIO_OUTPUTS))
        raise RuntimeError(f"VLC_AUDIO_OUTPUT must be one of: {choices}")
    return None if value == "automatic" else value


class VLCSession:
    """Keep one VLC window alive and control its playlist over localhost."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.port = self._available_port()
        self.password = secrets.token_urlsafe(24)
        self.process: subprocess.Popen[bytes] | None = None
        self._start_lock = threading.Lock()

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _authorization(self) -> str:
        credentials = base64.b64encode(f":{self.password}".encode("utf-8"))
        return f"Basic {credentials.decode('ascii')}"

    def _request(self, params: dict[str, str] | None = None) -> dict[str, object]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"http://127.0.0.1:{self.port}/requests/status.json{query}",
            headers={"Authorization": self._authorization()},
        )
        with urlopen(request, timeout=2) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise RuntimeError("VLC returned an invalid status response")
        return result

    def status(self) -> dict[str, object]:
        return self._request()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _wait_until_ready(self) -> None:
        assert self.process is not None
        started_at = time.monotonic()
        deadline = time.monotonic() + VLC_START_TIMEOUT
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"VLC exited immediately with code {self.process.returncode}"
                )
            try:
                self.status()
                LOGGER.info(
                    "VLC controller ready on 127.0.0.1:%s after %.1fs",
                    self.port,
                    time.monotonic() - started_at,
                )
                return
            except (OSError, RuntimeError, ValueError) as error:
                last_error = error
                time.sleep(VLC_START_POLL_INTERVAL)

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(
            "VLC's local control interface did not become ready "
            f"within {VLC_START_TIMEOUT:g} seconds{detail}"
        ) from last_error

    def _stop_failed_process(self) -> None:
        """Stop an unusable bot-owned VLC process before a clean retry."""
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return

        LOGGER.warning("Stopping unusable VLC process pid=%s", process.pid)
        process.terminate()
        try:
            process.wait(timeout=VLC_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=VLC_SHUTDOWN_TIMEOUT)

    def _launch(self) -> None:
        self.port = self._available_port()
        self.password = secrets.token_urlsafe(24)
        audio_output = configured_vlc_audio_output()
        command = [
            self.executable,
            "--no-one-instance",
            "--extraintf=http",
            "--http-host=127.0.0.1",
            f"--http-port={self.port}",
            f"--http-password={self.password}",
            "--no-qt-privacy-ask",
            "--no-video-title-show",
        ]
        if audio_output is not None:
            command.append(f"--aout={audio_output}")
        self.process = subprocess.Popen(
            command,
            cwd=Path(self.executable).parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        LOGGER.info(
            "Started VLC process pid=%s with controller port=%s audio_output=%s",
            self.process.pid,
            self.port,
            audio_output or "automatic",
        )

    def _ensure_started(self) -> None:
        last_error: RuntimeError | None = None
        if self.process is not None and self.process.poll() is None:
            try:
                self._wait_until_ready()
                return
            except RuntimeError as error:
                last_error = error
                LOGGER.warning(
                    "Existing VLC controller became unavailable: %s; restarting",
                    error,
                )
                self._stop_failed_process()

        for attempt in range(1, VLC_START_ATTEMPTS + 1):
            self._launch()
            try:
                self._wait_until_ready()
                return
            except RuntimeError as error:
                last_error = error
                LOGGER.warning(
                    "VLC initialization attempt %s/%s failed: %s",
                    attempt,
                    VLC_START_ATTEMPTS,
                    error,
                )
                self._stop_failed_process()

        detail = f" Last error: {last_error}" if last_error else ""
        raise RuntimeError(
            "VLC opened, but its local control interface could not be "
            f"initialized after {VLC_START_ATTEMPTS} attempts.{detail}"
        ) from last_error

    def ensure_started(self) -> None:
        with self._start_lock:
            self._ensure_started()

    def play(self, media: PreparedMedia) -> dict[str, object]:
        self.ensure_started()
        LOGGER.info(
            "Handing media to VLC: title=%r streams=%s",
            text_value(media.info, "title", "Requested media"),
            len(media.streams),
        )
        self._request({"command": "pl_empty"})
        command = {
            "command": "in_play",
            "input": media.streams[0],
        }
        if len(media.streams) == 2:
            command["option"] = f"input-slave={media.streams[1]}"
        return self._request(command)

    def pause(self) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        LOGGER.info("Pausing VLC playback")
        return self._request({"command": "pl_forcepause"})

    def resume(self) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        LOGGER.info("Resuming VLC playback")
        return self._request({"command": "pl_forceresume"})

    def stop(self) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        LOGGER.info("Stopping current VLC item; player process remains open")
        return self._request({"command": "pl_stop"})

    def seek_when_ready(self, seconds: float) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        deadline = time.monotonic() + VLC_PLAYBACK_TIMEOUT
        while time.monotonic() < deadline:
            status = self.status()
            state_name = str(status.get("state", "")).lower()
            if state_name in {"playing", "paused"}:
                return self._request(
                    {"command": "seek", "val": f"{max(0, round(seconds))}S"}
                )
            if not self.is_running():
                raise RuntimeError("VLC closed before playback could resume")
            time.sleep(0.1)
        raise RuntimeError("VLC did not become ready to resume playback")

    async def wait_until_finished(
        self,
        initial_status: dict[str, object],
        on_near_end: Callable[[], bool] | None = None,
    ) -> None:
        """Wait for the current item to stop without waiting for VLC to exit."""
        state_name = str(initial_status.get("state", "")).lower()
        seen_active = state_name in ACTIVE_VLC_STATES
        stopped_since: float | None = None
        now = time.monotonic()
        start_deadline = now + VLC_PLAYBACK_TIMEOUT
        last_position = status_number(initial_status, "time")
        last_progress_at = now
        prefetch_started = False

        while True:
            if self.process is None or self.process.poll() is not None:
                exit_code = None if self.process is None else self.process.returncode
                raise RuntimeError(f"VLC closed during playback (code {exit_code})")

            status = await asyncio.to_thread(self.status)
            state_name = str(status.get("state", "")).lower()
            now = time.monotonic()
            position = status_number(status, "time")
            length = status_number(status, "length")

            if state_name in ACTIVE_VLC_STATES:
                seen_active = True
                stopped_since = None
            elif state_name == "stopped":
                if seen_active:
                    return
                if stopped_since is None:
                    stopped_since = now
                elif now - stopped_since >= 2.0:
                    return

            if state_name == "playing" and position is not None:
                if last_position is None or abs(position - last_position) >= 0.5:
                    last_position = position
                    last_progress_at = now
                elif (
                    length is not None
                    and length > 0
                    and now - last_progress_at >= VLC_STALL_TIMEOUT
                ):
                    raise PlaybackStalled(position)
            elif state_name in {"opening", "buffering", "paused"}:
                last_progress_at = now

            if (
                not prefetch_started
                and on_near_end is not None
                and position is not None
                and length is not None
                and length > 0
                and 0 < length - position <= QUEUE_PREFETCH_SECONDS
            ):
                prefetch_started = on_near_end()

            if not seen_active and now >= start_deadline:
                raise RuntimeError("VLC did not begin playback")
            await asyncio.sleep(VLC_POLL_INTERVAL)


@dataclass(slots=True)
class GuildState:
    guild_id: int
    queue: asyncio.Queue[MediaRequest] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    current: MediaRequest | None = None
    vlc: VLCSession | None = None
    completion_note: str | None = None
    cancel_current: bool = False
    prefetch: PrefetchedRequest | None = None


class CookieRetryView(discord.ui.View):
    """Collect requester-scoped cookies in DM and enqueue one private retry."""

    def __init__(
        self,
        bot: commands.Bot,
        state: GuildState,
        request: MediaRequest,
    ) -> None:
        super().__init__(timeout=COOKIE_UPLOAD_TIMEOUT)
        self.bot = bot
        self.state = state
        self.request = request

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user is not None
            and interaction.user.id == self.request.requester.id
        ):
            return True
        await interaction.response.send_message(
            "Only the user who requested this media can provide retry cookies.",
            ephemeral=interaction.guild is not None,
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await self.request.status_message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Retry with cookies.txt",
        style=discord.ButtonStyle.primary,
        emoji="🍪",
    )
    async def retry_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        button.disabled = True
        await interaction.response.send_message(
            "Check your DMs and upload a Netscape-format `.txt` cookie file "
            f"within {round(COOKIE_UPLOAD_TIMEOUT)} seconds.",
            ephemeral=interaction.guild is not None,
        )
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()
        await self.handle_temporary_cookie_retry(interaction)

    async def handle_temporary_cookie_retry(
        self,
        interaction: discord.Interaction,
    ) -> None:
        user = interaction.user
        if user is None:
            return
        ephemeral = interaction.guild is not None
        try:
            upload_channel = await user.create_dm()
            await upload_channel.send(
                "Upload one Netscape-format `.txt` cookie file here within "
                f"{round(COOKIE_UPLOAD_TIMEOUT)} seconds. It will be filtered "
                "for the requested site and used only for this retry."
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not open a DM for the cookie upload.",
                ephemeral=ephemeral,
            )
            return

        def upload_check(candidate: discord.Message) -> bool:
            return (
                candidate.author.id == user.id
                and candidate.channel.id == upload_channel.id
                and any(
                    attachment.filename.lower().endswith(".txt")
                    for attachment in candidate.attachments
                )
            )

        try:
            upload_message = await self.bot.wait_for(
                "message",
                check=upload_check,
                timeout=COOKIE_UPLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Cookie upload timed out; the retry was cancelled.",
                ephemeral=ephemeral,
            )
            return

        attachment = next(
            item
            for item in upload_message.attachments
            if item.filename.lower().endswith(".txt")
        )
        try:
            if attachment.size > MAX_TEMP_COOKIE_BYTES:
                raise CookieFormatError(
                    "cookie file exceeds the 2 MiB temporary upload limit"
                )
            content = await attachment.read()
            cookie_data = prepare_temporary_cookies(content, self.request.url)
        except (CookieFormatError, discord.HTTPException) as error:
            await interaction.followup.send(
                f"Cookie upload rejected: {error}",
                ephemeral=ephemeral,
            )
            return
        finally:
            try:
                await upload_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "I could not delete your cookie upload; delete that DM immediately.",
                    ephemeral=ephemeral,
                )

        position = self.state.queue.qsize() + (1 if self.state.current else 0) + 1
        retry = MediaRequest(
            url=self.request.url,
            requester=self.request.requester,
            status_message=self.request.status_message,
            bot=self.bot,
            cookie_data=cookie_data,
        )
        await self.state.queue.put(retry)
        ensure_guild_worker(self.state)
        await self.request.status_message.edit(
            content=(
                "Temporary cookies accepted; retrying now…"
                if position == 1
                else f"Temporary cookies accepted; retry queued at position {position}."
            ),
            embed=None,
            view=None,
        )
        LOGGER.info(
            "Queued temporary-cookie retry guild=%s requester=%s target=%s",
            self.state.guild_id,
            user.id,
            cookie_target_for_url(self.request.url) or "generic",
        )
        await interaction.followup.send(
            "Cookie file accepted and queued for one retry.",
            ephemeral=ephemeral,
        )


# State is per guild even when this bot is configured for only one guild.
GUILD_STATE: dict[int, GuildState] = {}


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding existing environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def optional_snowflake(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        snowflake = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a numeric Discord ID") from error
    if snowflake <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return snowflake


def validate_media_url(value: str) -> str:
    url = value.strip().strip("<>")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Provide a complete http:// or https:// media URL.")
    return url


def cookie_target_for_url(url: str) -> str | None:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    for target, domains in COOKIE_TARGET_DOMAINS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return target
    return None


def cookie_domain_matches(domain: str, candidates: tuple[str, ...]) -> bool:
    normalized = domain.lower().lstrip(".")
    return any(
        normalized == candidate or normalized.endswith(f".{candidate}")
        for candidate in candidates
    )


def prepare_temporary_cookies(content: bytes, url: str) -> bytes:
    """Validate a Netscape cookie file and retain only the requested site."""
    if len(content) > MAX_TEMP_COOKIE_BYTES:
        raise CookieFormatError("cookie file exceeds the 2 MiB temporary upload limit")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CookieFormatError("cookie file must be UTF-8 text") from error

    lines = decoded.splitlines()
    if not lines or not lines[0].startswith(
        ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
    ):
        raise CookieFormatError("cookie file must use Netscape cookie format")

    target = cookie_target_for_url(url)
    domains = COOKIE_TARGET_DOMAINS.get(target, ())
    accepted: list[str] = []
    for line_number, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        record = line[len("#HttpOnly_") :] if line.startswith("#HttpOnly_") else line
        fields = record.split("\t")
        if len(fields) != 7:
            raise CookieFormatError(
                f"invalid Netscape cookie record on line {line_number}"
            )
        domain, include_subdomains, path, secure, expires, name, _ = fields
        clean_domain = domain.lstrip(".")
        if (
            not clean_domain
            or "." not in clean_domain
            or include_subdomains.upper() not in {"TRUE", "FALSE"}
            or secure.upper() not in {"TRUE", "FALSE"}
            or not path.startswith("/")
            or not name
        ):
            raise CookieFormatError(
                f"invalid Netscape cookie record on line {line_number}"
            )
        try:
            expires_value = int(expires) if expires else 0
        except ValueError as error:
            raise CookieFormatError(
                f"invalid Netscape cookie record on line {line_number}"
            ) from error
        if expires_value < 0:
            raise CookieFormatError(
                f"invalid Netscape cookie record on line {line_number}"
            )
        if not domains or cookie_domain_matches(domain, domains):
            accepted.append(line)

    if not accepted:
        site = target or "the requested site"
        raise CookieFormatError(f"cookie file contains no cookies for {site}")
    return (
        "# Netscape HTTP Cookie File\n" + "\n".join(accepted) + "\n"
    ).encode("utf-8")


def is_cookie_related_error(error: Exception | str) -> bool:
    lowered = str(error).lower()
    return any(pattern in lowered for pattern in COOKIE_AUTH_PATTERNS)


def is_sensitive_link(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").lower().rstrip(".")
    domain_match = any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in SENSITIVE_LINK_DOMAINS
    )
    return domain_match or any(
        marker in hostname for marker in SENSITIVE_HOST_MARKERS
    )


def redact_sensitive_links(value: str) -> str:
    return URL_IN_TEXT.sub(
        lambda match: (
            REDACTED_LINK
            if is_sensitive_link(match.group(0))
            else match.group(0)
        ),
        value,
    )


class RedactingFormatter(logging.Formatter):
    """Redact private media URLs after formatting messages and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_links(super().format(record))


def configure_logging() -> Path | None:
    """Configure console and rotating-file logs for the bot and discord.py."""
    level_name = os.environ.get("DISCORD_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise RuntimeError(
            "DISCORD_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )

    formatter = RedactingFormatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(console)

    configured_path = os.environ.get(
        "DISCORD_LOG_FILE",
        str(DEFAULT_LOG_FILE),
    ).strip()
    if configured_path.lower() in {"", "none", "off", "-"}:
        LOGGER.info("File logging is disabled")
        return None

    log_path = Path(configured_path).expanduser()
    if not log_path.is_absolute():
        log_path = APP_DIR / log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError as error:
        LOGGER.warning("Could not enable file logging at %s: %s", log_path, error)
        return None

    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    LOGGER.info("Logging initialized at %s (%s)", log_path, level_name)
    return log_path


def text_value(info: dict[str, object], key: str, fallback: str = "Unknown") -> str:
    value = info.get(key)
    return str(value) if yt_vlc.usable_metadata(value) else fallback


def runtime_programs() -> tuple[str, str, str]:
    """Prepare and locate bundled tools without racing startup and requests."""
    with TOOLS_LOCK:
        bundled_yt_dlp, bundled_vlc, bundled_deno = yt_vlc.ensure_bundled_tools()
    yt_dlp = yt_vlc.find_program("yt-dlp", bundled=bundled_yt_dlp)
    vlc = yt_vlc.find_program("vlc", bundled=bundled_vlc)
    deno = yt_vlc.find_program("deno", bundled=bundled_deno)
    yt_vlc.clear_live()
    return yt_dlp, vlc, deno


def resolve_discord_streams(
    yt_dlp: str,
    url: str,
    format_selector: str,
    cookie_file: Path | None = None,
    js_runtime: str | Path | None = None,
) -> tuple[list[str], dict[str, object]]:
    """Resolve streams and broaden the default selector once when necessary."""
    try:
        return yt_vlc.resolve_streams(
            yt_dlp,
            url,
            format_selector,
            cookie_file=cookie_file,
            js_runtime=js_runtime,
        )
    except RuntimeError as error:
        if (
            format_selector != yt_vlc.DEFAULT_FORMAT
            or "requested format is not available" not in str(error).lower()
        ):
            raise
        LOGGER.warning(
            "Default media format unavailable; retrying with flexible selector "
            "url=%s temporary_cookies=%s",
            url,
            cookie_file is not None,
        )
        return yt_vlc.resolve_streams(
            yt_dlp,
            url,
            FORMAT_AVAILABILITY_FALLBACK,
            cookie_file=cookie_file,
            js_runtime=js_runtime,
        )


def resolve_media(
    url: str,
    format_selector: str = yt_vlc.DEFAULT_FORMAT,
    cookie_data: bytes | None = None,
) -> PreparedMedia:
    """Resolve only the request currently at the head of a guild queue."""
    yt_dlp, vlc, deno = runtime_programs()

    if cookie_data is None:
        streams, media_info = resolve_discord_streams(
            yt_dlp,
            url,
            format_selector,
            js_runtime=deno,
        )
    else:
        LOGGER.info(
            "Resolving request with temporary cookies target=%s",
            cookie_target_for_url(url) or "generic",
        )
        with tempfile.TemporaryDirectory(prefix="yt-vlc-cookie-") as directory:
            cookie_path = Path(directory) / "cookies.txt"
            cookie_path.write_bytes(cookie_data)
            try:
                os.chmod(cookie_path, 0o600)
            except OSError:
                pass
            try:
                streams, media_info = resolve_discord_streams(
                    yt_dlp,
                    url,
                    format_selector,
                    cookie_file=cookie_path,
                    js_runtime=deno,
                )
            except RuntimeError as error:
                detail = str(error).replace(
                    str(cookie_path),
                    "<temporary-cookie-file>",
                )
                raise RuntimeError(detail) from error
    return PreparedMedia(vlc=vlc, streams=streams, info=media_info)


def now_playing_embed(
    info: dict[str, object],
    requester: discord.abc.User,
    stream_count: int,
) -> discord.Embed:
    title = text_value(info, "title", "Requested media")
    embed = discord.Embed(
        title="Now playing",
        description=title[:MAX_DISCORD_TEXT],
        color=0xF26B38,
    )
    creator = info.get("uploader")
    if not yt_vlc.usable_metadata(creator):
        creator = info.get("channel")

    duration = yt_vlc.human_duration(info.get("duration"))
    resolution = text_value(info, "resolution")
    fps = info.get("fps")
    if yt_vlc.usable_metadata(fps):
        resolution += f" @ {fps} fps"

    embed.add_field(name="Requested by", value=requester.mention, inline=True)
    embed.add_field(name="Duration", value=duration or "Unknown", inline=True)
    embed.add_field(name="Quality", value=resolution, inline=True)
    if yt_vlc.usable_metadata(creator):
        embed.add_field(name="Creator", value=str(creator)[:256], inline=True)
    embed.add_field(
        name="Streams",
        value="Video + audio" if stream_count == 2 else "Combined",
        inline=True,
    )
    return embed


def get_guild_state(guild_id: int) -> GuildState:
    state = GUILD_STATE.get(guild_id)
    if state is None:
        state = GuildState(guild_id=guild_id)
        GUILD_STATE[guild_id] = state
    return state


def pending_requests(state: GuildState) -> tuple[MediaRequest, ...]:
    """Return a stable display snapshot without removing queue entries."""
    return tuple(state.queue._queue)  # type: ignore[attr-defined]


def resolve_request_media(
    request: MediaRequest,
    format_selector: str = yt_vlc.DEFAULT_FORMAT,
) -> PreparedMedia:
    """Resolve a queued request, adding temporary cookies only when supplied."""
    if request.cookie_data is not None:
        return resolve_media(request.url, format_selector, request.cookie_data)
    if format_selector != yt_vlc.DEFAULT_FORMAT:
        return resolve_media(request.url, format_selector)
    return resolve_media(request.url)


def start_next_prefetch(state: GuildState) -> bool:
    queued = pending_requests(state)
    if not queued:
        return False
    request = queued[0]
    if state.prefetch is not None:
        if state.prefetch.request is request and not state.prefetch.task.cancelled():
            return True
        state.prefetch.task.cancel()
    task = asyncio.create_task(
        asyncio.to_thread(resolve_request_media, request),
        name=f"yt-vlc-prefetch-{state.guild_id}",
    )
    state.prefetch = PrefetchedRequest(request=request, task=task)
    LOGGER.info(
        "Started near-end prefetch guild=%s url=%s",
        state.guild_id,
        request.url,
    )
    return True


async def resolve_queued_request(
    state: GuildState,
    request: MediaRequest,
) -> PreparedMedia:
    if state.prefetch is not None and state.prefetch.request is request:
        task = state.prefetch.task
        state.prefetch = None
        return await task
    return await asyncio.to_thread(resolve_request_media, request)


def cancel_removed_prefetch(
    state: GuildState,
    removed: list[MediaRequest],
) -> None:
    if state.prefetch is None or not any(
        request is state.prefetch.request for request in removed
    ):
        return
    if state.prefetch.task.done() and not state.prefetch.task.cancelled():
        state.prefetch.task.exception()
    else:
        state.prefetch.task.cancel()
    state.prefetch = None


def drain_pending_requests(state: GuildState) -> list[MediaRequest]:
    """Remove every request that has not reached the worker yet."""
    removed: list[MediaRequest] = []
    while True:
        try:
            removed.append(state.queue.get_nowait())
            state.queue.task_done()
        except asyncio.QueueEmpty:
            cancel_removed_prefetch(state, removed)
            for request in removed:
                request.cookie_data = None
            return removed


async def mark_requests_removed(
    requests: list[MediaRequest],
    reason: str,
) -> None:
    if not requests:
        return
    await asyncio.gather(
        *(
            request.status_message.edit(content=reason, embed=None)
            for request in requests
        ),
        return_exceptions=True,
    )


def queue_line(request: MediaRequest, label: str) -> str:
    if is_sensitive_link(request.url):
        rendered_url = f"`{REDACTED_LINK}`"
    else:
        url = request.url
        if len(url) > 140:
            url = f"{url[:137]}..."
        rendered_url = f"<{url}>"
    return f"**{label}** {rendered_url} — {request.requester.mention}"


def queue_embed(state: GuildState | None) -> discord.Embed:
    current = state.current if state is not None else None
    queued = pending_requests(state) if state is not None else ()
    entries: list[tuple[MediaRequest, str]] = []
    if current is not None:
        entries.append((current, "Current"))
    entries.extend(
        (request, str(position))
        for position, request in enumerate(queued, start=1)
    )

    lines: list[str] = []
    for request, label in entries:
        line = queue_line(request, label)
        candidate = "\n".join((*lines, line))
        if len(candidate) > MAX_QUEUE_EMBED_DESCRIPTION - 40:
            break
        lines.append(line)

    omitted = len(entries) - len(lines)
    if omitted:
        lines.append(f"*...and {omitted} more request{'s' if omitted != 1 else ''}.*")

    description = (
        "\n".join(lines)
        if lines
        else "No media is currently playing or queued."
    )
    embed = discord.Embed(
        title="Request queue",
        description=description,
        color=0xF26B38,
    )
    pending_count = len(queued)
    embed.set_footer(
        text=(
            f"{pending_count} pending request"
            f"{'s' if pending_count != 1 else ''}"
        )
    )
    return embed


async def run_guild_queue(state: GuildState) -> None:
    """Resolve, play, and recover queued requests for one guild."""
    while True:
        request = await state.queue.get()
        used_temporary_cookies = request.cookie_data is not None
        state.current = request
        state.completion_note = None
        state.cancel_current = False
        LOGGER.info(
            "Dequeued request guild=%s requester=%s pending=%s url=%s",
            state.guild_id,
            getattr(request.requester, "id", "unknown"),
            state.queue.qsize(),
            request.url,
        )
        try:
            using_prefetch = (
                state.prefetch is not None
                and state.prefetch.request is request
            )
            await request.status_message.edit(
                content=(
                    "Starting prepared request…"
                    if using_prefetch
                    else "Resolving request…"
                )
            )
            media = await resolve_queued_request(state, request)
            LOGGER.info(
                "Resolved request guild=%s title=%r streams=%s duration=%s",
                state.guild_id,
                text_value(media.info, "title", "Requested media"),
                len(media.streams),
                yt_vlc.human_duration(media.info.get("duration")) or "unknown",
            )
            if state.cancel_current:
                await request.status_message.edit(
                    content=state.completion_note or "Request cancelled.",
                    embed=None,
                )
                continue
            if state.vlc is None or state.vlc.executable != media.vlc:
                state.vlc = VLCSession(media.vlc)

            await request.status_message.edit(content="Starting VLC…", embed=None)
            initial_status = await asyncio.to_thread(state.vlc.play, media)
            if state.cancel_current:
                await asyncio.to_thread(state.vlc.stop)
                await request.status_message.edit(
                    content=state.completion_note or "Request cancelled.",
                    embed=None,
                )
                continue
            embed = now_playing_embed(
                media.info,
                request.requester,
                len(media.streams),
            )
            await request.status_message.edit(content=None, embed=embed)

            recovered = False
            try:
                await state.vlc.wait_until_finished(
                    initial_status,
                    on_near_end=lambda: start_next_prefetch(state),
                )
            except PlaybackStalled as stalled:
                LOGGER.warning(
                    "Playback stalled guild=%s position=%s; resolving 720p fallback",
                    state.guild_id,
                    yt_vlc.human_duration(stalled.position_seconds),
                )
                if state.cancel_current:
                    await request.status_message.edit(
                        content=state.completion_note or "Request cancelled.",
                        embed=None,
                    )
                    continue
                await request.status_message.edit(
                    content="Playback stalled; reconnecting at up to 720p…",
                    embed=embed,
                )
                media = await asyncio.to_thread(
                    resolve_request_media,
                    request,
                    STABILITY_FALLBACK_FORMAT,
                )
                if state.cancel_current:
                    await request.status_message.edit(
                        content=state.completion_note or "Request cancelled.",
                        embed=None,
                    )
                    continue
                initial_status = await asyncio.to_thread(state.vlc.play, media)
                if state.cancel_current:
                    await asyncio.to_thread(state.vlc.stop)
                    await request.status_message.edit(
                        content=state.completion_note or "Request cancelled.",
                        embed=None,
                    )
                    continue
                if stalled.position_seconds >= 2:
                    try:
                        await asyncio.to_thread(
                            state.vlc.seek_when_ready,
                            stalled.position_seconds,
                        )
                    except (OSError, RuntimeError, ValueError):
                        pass
                embed = now_playing_embed(
                    media.info,
                    request.requester,
                    len(media.streams),
                )
                embed.set_footer(text="Reconnected after a playback stall")
                await request.status_message.edit(content=None, embed=embed)
                recovered = True
                LOGGER.info(
                    "Playback recovered guild=%s position=%s",
                    state.guild_id,
                    yt_vlc.human_duration(stalled.position_seconds),
                )
                await state.vlc.wait_until_finished(
                    initial_status,
                    on_near_end=lambda: start_next_prefetch(state),
                )

            completion = state.completion_note or "Playback finished"
            if recovered and state.completion_note is None:
                completion = "Playback finished • recovered at up to 720p"
            embed.set_footer(text=completion)
            await request.status_message.edit(embed=embed)
            LOGGER.info("Playback completed guild=%s result=%s", state.guild_id, completion)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception(
                "Request failed guild=%s url=%s",
                state.guild_id,
                request.url,
            )
            message = redact_sensitive_links(
                str(error).strip() or type(error).__name__
            )
            retry_view: CookieRetryView | None = None
            retry_note = ""
            if is_cookie_related_error(error):
                if used_temporary_cookies:
                    retry_note = (
                        "\nThe temporary cookies did not unlock this media; "
                        "export a fresh logged-in session before trying again."
                    )
                elif request.bot is not None:
                    retry_view = CookieRetryView(request.bot, state, request)
                    retry_note = (
                        "\nThis looks like a login or bot-check failure. Use the "
                        "button below to provide a temporary cookies.txt file in DM."
                    )
            await request.status_message.edit(
                content=f"Request failed: {message[:MAX_DISCORD_TEXT]}{retry_note}",
                embed=None,
                view=retry_view,
            )
        finally:
            request.cookie_data = None
            state.current = None
            state.completion_note = None
            state.cancel_current = False
            state.queue.task_done()


def ensure_guild_worker(state: GuildState) -> None:
    if state.worker is None or state.worker.done():
        state.worker = asyncio.create_task(
            run_guild_queue(state),
            name=f"yt-vlc-guild-{state.guild_id}",
        )


async def warm_up_vlc(state: GuildState) -> None:
    """Start an idle VLC controller so the first request is ready to hand off."""
    _, executable, _ = await asyncio.to_thread(runtime_programs)
    if state.vlc is None:
        state.vlc = VLCSession(executable)
    await asyncio.to_thread(state.vlc.ensure_started)
    LOGGER.info(
        "VLC playback warm-up complete guild=%s pid=%s",
        state.guild_id,
        state.vlc.process.pid if state.vlc.process is not None else "unknown",
    )


def build_bot(
    request_channel_id: int | None,
    configured_guild_id: int | None,
) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(
        command_prefix=COMMAND_PREFIX,
        intents=intents,
        allowed_mentions=discord.AllowedMentions.none(),
        description="Send media requests to yt-vlc.",
    )
    vlc_warmup_lock = asyncio.Lock()

    @bot.check
    async def owner_only_dms(ctx: commands.Context[commands.Bot]) -> bool:
        return ctx.guild is not None or await bot.is_owner(ctx.author)

    def dm_target_guild_id() -> int | None:
        if configured_guild_id is not None:
            return configured_guild_id
        if len(bot.guilds) == 1:
            return bot.guilds[0].id
        return None

    async def allowed_context(ctx: commands.Context[commands.Bot]) -> bool:
        if ctx.guild is None:
            if not await bot.is_owner(ctx.author):
                return False
            if dm_target_guild_id() is None:
                await ctx.reply(
                    "Set `DISCORD_GUILD_ID` to choose the server controlled "
                    "by DM commands.",
                    mention_author=False,
                )
                return False
            return True
        if configured_guild_id is not None and ctx.guild.id != configured_guild_id:
            return False
        return request_channel_id is None or ctx.channel.id == request_channel_id

    def target_guild_id(ctx: commands.Context[commands.Bot]) -> int:
        guild_id = ctx.guild.id if ctx.guild is not None else dm_target_guild_id()
        if guild_id is None:
            raise RuntimeError("Could not determine the DM command's target guild")
        return guild_id

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        guild_note = (
            f"guild {configured_guild_id}" if configured_guild_id else "visible guilds"
        )
        channel_note = (
            f"channel {request_channel_id}" if request_channel_id else "all visible channels"
        )
        LOGGER.info(
            "Discord bot ready as %s (%s, %s)",
            bot.user,
            guild_note,
            channel_note,
        )
        warmup_guild_id = dm_target_guild_id()
        if warmup_guild_id is None:
            LOGGER.warning(
                "VLC warm-up skipped because no single target guild is available; "
                "set DISCORD_GUILD_ID"
            )
            return
        if bot.get_guild(warmup_guild_id) is None:
            LOGGER.warning(
                "VLC warm-up skipped because configured guild %s is not visible",
                warmup_guild_id,
            )
            return
        async with vlc_warmup_lock:
            try:
                await warm_up_vlc(get_guild_state(warmup_guild_id))
            except (OSError, RuntimeError, ValueError):
                LOGGER.exception(
                    "VLC playback warm-up failed guild=%s; the first request "
                    "will retry initialization",
                    warmup_guild_id,
                )

    @bot.command(name="play", aliases=["request", "p"])
    async def play_command(ctx: commands.Context[commands.Bot], *, url: str) -> None:
        """Queue a URL for lazy VLC playback."""
        if not await allowed_context(ctx):
            return
        try:
            media_url = validate_media_url(url)
        except ValueError as error:
            await ctx.reply(str(error), mention_author=False)
            return

        source_deleted = False
        if ctx.guild is not None and is_sensitive_link(media_url):
            try:
                await ctx.message.delete()
                source_deleted = True
            except discord.NotFound:
                source_deleted = True
            except discord.Forbidden:
                await ctx.send(
                    "I could not safely accept that private link. Grant the bot "
                    "**Manage Messages** permission so it can remove the original "
                    "request.",
                )
                return
            except discord.HTTPException:
                await ctx.send(
                    "I could not delete the message containing that private link, "
                    "so the request was not queued.",
                )
                return

        state = get_guild_state(target_guild_id(ctx))
        position = state.queue.qsize() + (1 if state.current else 0) + 1
        status_text = (
            "Resolving request…"
            if position == 1
            else f"Queued at position {position}."
        )
        if source_deleted:
            progress = await ctx.send(status_text)
        else:
            progress = await ctx.reply(status_text, mention_author=False)
        await state.queue.put(
            MediaRequest(
                url=media_url,
                requester=ctx.author,
                status_message=progress,
                bot=bot,
            )
        )
        LOGGER.info(
            "Queued request guild=%s requester=%s position=%s source_deleted=%s url=%s",
            state.guild_id,
            getattr(ctx.author, "id", "unknown"),
            position,
            source_deleted,
            media_url,
        )
        ensure_guild_worker(state)

    @play_command.error
    async def play_error(
        ctx: commands.Context[commands.Bot],
        error: commands.CommandError,
    ) -> None:
        if not await allowed_context(ctx):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                f"Usage: `{COMMAND_PREFIX}play <media URL>`",
                mention_author=False,
            )
            return
        raise error

    @bot.command(name="pause")
    async def pause_command(ctx: commands.Context[commands.Bot]) -> None:
        """Pause the current VLC item."""
        if not await allowed_context(ctx):
            return
        state = GUILD_STATE.get(target_guild_id(ctx))
        if (
            state is None
            or state.current is None
            or state.vlc is None
            or not state.vlc.is_running()
        ):
            await ctx.reply("Nothing is currently playing.", mention_author=False)
            return
        try:
            await asyncio.to_thread(state.vlc.pause)
            await ctx.reply("Playback paused.", mention_author=False)
        except Exception as error:
            LOGGER.exception("Pause command failed guild=%s", state.guild_id)
            await ctx.reply(
                f"Could not pause VLC: {str(error)[:MAX_DISCORD_TEXT]}",
                mention_author=False,
            )

    @bot.command(name="resume")
    async def resume_command(ctx: commands.Context[commands.Bot]) -> None:
        """Resume the current VLC item."""
        if not await allowed_context(ctx):
            return
        state = GUILD_STATE.get(target_guild_id(ctx))
        if (
            state is None
            or state.current is None
            or state.vlc is None
            or not state.vlc.is_running()
        ):
            await ctx.reply("Nothing is available to resume.", mention_author=False)
            return
        try:
            await asyncio.to_thread(state.vlc.resume)
            await ctx.reply("Playback resumed.", mention_author=False)
        except Exception as error:
            LOGGER.exception("Resume command failed guild=%s", state.guild_id)
            await ctx.reply(
                f"Could not resume VLC: {str(error)[:MAX_DISCORD_TEXT]}",
                mention_author=False,
            )

    @bot.command(name="skip", aliases=["next", "s"])
    async def skip_command(ctx: commands.Context[commands.Bot]) -> None:
        """Stop the current item and advance the lazy queue."""
        if not await allowed_context(ctx):
            return
        state = GUILD_STATE.get(target_guild_id(ctx))
        if state is None or state.current is None:
            await ctx.reply("Nothing is currently playing.", mention_author=False)
            return
        try:
            state.completion_note = f"Skipped by {ctx.author.display_name}"
            state.cancel_current = True
            if state.vlc is not None and state.vlc.is_running():
                await asyncio.to_thread(state.vlc.stop)
            LOGGER.info(
                "Skip requested guild=%s requester=%s",
                state.guild_id,
                getattr(ctx.author, "id", "unknown"),
            )
            await ctx.reply("Skipped the current request.", mention_author=False)
        except Exception as error:
            LOGGER.exception("Skip command failed guild=%s", state.guild_id)
            state.completion_note = None
            state.cancel_current = False
            await ctx.reply(
                f"Could not skip the request: {str(error)[:MAX_DISCORD_TEXT]}",
                mention_author=False,
            )

    @bot.command(name="stop")
    async def stop_command(ctx: commands.Context[commands.Bot]) -> None:
        """Stop playback, clear pending requests, and leave VLC open."""
        if not await allowed_context(ctx):
            return
        state = GUILD_STATE.get(target_guild_id(ctx))
        if state is None:
            await ctx.reply("Nothing is playing or queued.", mention_author=False)
            return

        removed = drain_pending_requests(state)
        stopped = state.current is not None
        try:
            if state.current is not None:
                state.completion_note = f"Stopped by {ctx.author.display_name}"
                state.cancel_current = True
                if state.vlc is not None and state.vlc.is_running():
                    await asyncio.to_thread(state.vlc.stop)
            elif state.vlc is not None and state.vlc.is_running() and removed:
                await asyncio.to_thread(state.vlc.stop)
        except Exception as error:
            LOGGER.exception("Stop command failed guild=%s", state.guild_id)
            state.completion_note = None
            state.cancel_current = False
            await ctx.reply(
                f"Could not stop VLC: {str(error)[:MAX_DISCORD_TEXT]}",
                mention_author=False,
            )
            return
        finally:
            await mark_requests_removed(
                removed,
                f"Removed from the queue by {ctx.author.display_name}.",
            )

        if not stopped and not removed:
            await ctx.reply("Nothing is playing or queued.", mention_author=False)
            return
        LOGGER.info(
            "Stop requested guild=%s requester=%s stopped_current=%s cleared=%s",
            state.guild_id,
            getattr(ctx.author, "id", "unknown"),
            stopped,
            len(removed),
        )
        queue_note = (
            f" Cleared {len(removed)} pending request"
            f"{'s' if len(removed) != 1 else ''}."
            if removed
            else ""
        )
        await ctx.reply(
            f"Playback stopped; VLC remains open.{queue_note}",
            mention_author=False,
        )

    @bot.command(name="queue", aliases=["q"])
    async def queue_command(ctx: commands.Context[commands.Bot]) -> None:
        """Show the current item and unresolved pending URLs."""
        if not await allowed_context(ctx):
            return
        state = GUILD_STATE.get(target_guild_id(ctx))
        await ctx.reply(embed=queue_embed(state), mention_author=False)

    return bot


def main() -> int:
    load_env_file(ENV_FILE)
    try:
        configure_logging()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        LOGGER.error("Set DISCORD_BOT_TOKEN in .env or the environment")
        return 1

    try:
        configured_guild_id = optional_snowflake("DISCORD_GUILD_ID")
        request_channel_id = optional_snowflake("DISCORD_REQUEST_CHANNEL_ID")
        bot = build_bot(request_channel_id, configured_guild_id)
        LOGGER.info("Starting Discord bot")
        bot.run(token, log_handler=None)
        LOGGER.info("Discord bot stopped")
        return 0
    except (RuntimeError, discord.DiscordException) as error:
        LOGGER.exception("Discord bot stopped with an error: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
