from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import discord

import discord_bot
import yt_vlc


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.deleted = False

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def delete(self) -> None:
        self.deleted = True


class FakeRequester:
    id = 789
    mention = "@requester"
    display_name = "requester"


class FakeGuild:
    id = 123


class FakeChannel:
    id = 456


class FakeContext:
    guild = FakeGuild()
    channel = FakeChannel()
    author = FakeRequester()

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_options: list[dict[str, object]] = []
        self.sent: list[str] = []
        self.message = FakeMessage()
        self.response_message = FakeMessage()

    async def reply(
        self,
        content: str = "",
        **options: object,
    ) -> FakeMessage:
        self.replies.append(content)
        self.reply_options.append(options)
        return self.response_message

    async def send(self, content: str = "", **_: object) -> FakeMessage:
        self.sent.append(content)
        return self.response_message


class FakeVLCSession:
    def __init__(
        self,
        executable: str,
        first_finished: asyncio.Event,
        second_finished: asyncio.Event,
        trigger_prefetch: bool = False,
    ) -> None:
        self.executable = executable
        self.first_finished = first_finished
        self.second_finished = second_finished
        self.played: list[str] = []
        self.stop_calls = 0
        self.trigger_prefetch = trigger_prefetch

    def play(self, media: discord_bot.PreparedMedia) -> dict[str, object]:
        self.played.append(media.streams[0])
        return {"state": "playing"}

    async def wait_until_finished(
        self,
        _: dict[str, object],
        on_near_end: object = None,
        should_stop_waiting: object = None,
    ) -> None:
        current = self.played[-1]
        if (
            self.trigger_prefetch
            and current == "https://example.com/first"
            and callable(on_near_end)
        ):
            on_near_end()
        finished = (
            self.first_finished
            if current == "https://example.com/first"
            else self.second_finished
        )
        await finished.wait()

    def is_running(self) -> bool:
        return True

    def stop(self) -> dict[str, object]:
        self.stop_calls += 1
        return {"state": "stopped"}


class GuildQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_vlc_warmup_starts_idle_reusable_session(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        session = MagicMock()
        session.executable = "vlc.exe"
        session.process.pid = 4321

        with (
            patch.object(
                discord_bot,
                "runtime_programs",
                return_value=("yt-dlp.exe", "vlc.exe", "deno.exe"),
            ),
            patch.object(
                discord_bot,
                "VLCSession",
                return_value=session,
            ) as session_type,
        ):
            await discord_bot.warm_up_vlc(state)

        session_type.assert_called_once_with("vlc.exe")
        session.ensure_started.assert_called_once_with()
        self.assertIs(state.vlc, session)

    async def test_ready_event_warms_vlc_for_the_only_guild(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=None)
        guild = FakeGuild()
        with (
            patch.object(
                type(bot),
                "user",
                new_callable=PropertyMock,
                return_value=SimpleNamespace(__str__=lambda _: "bot"),
            ),
            patch.object(
                type(bot),
                "guilds",
                new_callable=PropertyMock,
                return_value=[guild],
            ),
            patch.object(bot, "get_guild", return_value=guild),
            patch.object(discord_bot, "warm_up_vlc", new=AsyncMock()) as warmup,
        ):
            await bot.on_ready()

        state = discord_bot.GUILD_STATE.pop(guild.id)
        warmup.assert_awaited_once_with(state)

    def test_temporary_cookies_are_validated_and_filtered_for_site(self) -> None:
        content = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-secret\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tinstagram-secret\n"
        ).encode()

        prepared = discord_bot.prepare_temporary_cookies(
            content,
            "https://www.youtube.com/watch?v=example",
        ).decode()

        self.assertIn("youtube-secret", prepared)
        self.assertNotIn("instagram-secret", prepared)

    def test_youtube_short_urls_use_youtube_cookie_filter(self) -> None:
        self.assertEqual(
            discord_bot.cookie_target_for_url("https://youtu.be/HSpJOu73cvM"),
            "youtube",
        )

    def test_unavailable_default_format_retries_with_flexible_selector(self) -> None:
        cookie_path = Path("temporary-cookies.txt")
        with patch.object(
            yt_vlc,
            "resolve_streams",
            side_effect=[
                RuntimeError("Requested format is not available"),
                (["https://example.com/video", "https://example.com/audio"], {}),
            ],
        ) as resolve:
            streams, _ = discord_bot.resolve_discord_streams(
                "yt-dlp.exe",
                "https://youtu.be/HSpJOu73cvM",
                yt_vlc.DEFAULT_FORMAT,
                cookie_file=cookie_path,
                js_runtime="deno.exe",
            )

        self.assertEqual(len(streams), 2)
        self.assertEqual(
            [item.args[2] for item in resolve.call_args_list],
            [yt_vlc.DEFAULT_FORMAT, discord_bot.FORMAT_AVAILABILITY_FALLBACK],
        )
        self.assertEqual(
            [item.kwargs["cookie_file"] for item in resolve.call_args_list],
            [cookie_path, cookie_path],
        )
        self.assertEqual(
            [item.kwargs["js_runtime"] for item in resolve.call_args_list],
            ["deno.exe", "deno.exe"],
        )

    def test_malformed_temporary_cookie_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            discord_bot.CookieFormatError,
            "Netscape cookie format",
        ):
            discord_bot.prepare_temporary_cookies(
                b"not a cookie file",
                "https://www.youtube.com/watch?v=example",
            )

    def test_resolve_media_uses_and_removes_temporary_cookie_file(self) -> None:
        observed_path: Path | None = None

        def fake_resolve(
            _yt_dlp: str,
            _url: str,
            _selector: str,
            cookie_file: Path | None = None,
            js_runtime: str | Path | None = None,
        ) -> tuple[list[str], dict[str, object]]:
            nonlocal observed_path
            self.assertIsNotNone(cookie_file)
            self.assertEqual(js_runtime, "deno.exe")
            observed_path = Path(cookie_file)  # type: ignore[arg-type]
            self.assertTrue(observed_path.is_file())
            self.assertIn("youtube-secret", observed_path.read_text(encoding="utf-8"))
            return ["https://example.com/stream"], {"title": "Private media"}

        cookie_data = discord_bot.prepare_temporary_cookies(
            (
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-secret\n"
            ).encode(),
            "https://www.youtube.com/watch?v=example",
        )
        with (
            patch.object(
                yt_vlc,
                "ensure_bundled_tools",
                return_value=(
                    Path("yt-dlp.exe"),
                    Path("vlc.exe"),
                    Path("deno.exe"),
                ),
            ),
            patch.object(
                yt_vlc,
                "find_program",
                side_effect=["yt-dlp.exe", "vlc.exe", "deno.exe"],
            ),
            patch.object(yt_vlc, "resolve_streams", side_effect=fake_resolve),
        ):
            media = discord_bot.resolve_media(
                "https://www.youtube.com/watch?v=example",
                cookie_data=cookie_data,
            )

        self.assertEqual(media.streams, ["https://example.com/stream"])
        self.assertIsNotNone(observed_path)
        self.assertFalse(observed_path.exists())  # type: ignore[union-attr]

    def test_yt_dlp_resolution_receives_cookie_file_argument(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="https://example.com/stream\n",
            stderr="",
        )
        with (
            patch.object(yt_vlc, "BrailleSpinner", return_value=MagicMock()),
            patch.object(
                yt_vlc.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            streams, _ = yt_vlc.resolve_streams(
                "yt-dlp.exe",
                "https://www.youtube.com/watch?v=private",
                yt_vlc.DEFAULT_FORMAT,
                cookie_file=Path("cookies.txt"),
                js_runtime=Path("deno.exe"),
            )

        command = run.call_args.args[0]
        self.assertEqual(streams, ["https://example.com/stream"])
        cookie_index = command.index("--cookies")
        self.assertEqual(command[cookie_index + 1], "cookies.txt")
        runtime_index = command.index("--js-runtimes")
        self.assertEqual(
            command[runtime_index + 1],
            f"deno:{Path('deno.exe').resolve()}",
        )

    async def test_auth_failure_offers_requester_scoped_cookie_retry(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        message = FakeMessage()
        state = discord_bot.GuildState(guild_id=123)
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://www.youtube.com/watch?v=private",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=message,  # type: ignore[arg-type]
                bot=bot,
            )
        )

        with patch.object(
            discord_bot,
            "resolve_request_media",
            side_effect=RuntimeError("Sign in to confirm you are not a bot"),
        ):
            worker = asyncio.create_task(discord_bot.run_guild_queue(state))
            try:
                await asyncio.wait_for(state.queue.join(), timeout=1)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

        retry_view = message.edits[-1]["view"]
        self.assertIsInstance(retry_view, discord_bot.CookieRetryView)
        self.assertIn("temporary cookies.txt", message.edits[-1]["content"])
        retry_view.stop()

    async def test_cookie_retry_upload_is_deleted_filtered_and_requeued(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        status_message = FakeMessage()
        upload_channel = SimpleNamespace(id=999, send=AsyncMock())
        requester = SimpleNamespace(
            id=789,
            mention="@requester",
            display_name="requester",
            create_dm=AsyncMock(return_value=upload_channel),
        )
        cookie_content = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-secret\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tother-secret\n"
        ).encode()
        attachment = SimpleNamespace(
            filename="cookies.txt",
            size=len(cookie_content),
            read=AsyncMock(return_value=cookie_content),
        )
        upload_message = SimpleNamespace(
            author=requester,
            channel=upload_channel,
            attachments=[attachment],
            delete=AsyncMock(),
        )
        bot = SimpleNamespace(wait_for=AsyncMock(return_value=upload_message))
        request = discord_bot.MediaRequest(
            url="https://www.youtube.com/watch?v=private",
            requester=requester,  # type: ignore[arg-type]
            status_message=status_message,  # type: ignore[arg-type]
            bot=bot,  # type: ignore[arg-type]
        )
        view = discord_bot.CookieRetryView(bot, state, request)  # type: ignore[arg-type]
        interaction = SimpleNamespace(
            user=requester,
            guild=SimpleNamespace(id=123),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(discord_bot, "ensure_guild_worker") as ensure_worker:
            await view.handle_temporary_cookie_retry(interaction)  # type: ignore[arg-type]

        retried = state.queue.get_nowait()
        state.queue.task_done()
        self.assertIsNotNone(retried.cookie_data)
        prepared = retried.cookie_data.decode()  # type: ignore[union-attr]
        self.assertIn("youtube-secret", prepared)
        self.assertNotIn("other-secret", prepared)
        upload_message.delete.assert_awaited_once_with()
        ensure_worker.assert_called_once_with(state)
        self.assertIsNone(status_message.edits[-1]["view"])
        view.stop()

    def test_log_formatter_redacts_private_links_only(self) -> None:
        sensitive = "https://nexus-220.cnam.tb-cdn.io/file?token=secret"
        youtube = "https://www.youtube.com/watch?v=public-video"
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=f"private={sensitive} public={youtube}",
            args=(),
            exc_info=None,
        )

        rendered = discord_bot.RedactingFormatter("%(message)s").format(record)

        self.assertNotIn("secret", rendered)
        self.assertIn(discord_bot.REDACTED_LINK, rendered)
        self.assertIn(youtube, rendered)

    def test_default_format_rejects_low_resolution_combined_streams(self) -> None:
        self.assertEqual(
            yt_vlc.DEFAULT_FORMAT,
            "b[height>=720][height<=1080]/"
            "bv*[height<=1080]+ba/b[height<=1080]/b",
        )

    async def test_stalled_playback_is_detected(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]
        stalled_status = {"state": "playing", "time": 10, "length": 100}
        with (
            patch.object(session, "status", return_value=stalled_status),
            patch.object(
                discord_bot.time,
                "monotonic",
                side_effect=[0.0, discord_bot.VLC_STALL_TIMEOUT + 1],
            ),
        ):
            with self.assertRaises(discord_bot.PlaybackStalled) as raised:
                await session.wait_until_finished(stalled_status)

        self.assertEqual(raised.exception.position_seconds, 10)

    async def test_stall_reconnects_once_at_720p_and_resumes_position(self) -> None:
        class RecoveringVLC:
            executable = "vlc.exe"

            def __init__(self) -> None:
                self.played: list[str] = []
                self.wait_calls = 0
                self.seek_positions: list[float] = []

            def play(self, media: discord_bot.PreparedMedia) -> dict[str, object]:
                self.played.append(media.streams[0])
                return {"state": "playing"}

            async def wait_until_finished(
                self,
                _: dict[str, object],
                on_near_end: object = None,
                should_stop_waiting: object = None,
            ) -> None:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise discord_bot.PlaybackStalled(42)

            def seek_when_ready(self, seconds: float) -> dict[str, object]:
                self.seek_positions.append(seconds)
                return {"state": "playing"}

        selectors: list[str] = []

        def fake_resolve(
            _: str,
            selector: str = yt_vlc.DEFAULT_FORMAT,
        ) -> discord_bot.PreparedMedia:
            selectors.append(selector)
            return discord_bot.PreparedMedia(
                vlc="vlc.exe",
                streams=[f"stream-{len(selectors)}"],
                info={"title": "Test media"},
            )

        state = discord_bot.GuildState(guild_id=123)
        vlc = RecoveringVLC()
        state.vlc = vlc  # type: ignore[assignment]
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/media",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
            )
        )

        with (
            patch.object(discord_bot, "resolve_media", side_effect=fake_resolve),
            patch.object(
                discord_bot,
                "now_playing_embed",
                return_value=discord.Embed(title="Now playing"),
            ),
        ):
            worker = asyncio.create_task(discord_bot.run_guild_queue(state))
            try:
                await asyncio.wait_for(state.queue.join(), timeout=1)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

        self.assertEqual(
            selectors,
            [yt_vlc.DEFAULT_FORMAT, discord_bot.STABILITY_FALLBACK_FORMAT],
        )
        self.assertEqual(vlc.played, ["stream-1", "stream-2"])
        self.assertEqual(vlc.seek_positions, [42])

    async def test_public_sensitive_request_is_deleted_before_queueing(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("play")
        self.assertIsNotNone(command)
        context = FakeContext()
        private_url = (
            "https://nexus-220.cnam.tb-cdn.io/dld/example"
            "?token=private-token"
        )

        with patch.object(discord_bot, "ensure_guild_worker"):
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                url=private_url,
            )

        state = discord_bot.GUILD_STATE.pop(123)
        queued = state.queue.get_nowait()
        state.queue.task_done()
        self.assertTrue(context.message.deleted)
        self.assertEqual(queued.url, private_url)
        self.assertEqual(context.replies, [])
        self.assertIn("Resolving request", context.sent[-1])

    async def test_public_social_request_message_is_not_deleted(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("play")
        self.assertIsNotNone(command)
        context = FakeContext()

        with patch.object(discord_bot, "ensure_guild_worker"):
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                url="https://www.youtube.com/watch?v=public-video",
            )

        state = discord_bot.GUILD_STATE.pop(123)
        state.queue.get_nowait()
        state.queue.task_done()
        self.assertFalse(context.message.deleted)
        self.assertIn("Resolving request", context.replies[-1])

    async def test_play_queues_multiple_links_in_the_given_order(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("play")
        self.assertIsNotNone(command)
        context = FakeContext()
        urls = [
            "https://example.com/first",
            "https://example.com/second",
            "https://example.com/third",
        ]

        with patch.object(discord_bot, "ensure_guild_worker") as ensure_worker:
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                url=" ".join(urls),
            )

        state = discord_bot.GUILD_STATE.pop(123)
        queued = [state.queue.get_nowait() for _ in urls]
        for _ in queued:
            state.queue.task_done()
        self.assertEqual([request.url for request in queued], urls)
        self.assertEqual(
            context.replies,
            [
                "Resolving request 1/3…",
                "Queued request 2/3 at position 2.",
                "Queued request 3/3 at position 3.",
            ],
        )
        ensure_worker.assert_called_once_with(state)

    async def test_multi_play_deletes_public_message_when_any_link_is_sensitive(
        self,
    ) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("play")
        self.assertIsNotNone(command)
        context = FakeContext()
        public_url = "https://www.youtube.com/watch?v=public-video"
        private_url = "https://nexus-220.cnam.tb-cdn.io/file?token=secret"

        with patch.object(discord_bot, "ensure_guild_worker"):
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                url=f"{public_url} {private_url}",
            )

        state = discord_bot.GUILD_STATE.pop(123)
        queued = [state.queue.get_nowait(), state.queue.get_nowait()]
        for _ in queued:
            state.queue.task_done()
        self.assertTrue(context.message.deleted)
        self.assertEqual([request.url for request in queued], [public_url, private_url])
        self.assertEqual(context.replies, [])
        self.assertEqual(len(context.sent), 2)

    async def test_multi_play_rejects_the_entire_batch_when_one_link_is_invalid(
        self,
    ) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("play")
        self.assertIsNotNone(command)
        context = FakeContext()

        with patch.object(discord_bot, "ensure_guild_worker") as ensure_worker:
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                url="https://example.com/valid not-a-url",
            )

        self.assertNotIn(123, discord_bot.GUILD_STATE)
        self.assertIn("Link 2:", context.replies[-1])
        ensure_worker.assert_not_called()

    def test_sensitive_debrid_links_are_redacted_but_social_links_are_not(self) -> None:
        sensitive = "https://api.torbox.app/v1/download?token=super-secret"
        torbox_cdn = (
            "https://nexus-220.cnam.tb-cdn.io/dld/example"
            "?token=cdn-secret"
        )
        youtube = "https://www.youtube.com/watch?v=public-video"
        social = "https://www.tiktok.com/@creator/video/123"
        message = f"failed: {sensitive} {torbox_cdn} {youtube} {social}"

        redacted = discord_bot.redact_sensitive_links(message)

        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("cdn-secret", redacted)
        self.assertIn(discord_bot.REDACTED_LINK, redacted)
        self.assertIn(youtube, redacted)
        self.assertIn(social, redacted)

    def test_queue_embed_hides_debrid_credentials(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.queue.put_nowait(
            discord_bot.MediaRequest(
                url="https://download.real-debrid.com/d/secret-token/file.mkv",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
            )
        )

        embed = discord_bot.queue_embed(state)

        self.assertNotIn("secret-token", embed.description)
        self.assertIn(discord_bot.REDACTED_LINK, embed.description)

    def test_vlc_playlist_reads_items_added_outside_bot_queue(self) -> None:
        session = discord_bot.VLCSession("vlc.exe")
        with (
            patch.object(session, "is_running", return_value=True),
            patch.object(
                session,
                "status",
                return_value={"state": "playing", "currentplid": 7},
            ),
            patch.object(
                session,
                "_json_request",
                return_value={
                    "children": [
                        {
                            "name": "Playlist",
                            "children": [
                                {
                                    "id": "7",
                                    "name": "Manually opened movie",
                                    "uri": "file:///C:/private/path/movie.mkv",
                                    "duration": 300,
                                    "type": "leaf",
                                },
                                {
                                    "id": "8",
                                    "name": "Next external item",
                                    "uri": "https://example.com/media",
                                    "duration": 90,
                                    "type": "leaf",
                                },
                            ],
                        }
                    ]
                },
            ) as request,
        ):
            items = session.playlist()

        request.assert_called_once_with("playlist.json")
        self.assertEqual([item.title for item in items], [
            "Manually opened movie",
            "Next external item",
        ])
        self.assertTrue(items[0].current)
        self.assertFalse(items[1].current)

    async def test_queue_command_includes_live_vlc_playlist(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        state.vlc.playlist.return_value = [
            discord_bot.VLCPlaylistItem(
                item_id="7",
                title="Manually opened movie",
                duration=300,
                current=True,
            ),
            discord_bot.VLCPlaylistItem(
                item_id="8",
                title="Next external item",
                duration=90,
                current=False,
            ),
        ]
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("queue")
            self.assertIsNotNone(command)
            await command.callback(context)  # type: ignore[arg-type, union-attr]
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        embed = context.reply_options[-1]["embed"]
        self.assertIn("VLC playlist", embed.description)
        self.assertIn("▶ Now playing", embed.description)
        self.assertIn("Manually opened movie", embed.description)
        self.assertIn("Next external item", embed.description)

    async def test_only_the_bot_owner_can_use_dm_commands(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        command = bot.get_command("queue")
        self.assertIsNotNone(command)

        owner_context = FakeContext()
        owner_context.guild = None  # type: ignore[assignment]
        with patch.object(bot, "is_owner", new=AsyncMock(return_value=True)):
            await command.callback(owner_context)  # type: ignore[arg-type, union-attr]
        self.assertIsInstance(owner_context.reply_options[-1]["embed"], discord.Embed)

        other_context = FakeContext()
        other_context.guild = None  # type: ignore[assignment]
        with patch.object(bot, "is_owner", new=AsyncMock(return_value=False)):
            await command.callback(other_context)  # type: ignore[arg-type, union-attr]
        self.assertEqual(other_context.replies, [])

    async def test_owner_dm_automatically_targets_the_only_guild(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=None)
        command = bot.get_command("queue")
        self.assertIsNotNone(command)
        context = FakeContext()
        context.guild = None  # type: ignore[assignment]

        state = discord_bot.GuildState(guild_id=123)
        state.queue.put_nowait(
            discord_bot.MediaRequest(
                url="https://www.youtube.com/watch?v=private-request",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
            )
        )
        discord_bot.GUILD_STATE[123] = state
        try:
            with (
                patch.object(
                    type(bot),
                    "guilds",
                    new_callable=PropertyMock,
                    return_value=[FakeGuild()],
                ),
                patch.object(bot, "is_owner", new=AsyncMock(return_value=True)),
            ):
                await command.callback(context)  # type: ignore[arg-type, union-attr]
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        embed = context.reply_options[-1]["embed"]
        self.assertIsInstance(embed, discord.Embed)
        self.assertIn("private-request", embed.description)

    def test_queue_embed_stays_within_discord_limits(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        for index in range(100):
            state.queue.put_nowait(
                discord_bot.MediaRequest(
                    url=f"https://example.com/{index}/{'x' * 300}",
                    requester=FakeRequester(),  # type: ignore[arg-type]
                    status_message=FakeMessage(),  # type: ignore[arg-type]
                )
            )

        embed = discord_bot.queue_embed(state)
        self.assertIsNotNone(embed.description)
        self.assertLessEqual(
            len(embed.description),
            discord_bot.MAX_QUEUE_EMBED_DESCRIPTION,
        )
        self.assertLessEqual(len(embed), 6_000)
        self.assertIn("more requests", embed.description)

    def test_short_command_aliases_are_registered(self) -> None:
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)

        self.assertIs(bot.get_command("p"), bot.get_command("play"))
        self.assertIs(bot.get_command("s"), bot.get_command("skip"))
        self.assertIs(bot.get_command("q"), bot.get_command("queue"))
        self.assertIs(bot.get_command("clearplaylist"), bot.get_command("clear"))
        self.assertIs(bot.get_command("localqueue"), bot.get_command("local"))
        self.assertIs(bot.get_command("media"), bot.get_command("local"))

    def test_seek_positions_accept_seconds_and_clock_formats(self) -> None:
        self.assertEqual(discord_bot.parse_seek_position("90"), 90)
        self.assertEqual(discord_bot.parse_seek_position("01:30"), 90)
        self.assertEqual(discord_bot.parse_seek_position("1:02:30"), 3750)
        self.assertEqual(discord_bot.parse_seek_expression("+10"), (10, True))
        self.assertEqual(discord_bot.parse_seek_expression("+60"), (60, True))
        self.assertEqual(discord_bot.parse_seek_expression("+05:00"), (300, True))
        self.assertEqual(discord_bot.parse_seek_expression("-10"), (-10, True))
        self.assertEqual(discord_bot.parse_seek_expression("-05:00"), (-300, True))
        self.assertEqual(discord_bot.parse_seek_expression("05:00"), (300, False))

    def test_seek_positions_reject_invalid_clock_values(self) -> None:
        for value in ("", "+", "-", "1:60", "1:60:00", "1:2:3:4", "soon"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    discord_bot.parse_seek_position(value)

    def test_local_folder_media_is_recursive_filtered_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            album = root / "Album"
            album.mkdir(parents=True)
            (root / "z-last.mp4").write_bytes(b"video")
            (album / "a-first.flac").write_bytes(b"audio")
            (album / "notes.txt").write_text("ignored", encoding="utf-8")
            outside = Path(temporary) / "outside.mp4"
            outside.write_bytes(b"outside")

            with patch.object(discord_bot, "MEDIA_DIR", root):
                files = discord_bot.local_folder_media(root)
                labels = [discord_bot.local_media_label(path) for path in files]
                with self.assertRaisesRegex(ValueError, "escapes"):
                    discord_bot.resolve_local_media_path(outside, directory=False)

        self.assertEqual(
            labels,
            ["media/Album/a-first.flac", "media/z-last.mp4"],
        )

    def test_local_media_resolution_bypasses_ytdlp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            root.mkdir()
            media_file = root / "movie.mkv"
            media_file.write_bytes(b"local-video")
            request = discord_bot.MediaRequest(
                url="local:media/movie.mkv",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
                local_path=media_file,
            )

            with (
                patch.object(discord_bot, "MEDIA_DIR", root),
                patch.object(
                    discord_bot,
                    "runtime_programs",
                    return_value=("yt-dlp.exe", "vlc.exe", "deno.exe"),
                ),
                patch.object(discord_bot, "resolve_media") as resolve_remote,
            ):
                prepared = discord_bot.resolve_request_media(request)

        resolve_remote.assert_not_called()
        self.assertEqual(prepared.vlc, "vlc.exe")
        self.assertEqual(prepared.info["title"], "movie.mkv")
        self.assertTrue(prepared.info["_local_media"])

    async def test_local_command_opens_requester_scoped_media_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            root.mkdir()
            (root / "movie.mp4").write_bytes(b"video")
            (root / "ignore.txt").write_text("ignored", encoding="utf-8")
            bot = discord_bot.build_bot(
                request_channel_id=None,
                configured_guild_id=123,
            )
            context = FakeContext()

            with patch.object(discord_bot, "MEDIA_DIR", root):
                command = bot.get_command("local")
                self.assertIsNotNone(command)
                await command.callback(context)  # type: ignore[arg-type, union-attr]
                view = context.reply_options[-1]["view"]

            self.assertIsInstance(view, discord_bot.LocalMediaBrowserView)
            labels = [option.label for option in view.selector.options]
            self.assertIn("Queue this folder recursively", labels)
            self.assertIn("movie.mp4", labels)
            self.assertNotIn("ignore.txt", labels)
            self.assertEqual(view.requester.id, FakeRequester.id)
            view.stop()
            discord_bot.GUILD_STATE.pop(123, None)

    async def test_local_browser_appends_an_entire_folder_to_vlc_playlist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            folder = root / "Shows"
            folder.mkdir(parents=True)
            first = folder / "01.mkv"
            second = folder / "02.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            bot = discord_bot.build_bot(
                request_channel_id=None,
                configured_guild_id=123,
            )
            state = discord_bot.GuildState(guild_id=123)
            state.vlc = MagicMock()
            state.vlc.enqueue_local_inputs.return_value = (3, False)
            message = FakeMessage()
            interaction = SimpleNamespace(
                message=message,
                response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(),
            )

            with patch.object(discord_bot, "MEDIA_DIR", root):
                view = discord_bot.LocalMediaBrowserView(
                    bot,
                    state,
                    FakeRequester(),  # type: ignore[arg-type]
                )
                view.current = folder
                view.refresh()
                await view.handle_selection(  # type: ignore[arg-type]
                    interaction,
                    "queue-folder",
                )

            state.vlc.enqueue_local_inputs.assert_called_once_with([folder])
            self.assertEqual(state.queue.qsize(), 0)
            interaction.response.defer.assert_awaited_once_with()
            self.assertIn(
                "Appended folder containing 2 local media files to the end of VLC's playlist",
                interaction.edit_original_response.await_args.kwargs["content"],
            )

    async def test_playback_end_does_not_close_vlc_process(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]

        with (
            patch.object(
                session,
                "status",
                side_effect=[{"state": "playing"}, {"state": "stopped"}],
            ),
            patch.object(discord_bot.asyncio, "sleep", new=AsyncMock()),
        ):
            await session.wait_until_finished({"state": "stopped"})

        self.assertIsNotNone(session.process)
        self.assertIsNone(session.process.poll())

    def test_vlc_readiness_tolerates_delayed_http_startup(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        session = discord_bot.VLCSession("vlc.exe")
        session.process = process

        with (
            patch.object(
                session,
                "status",
                side_effect=[ConnectionRefusedError(), {"state": "stopped"}],
            ) as status,
            patch.object(discord_bot.time, "sleep") as sleep,
        ):
            session._wait_until_ready()

        self.assertEqual(status.call_count, 2)
        sleep.assert_called_once_with(discord_bot.VLC_START_POLL_INTERVAL)
        process.terminate.assert_not_called()

    def test_vlc_launch_uses_directsound_for_discord_capture(self) -> None:
        process = MagicMock()
        process.pid = 1234
        with (
            patch.dict(
                discord_bot.os.environ,
                {"VLC_AUDIO_OUTPUT": "directsound"},
            ),
            patch.object(
                discord_bot.VLCSession,
                "_available_port",
                return_value=4567,
            ),
            patch.object(
                discord_bot.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            session = discord_bot.VLCSession("vlc.exe")
            session._launch()

        command = popen.call_args.args[0]
        self.assertIn("--aout=directsound", command)

    def test_vlc_launch_resolves_and_pins_mmdevice_output(self) -> None:
        process = MagicMock()
        process.pid = 1234
        endpoint_id = "{0.0.0.00000000}.{CABLE-ENDPOINT}"
        with (
            patch.dict(
                discord_bot.os.environ,
                {
                    "VLC_AUDIO_OUTPUT": "mmdevice",
                    "VLC_AUDIO_DEVICE": "CABLE Input",
                },
            ),
            patch.object(
                yt_vlc,
                "windows_render_endpoints",
                return_value=[(endpoint_id, "CABLE Input")],
            ),
            patch.object(
                discord_bot.VLCSession,
                "_available_port",
                return_value=4567,
            ),
            patch.object(
                discord_bot.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            session = discord_bot.VLCSession("vlc.exe")
            session._launch()

        command = popen.call_args.args[0]
        self.assertIn("--aout=mmdevice", command)
        self.assertIn(f"--mmdevice-audio-device={endpoint_id}", command)

    def test_mmdevice_output_never_silently_falls_back(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "is not active"):
            yt_vlc.resolve_mmdevice_audio_device(
                "CABLE Input",
                endpoints=[
                    ("{0.0.0.00000000}.{SPEAKERS}", "Speakers"),
                ],
            )

    def test_mmdevice_friendly_name_resolves_case_insensitively(self) -> None:
        endpoint_id = "{0.0.0.00000000}.{CABLE-ENDPOINT}"
        self.assertEqual(
            yt_vlc.resolve_mmdevice_audio_device(
                "cable input",
                endpoints=[(endpoint_id, "CABLE Input")],
            ),
            endpoint_id,
        )

    def test_automatic_vlc_audio_output_omits_module_override(self) -> None:
        with patch.dict(
            discord_bot.os.environ,
            {"VLC_AUDIO_OUTPUT": "automatic"},
        ):
            self.assertIsNone(discord_bot.configured_vlc_audio_output())

    def test_invalid_vlc_audio_output_is_rejected(self) -> None:
        with patch.dict(
            discord_bot.os.environ,
            {"VLC_AUDIO_OUTPUT": "not-a-module"},
        ):
            with self.assertRaisesRegex(RuntimeError, "VLC_AUDIO_OUTPUT"):
                discord_bot.configured_vlc_audio_output()

    def test_vlc_initialization_relaunches_failed_controller(self) -> None:
        first_process = MagicMock()
        first_process.poll.return_value = None
        second_process = MagicMock()
        second_process.poll.return_value = None
        processes = iter((first_process, second_process))
        session = discord_bot.VLCSession("vlc.exe")

        def fake_launch() -> None:
            session.process = next(processes)

        with (
            patch.object(session, "_launch", side_effect=fake_launch) as launch,
            patch.object(
                session,
                "_wait_until_ready",
                side_effect=[RuntimeError("controller unavailable"), None],
            ),
        ):
            session.ensure_started()

        self.assertEqual(launch.call_count, 2)
        first_process.terminate.assert_called_once_with()
        first_process.wait.assert_called_once_with(
            timeout=discord_bot.VLC_SHUTDOWN_TIMEOUT
        )
        second_process.terminate.assert_not_called()
        self.assertIs(session.process, second_process)

    def test_separate_audio_is_sent_as_an_input_slave(self) -> None:
        session = discord_bot.VLCSession("vlc.exe")
        media = discord_bot.PreparedMedia(
            vlc="vlc.exe",
            streams=["https://example.com/video", "https://example.com/audio"],
            info={},
        )

        with (
            patch.object(session, "ensure_started"),
            patch.object(
                session,
                "_request",
                side_effect=[{"state": "stopped"}, {"state": "stopped"}],
            ) as request,
        ):
            session.play(media)

        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "pl_empty"}),
                call(
                    {
                        "command": "in_play",
                        "input": "https://example.com/video",
                        "option": "input-slave=https://example.com/audio",
                    }
                ),
            ],
        )

    def test_vlc_playback_controls_do_not_close_the_process(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]
        with patch.object(
            session,
            "_request",
            side_effect=[
                {"state": "paused"},
                {"state": "playing"},
                {"state": "stopped"},
                {"state": "playing"},
                {"state": "stopped"},
            ],
        ) as request:
            session.pause()
            session.resume()
            session.stop()
            session.advance()
            session.clear_playlist()

        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "pl_forcepause"}),
                call({"command": "pl_forceresume"}),
                call({"command": "pl_stop"}),
                call({"command": "pl_next"}),
                call({"command": "pl_empty"}),
            ],
        )
        self.assertIsNone(session.process.poll())

    def test_local_files_append_to_existing_vlc_playlist_without_clearing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            root.mkdir()
            first = root / "01.mkv"
            second = root / "02.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            session = discord_bot.VLCSession("vlc.exe")

            with (
                patch.object(discord_bot, "MEDIA_DIR", root),
                patch.object(session, "ensure_started"),
                patch.object(
                    session,
                    "playlist",
                    return_value=[
                        discord_bot.VLCPlaylistItem("7", "Active", 300, True)
                    ],
                ),
                patch.object(session, "status", return_value={"state": "playing"}),
                patch.object(session, "_request", return_value={}) as request,
            ):
                existing, started = session.enqueue_local_inputs([first, second])

        self.assertEqual(existing, 1)
        self.assertFalse(started)
        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "in_enqueue", "input": str(first)}),
                call({"command": "in_enqueue", "input": str(second)}),
            ],
        )
        self.assertNotIn(call({"command": "pl_empty"}), request.call_args_list)

    def test_vlc_request_percent_encodes_local_path_spaces(self) -> None:
        session = discord_bot.VLCSession("vlc.exe")
        local_path = r"C:\media\Show + Extras\Episode 01.mkv"

        with patch.object(
            discord_bot,
            "urlopen",
            return_value=io.BytesIO(b"{}"),
        ) as open_url:
            session._request({"command": "in_enqueue", "input": local_path})

        request = open_url.call_args.args[0]
        self.assertIn("Show%20%2B%20Extras", request.full_url)
        self.assertIn("Episode%2001.mkv", request.full_url)
        self.assertNotIn("Show+%2B+Extras", request.full_url)

    def test_local_files_explicitly_start_vlc_when_playlist_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            root.mkdir()
            first = root / "01.mkv"
            second = root / "02.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            session = discord_bot.VLCSession("vlc.exe")

            with (
                patch.object(discord_bot, "MEDIA_DIR", root),
                patch.object(session, "ensure_started"),
                patch.object(
                    session,
                    "playlist",
                    side_effect=[
                        [],
                        [
                            discord_bot.VLCPlaylistItem(
                                "8", "First", 300, False
                            ),
                            discord_bot.VLCPlaylistItem(
                                "9", "Second", 300, False
                            ),
                        ],
                    ],
                ),
                patch.object(session, "status", return_value={"state": "stopped"}),
                patch.object(session, "_request", return_value={}) as request,
            ):
                existing, started = session.enqueue_local_inputs([first, second])

        self.assertEqual(existing, 0)
        self.assertTrue(started)
        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "in_enqueue", "input": str(first)}),
                call({"command": "in_enqueue", "input": str(second)}),
                call({"command": "pl_play", "id": "8"}),
            ],
        )

    def test_local_file_starts_when_existing_vlc_playlist_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            root.mkdir()
            local_file = root / "episode.mkv"
            local_file.write_bytes(b"episode")
            old_item = discord_bot.VLCPlaylistItem("4", "Old", 300, True)
            new_item = discord_bot.VLCPlaylistItem("5", "Episode", 300, False)
            session = discord_bot.VLCSession("vlc.exe")

            with (
                patch.object(discord_bot, "MEDIA_DIR", root),
                patch.object(session, "ensure_started"),
                patch.object(
                    session,
                    "playlist",
                    side_effect=[[old_item], [old_item, new_item]],
                ),
                patch.object(session, "status", return_value={"state": "stopped"}),
                patch.object(session, "_request", return_value={}) as request,
            ):
                existing, started = session.enqueue_local_inputs([local_file])

        self.assertEqual(existing, 1)
        self.assertTrue(started)
        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "in_enqueue", "input": str(local_file)}),
                call({"command": "pl_play", "id": "5"}),
            ],
        )

    def test_vlc_seek_uses_absolute_seconds_without_closing_player(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]
        with patch.object(
            session,
            "_request",
            side_effect=[
                {"state": "playing", "time": 20, "length": 300},
                {"state": "playing", "time": 90, "length": 300},
            ],
        ) as request:
            result = session.seek_when_ready(90)

        self.assertEqual(result["time"], 90)
        self.assertEqual(
            request.call_args_list,
            [call(), call({"command": "seek", "val": "90S"})],
        )
        self.assertIsNone(session.process.poll())

    def test_vlc_relative_seek_adds_to_current_playback_time(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]
        with patch.object(
            session,
            "_request",
            side_effect=[
                {"state": "playing", "time": 75, "length": 300},
                {"state": "playing", "time": 135, "length": 300},
            ],
        ) as request:
            target = session.seek_relative_when_ready(60)

        self.assertEqual(target, 135)
        self.assertEqual(
            request.call_args_list,
            [call(), call({"command": "seek", "val": "135S"})],
        )

    def test_vlc_relative_seek_rewinds_and_clamps_at_start(self) -> None:
        class RunningProcess:
            returncode = None

            def poll(self) -> None:
                return None

        session = discord_bot.VLCSession("vlc.exe")
        session.process = RunningProcess()  # type: ignore[assignment]
        with patch.object(
            session,
            "_request",
            side_effect=[
                {"state": "playing", "time": 20, "length": 300},
                {"state": "playing", "time": 0, "length": 300},
            ],
        ) as request:
            target = session.seek_relative_when_ready(-60)

        self.assertEqual(target, 0)
        self.assertEqual(
            request.call_args_list,
            [call(), call({"command": "seek", "val": "0S"})],
        )

    async def test_pause_and_resume_control_local_or_manual_vlc_media(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        pause_context = FakeContext()
        resume_context = FakeContext()

        try:
            pause = bot.get_command("pause")
            resume = bot.get_command("resume")
            self.assertIsNotNone(pause)
            self.assertIsNotNone(resume)
            await pause.callback(pause_context)  # type: ignore[arg-type, union-attr]
            await resume.callback(resume_context)  # type: ignore[arg-type, union-attr]
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        state.vlc.pause.assert_called_once_with()
        state.vlc.resume.assert_called_once_with()
        self.assertEqual(pause_context.replies[-1], "Playback paused.")
        self.assertEqual(resume_context.replies[-1], "Playback resumed.")

    async def test_seek_command_moves_current_playback(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.current = discord_bot.MediaRequest(
            url="https://example.com/current",
            requester=FakeRequester(),  # type: ignore[arg-type]
            status_message=FakeMessage(),  # type: ignore[arg-type]
        )
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        state.vlc.seek_when_ready.return_value = {
            "state": "playing",
            "time": 90,
            "length": 300,
        }
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("seek")
            self.assertIsNotNone(command)
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                position="01:30",
            )
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        state.vlc.seek_when_ready.assert_called_once_with(90)
        self.assertEqual(context.replies[-1], "Playback moved to `1:30`.")

    async def test_seek_command_controls_manually_added_vlc_media(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        state.vlc.seek_relative_when_ready.return_value = 135
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("seek")
            self.assertIsNotNone(command)
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                position="+60",
            )
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        state.vlc.seek_relative_when_ready.assert_called_once_with(60)
        state.vlc.seek_when_ready.assert_not_called()
        self.assertEqual(
            context.replies[-1],
            "Playback advanced by `1:00` to `2:15`.",
        )

    async def test_seek_command_minus_modifier_rewinds_playback(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        state.vlc.seek_relative_when_ready.return_value = 75
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("seek")
            self.assertIsNotNone(command)
            await command.callback(  # type: ignore[arg-type, union-attr]
                context,
                position="-60",
            )
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        state.vlc.seek_relative_when_ready.assert_called_once_with(-60)
        self.assertEqual(
            context.replies[-1],
            "Playback rewound by `1:00` to `1:15`.",
        )

    async def test_stop_clears_pending_requests_and_keeps_vlc(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        first_finished = asyncio.Event()
        second_finished = asyncio.Event()
        vlc = FakeVLCSession("vlc.exe", first_finished, second_finished)
        state.vlc = vlc  # type: ignore[assignment]
        state.current = discord_bot.MediaRequest(
            url="https://example.com/current",
            requester=FakeRequester(),  # type: ignore[arg-type]
            status_message=FakeMessage(),  # type: ignore[arg-type]
        )
        pending_message = FakeMessage()
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/pending",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=pending_message,  # type: ignore[arg-type]
            )
        )
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("stop")
            self.assertIsNotNone(command)
            await command.callback(context)  # type: ignore[arg-type, union-attr]
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        self.assertEqual(vlc.stop_calls, 1)
        self.assertEqual(state.queue.qsize(), 0)
        self.assertEqual(state.completion_note, "Stopped by requester")
        self.assertTrue(state.cancel_current)
        self.assertIn("VLC remains open", context.replies[-1])
        self.assertIn("Removed from the queue", pending_message.edits[-1]["content"])

    async def test_clear_removes_vlc_playlist_and_all_bot_requests(self) -> None:
        state = discord_bot.GuildState(guild_id=123)
        current_message = FakeMessage()
        pending_message = FakeMessage()
        state.current = discord_bot.MediaRequest(
            url="https://example.com/current",
            requester=FakeRequester(),  # type: ignore[arg-type]
            status_message=current_message,  # type: ignore[arg-type]
        )
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/pending",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=pending_message,  # type: ignore[arg-type]
            )
        )
        state.vlc = MagicMock()
        state.vlc.is_running.return_value = True
        state.vlc.playlist.return_value = [
            discord_bot.VLCPlaylistItem("7", "Current", 300, True),
            discord_bot.VLCPlaylistItem("8", "Manual item", 90, False),
        ]
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        try:
            command = bot.get_command("clear")
            self.assertIsNotNone(command)
            await command.callback(context)  # type: ignore[arg-type, union-attr]
        finally:
            discord_bot.GUILD_STATE.pop(123, None)

        state.vlc.clear_playlist.assert_called_once_with()
        self.assertEqual(state.queue.qsize(), 0)
        self.assertTrue(state.cancel_current)
        self.assertEqual(state.completion_note, "Playlist cleared by requester")
        self.assertIn("Removed from the queue", pending_message.edits[-1]["content"])
        self.assertIn("removed 2 VLC items", context.replies[-1])
        self.assertIn("cleared 2 bot requests", context.replies[-1])
        self.assertIn("VLC remains open", context.replies[-1])

    async def test_skip_during_resolution_never_opens_the_media(self) -> None:
        resolution_started = asyncio.Event()
        allow_resolution = threading.Event()
        loop = asyncio.get_running_loop()

        def fake_resolve(url: str) -> discord_bot.PreparedMedia:
            loop.call_soon_threadsafe(resolution_started.set)
            allow_resolution.wait(timeout=1)
            return discord_bot.PreparedMedia(
                vlc="vlc.exe",
                streams=[url],
                info={"title": url},
            )

        message = FakeMessage()
        state = discord_bot.GuildState(guild_id=123)
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/resolving",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=message,  # type: ignore[arg-type]
            )
        )
        discord_bot.GUILD_STATE[123] = state
        bot = discord_bot.build_bot(request_channel_id=None, configured_guild_id=123)
        context = FakeContext()

        with patch.object(discord_bot, "resolve_media", side_effect=fake_resolve):
            worker = asyncio.create_task(discord_bot.run_guild_queue(state))
            try:
                await asyncio.wait_for(resolution_started.wait(), timeout=1)
                command = bot.get_command("skip")
                self.assertIsNotNone(command)
                await command.callback(context)  # type: ignore[arg-type, union-attr]
                allow_resolution.set()
                await asyncio.wait_for(state.queue.join(), timeout=1)
            finally:
                allow_resolution.set()
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                discord_bot.GUILD_STATE.pop(123, None)

        self.assertIsNone(state.vlc)
        self.assertIn("Skipped by requester", message.edits[-1]["content"])

    async def test_next_url_is_not_resolved_until_playback_finishes(self) -> None:
        first_finished = asyncio.Event()
        second_finished = asyncio.Event()
        first_resolved = asyncio.Event()
        second_resolved = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_resolve(url: str) -> discord_bot.PreparedMedia:
            event = first_resolved if url == "https://example.com/first" else second_resolved
            loop.call_soon_threadsafe(event.set)
            return discord_bot.PreparedMedia(
                vlc="vlc.exe",
                streams=[url],
                info={"title": url},
            )

        state = discord_bot.GuildState(guild_id=123)
        state.vlc = FakeVLCSession(  # type: ignore[assignment]
            "vlc.exe",
            first_finished,
            second_finished,
        )
        first = discord_bot.MediaRequest(
            url="https://example.com/first",
            requester=FakeRequester(),  # type: ignore[arg-type]
            status_message=FakeMessage(),  # type: ignore[arg-type]
        )
        second = discord_bot.MediaRequest(
            url="https://example.com/second",
            requester=FakeRequester(),  # type: ignore[arg-type]
            status_message=FakeMessage(),  # type: ignore[arg-type]
        )
        await state.queue.put(first)
        await state.queue.put(second)

        with (
            patch.object(discord_bot, "resolve_media", side_effect=fake_resolve),
            patch.object(
                discord_bot,
                "now_playing_embed",
                return_value=discord.Embed(title="Now playing"),
            ),
        ):
            worker = asyncio.create_task(discord_bot.run_guild_queue(state))
            try:
                await asyncio.wait_for(first_resolved.wait(), timeout=1)
                await asyncio.sleep(0)
                self.assertFalse(second_resolved.is_set())

                first_finished.set()
                await asyncio.wait_for(second_resolved.wait(), timeout=1)
                second_finished.set()
                await asyncio.wait_for(state.queue.join(), timeout=1)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    async def test_next_url_is_prefetched_only_when_playback_nears_end(self) -> None:
        first_finished = asyncio.Event()
        second_finished = asyncio.Event()
        first_resolved = asyncio.Event()
        second_resolved = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_resolve(url: str) -> discord_bot.PreparedMedia:
            event = first_resolved if url.endswith("first") else second_resolved
            loop.call_soon_threadsafe(event.set)
            return discord_bot.PreparedMedia(
                vlc="vlc.exe",
                streams=[url],
                info={"title": url},
            )

        state = discord_bot.GuildState(guild_id=123)
        vlc = FakeVLCSession(
            "vlc.exe",
            first_finished,
            second_finished,
            trigger_prefetch=True,
        )
        state.vlc = vlc  # type: ignore[assignment]
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/first",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
            )
        )
        await state.queue.put(
            discord_bot.MediaRequest(
                url="https://example.com/second",
                requester=FakeRequester(),  # type: ignore[arg-type]
                status_message=FakeMessage(),  # type: ignore[arg-type]
            )
        )

        with (
            patch.object(discord_bot, "resolve_media", side_effect=fake_resolve),
            patch.object(
                discord_bot,
                "now_playing_embed",
                return_value=discord.Embed(title="Now playing"),
            ),
        ):
            worker = asyncio.create_task(discord_bot.run_guild_queue(state))
            try:
                await asyncio.wait_for(first_resolved.wait(), timeout=1)
                await asyncio.wait_for(second_resolved.wait(), timeout=1)
                self.assertEqual(vlc.played, ["https://example.com/first"])

                first_finished.set()
                for _ in range(20):
                    if len(vlc.played) == 2:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(vlc.played[-1], "https://example.com/second")
                second_finished.set()
                await asyncio.wait_for(state.queue.join(), timeout=1)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker


if __name__ == "__main__":
    unittest.main()
