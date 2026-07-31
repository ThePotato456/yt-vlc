from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, PropertyMock, call, patch

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
            ],
        ) as request:
            session.pause()
            session.resume()
            session.stop()

        self.assertEqual(
            request.call_args_list,
            [
                call({"command": "pl_forcepause"}),
                call({"command": "pl_forceresume"}),
                call({"command": "pl_stop"}),
            ],
        )
        self.assertIsNone(session.process.poll())

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
