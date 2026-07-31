#!/usr/bin/env python3
"""Discord request bot for yt-vlc."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import discord
from discord.ext import commands

import yt_vlc


APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
COMMAND_PREFIX = "!"
MAX_DISCORD_TEXT = 1_000
MAX_QUEUE_EMBED_DESCRIPTION = 4_000
VLC_START_TIMEOUT = 10.0
VLC_PLAYBACK_TIMEOUT = 30.0
VLC_POLL_INTERVAL = 0.5
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
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
REDACTED_LINK = "[sensitive debrid link redacted]"


@dataclass(slots=True)
class MediaRequest:
    url: str
    requester: discord.User | discord.Member
    status_message: discord.Message


@dataclass(slots=True)
class PreparedMedia:
    vlc: str
    streams: list[str]
    info: dict[str, object]


class VLCSession:
    """Keep one VLC window alive and control its playlist over localhost."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.port = self._available_port()
        self.password = secrets.token_urlsafe(24)
        self.process: subprocess.Popen[bytes] | None = None

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
        deadline = time.monotonic() + VLC_START_TIMEOUT
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"VLC exited immediately with code {self.process.returncode}"
                )
            try:
                self.status()
                return
            except (OSError, ValueError) as error:
                last_error = error
                time.sleep(0.1)
        raise RuntimeError("VLC's local control interface did not start") from last_error

    def ensure_started(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self._wait_until_ready()
            return

        self.port = self._available_port()
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
        self.process = subprocess.Popen(
            command,
            cwd=Path(self.executable).parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready()

    def play(self, media: PreparedMedia) -> dict[str, object]:
        self.ensure_started()
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
        return self._request({"command": "pl_forcepause"})

    def resume(self) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        return self._request({"command": "pl_forceresume"})

    def stop(self) -> dict[str, object]:
        if not self.is_running():
            raise RuntimeError("VLC is not running")
        return self._request({"command": "pl_stop"})

    async def wait_until_finished(
        self,
        initial_status: dict[str, object],
    ) -> None:
        """Wait for the current item to stop without waiting for VLC to exit."""
        state_name = str(initial_status.get("state", "")).lower()
        seen_active = state_name in ACTIVE_VLC_STATES
        stopped_since: float | None = None
        start_deadline = time.monotonic() + VLC_PLAYBACK_TIMEOUT

        while True:
            if self.process is None or self.process.poll() is not None:
                exit_code = None if self.process is None else self.process.returncode
                raise RuntimeError(f"VLC closed during playback (code {exit_code})")

            status = await asyncio.to_thread(self.status)
            state_name = str(status.get("state", "")).lower()
            now = time.monotonic()

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


def text_value(info: dict[str, object], key: str, fallback: str = "Unknown") -> str:
    value = info.get(key)
    return str(value) if yt_vlc.usable_metadata(value) else fallback


def resolve_media(url: str) -> PreparedMedia:
    """Resolve only the request currently at the head of a guild queue."""
    bundled_yt_dlp, bundled_vlc = yt_vlc.ensure_bundled_tools()
    yt_dlp = yt_vlc.find_program("yt-dlp", bundled=bundled_yt_dlp)
    vlc = yt_vlc.find_program("vlc", bundled=bundled_vlc)
    streams, media_info = yt_vlc.resolve_streams(
        yt_dlp,
        url,
        yt_vlc.DEFAULT_FORMAT,
    )
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


def drain_pending_requests(state: GuildState) -> list[MediaRequest]:
    """Remove every request that has not reached the worker yet."""
    removed: list[MediaRequest] = []
    while True:
        try:
            removed.append(state.queue.get_nowait())
            state.queue.task_done()
        except asyncio.QueueEmpty:
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
    """Lazily resolve and play one request at a time for a guild."""
    while True:
        request = await state.queue.get()
        state.current = request
        state.completion_note = None
        state.cancel_current = False
        try:
            await request.status_message.edit(content="Resolving request…")
            media = await asyncio.to_thread(resolve_media, request.url)
            if state.cancel_current:
                await request.status_message.edit(
                    content=state.completion_note or "Request cancelled.",
                    embed=None,
                )
                continue
            if state.vlc is None or state.vlc.executable != media.vlc:
                state.vlc = VLCSession(media.vlc)

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

            await state.vlc.wait_until_finished(initial_status)
            embed.set_footer(text=state.completion_note or "Playback finished")
            await request.status_message.edit(embed=embed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = redact_sensitive_links(
                str(error).strip() or type(error).__name__
            )
            await request.status_message.edit(
                content=f"Request failed: {message[:MAX_DISCORD_TEXT]}",
                embed=None,
            )
        finally:
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
        print(f"Discord bot ready as {bot.user} ({guild_note}, {channel_note})")

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
            )
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
            await ctx.reply("Skipped the current request.", mention_author=False)
        except Exception as error:
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
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print(
            "error: set DISCORD_BOT_TOKEN in .env or the environment",
            file=sys.stderr,
        )
        return 1

    try:
        configured_guild_id = optional_snowflake("DISCORD_GUILD_ID")
        request_channel_id = optional_snowflake("DISCORD_REQUEST_CHANNEL_ID")
        bot = build_bot(request_channel_id, configured_guild_id)
        bot.run(token, log_handler=None)
        return 0
    except (RuntimeError, discord.DiscordException) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
