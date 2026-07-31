from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, call, patch

import discord

import discord_bot


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


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

    async def reply(self, content: str = "", **_: object) -> None:
        self.replies.append(content)


class FakeVLCSession:
    def __init__(
        self,
        executable: str,
        first_finished: asyncio.Event,
        second_finished: asyncio.Event,
    ) -> None:
        self.executable = executable
        self.first_finished = first_finished
        self.second_finished = second_finished
        self.played: list[str] = []
        self.stop_calls = 0

    def play(self, media: discord_bot.PreparedMedia) -> dict[str, object]:
        self.played.append(media.streams[0])
        return {"state": "playing"}

    async def wait_until_finished(self, _: dict[str, object]) -> None:
        current = self.played[-1]
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


if __name__ == "__main__":
    unittest.main()
