from __future__ import annotations

import asyncio
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


class GuildQueueTests(unittest.IsolatedAsyncioTestCase):
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
