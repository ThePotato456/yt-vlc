from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

import client_bridge
import discord_bot


class FakeResponse:
    def __init__(self, status: int, body: dict[str, object]) -> None:
        self.status = status
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


def configured_client(token: str = "secret-token") -> client_bridge.ClientBridge:
    return client_bridge.ClientBridge(
        client_bridge.ClientBridgeConfig(
            api_url="http://127.0.0.1:38423",
            token=token,
            voice_channel_id=234567890123456789,
        )
    )


class ClientBridgeTests(unittest.TestCase):
    def test_configuration_is_optional_when_all_values_are_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(client_bridge.load_client_bridge_config())

    def test_example_url_alone_does_not_enable_the_bridge(self) -> None:
        with patch.dict(
            os.environ,
            {"DISCORD_CLIENT_API_URL": client_bridge.DEFAULT_API_URL},
            clear=True,
        ):
            self.assertIsNone(client_bridge.load_client_bridge_config())

    def test_configuration_requires_token_and_channel_together(self) -> None:
        with patch.dict(
            os.environ,
            {"DISCORD_CLIENT_API_TOKEN": "s" * 32},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "must both be set"):
                client_bridge.load_client_bridge_config()

    def test_configuration_rejects_non_loopback_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_CLIENT_API_URL": "https://example.com:38423",
                "DISCORD_CLIENT_API_TOKEN": "s" * 32,
                "DISCORD_VOICE_CHANNEL_ID": "234567890123456789",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                client_bridge.load_client_bridge_config()

    def test_configuration_rejects_other_addresses_in_loopback_block(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_CLIENT_API_URL": "http://127.0.0.2:38423",
                "DISCORD_CLIENT_API_TOKEN": "s" * 32,
                "DISCORD_VOICE_CHANNEL_ID": "234567890123456789",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "127.0.0.1"):
                client_bridge.load_client_bridge_config()

    def test_configuration_rejects_invalid_token_length(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_CLIENT_API_TOKEN": "too-short",
                "DISCORD_VOICE_CHANNEL_ID": "234567890123456789",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "32 to 256"):
                client_bridge.load_client_bridge_config()

    def test_session_request_authenticates_and_sends_exact_vlc_identity(self) -> None:
        bridge = configured_client()
        captured = {}

        def open_request(request: object, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(200, {"ok": True, "data": {"stream": {"active": True}}})

        with patch.object(client_bridge, "urlopen", side_effect=open_request):
            result = bridge.ensure_session(
                guild_id=123456789012345678,
                vlc_pid=4321,
                vlc_executable=Path("vlc.exe"),
            )

        request = captured["request"]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        payload = json.loads(request.data)
        self.assertEqual(payload["stream"]["pid"], 4321)
        self.assertTrue(payload["stream"]["executable_path"].endswith("vlc.exe"))
        self.assertEqual(payload["stream"]["resolution"], 720)
        self.assertEqual(payload["stream"]["fps"], 30)
        self.assertTrue(payload["stream"]["audio"])
        self.assertTrue(payload["self_mute"])
        self.assertTrue(payload["self_deaf"])
        self.assertTrue(result["ok"])

    def test_disconnect_stops_stream_before_leaving_voice(self) -> None:
        bridge = configured_client()
        requests = []

        def open_request(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            return FakeResponse(200, {"ok": True, "data": {}})

        with patch.object(client_bridge, "urlopen", side_effect=open_request):
            result = bridge.disconnect_session()

        self.assertEqual(
            [request.get_method() for request in requests],
            ["DELETE", "DELETE"],
        )
        self.assertTrue(requests[0].full_url.endswith("/v1/stream"))
        self.assertTrue(requests[1].full_url.endswith("/v1/voice"))
        self.assertTrue(result["ok"])

    def test_disconnect_still_leaves_voice_when_stream_cleanup_fails(self) -> None:
        bridge = configured_client()
        body = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "stream_stop_timeout",
                    "message": "Discord did not confirm stopping the stream",
                    "retryable": True,
                },
            }
        ).encode()
        stream_failure = HTTPError("url", 504, "Timeout", {}, io.BytesIO(body))
        voice_success = FakeResponse(200, {"ok": True, "data": {}})

        with patch.object(
            client_bridge,
            "urlopen",
            side_effect=[stream_failure, voice_success],
        ) as open_request:
            result = bridge.disconnect_session()

        self.assertEqual(open_request.call_count, 2)
        leave_request = open_request.call_args_list[1].args[0]
        self.assertEqual(leave_request.get_method(), "DELETE")
        self.assertTrue(leave_request.full_url.endswith("/v1/voice"))
        self.assertTrue(result["ok"])

    def test_http_error_is_structured_and_token_is_redacted(self) -> None:
        token = "never-show-this-token"
        bridge = configured_client(token)
        body = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "unauthorized",
                    "message": f"bad token {token}",
                    "retryable": False,
                },
            }
        ).encode()
        failure = HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(body))
        with patch.object(client_bridge, "urlopen", side_effect=failure):
            with self.assertRaises(client_bridge.ClientBridgeError) as raised:
                bridge.ensure_session(
                    guild_id=123456789012345678,
                    vlc_pid=4321,
                    vlc_executable="vlc.exe",
                )
        self.assertEqual(raised.exception.code, "unauthorized")
        self.assertNotIn(token, str(raised.exception))
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn(token, repr(bridge))

    def test_invalid_json_response_is_sanitized(self) -> None:
        bridge = configured_client()
        response = FakeResponse(200, {"ok": True})
        response.body = b"not-json"
        with patch.object(client_bridge, "urlopen", return_value=response):
            with self.assertRaisesRegex(client_bridge.ClientBridgeError, "invalid JSON"):
                bridge.ensure_session(
                    guild_id=123456789012345678,
                    vlc_pid=4321,
                    vlc_executable="vlc.exe",
                )

    def test_success_response_requires_explicit_ok_true(self) -> None:
        bridge = configured_client()
        response = FakeResponse(200, {"data": {}})
        with patch.object(client_bridge, "urlopen", return_value=response):
            with self.assertRaises(client_bridge.ClientBridgeError) as raised:
                bridge.ensure_session(
                    guild_id=123456789012345678,
                    vlc_pid=4321,
                    vlc_executable="vlc.exe",
                )
        self.assertEqual(raised.exception.code, "request_failed")


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class FakeBridge:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[dict[str, object]] = []
        self.disconnect_calls = 0

    def ensure_session(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise client_bridge.ClientBridgeError("bridge_unavailable", "unavailable")
        return {"ok": True}

    def disconnect_session(self) -> dict[str, object]:
        self.disconnect_calls += 1
        return {"ok": True}


class SessionSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self, bridge: FakeBridge, pid: int = 100) -> discord_bot.GuildState:
        state = discord_bot.GuildState(guild_id=123456789012345678)
        state.client_bridge = bridge  # type: ignore[assignment]
        state.vlc = SimpleNamespace(
            executable=str(Path("vlc.exe").resolve()),
            process=FakeProcess(pid),
        )
        return state

    async def test_retry_sequence_reaches_success(self) -> None:
        bridge = FakeBridge(failures=4)
        state = self.make_state(bridge)
        sleeps: list[float] = []

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        with patch.object(discord_bot.asyncio, "sleep", side_effect=no_wait):
            discord_bot.schedule_client_session(state)
            assert state.client_bridge_task is not None
            await state.client_bridge_task

        self.assertEqual(sleeps, [1.0, 2.0, 5.0, 10.0])
        self.assertEqual(len(bridge.calls), 5)
        self.assertEqual(state.client_bridge_confirmed_pid, 100)

    async def test_nonretryable_failure_stops_automatic_setup(self) -> None:
        bridge = FakeBridge()
        state = self.make_state(bridge)
        bridge.ensure_session = unittest.mock.MagicMock(
            side_effect=client_bridge.ClientBridgeError(
                "unauthorized",
                "Bearer authentication is required",
                retryable=False,
            )
        )

        with patch.object(discord_bot.asyncio, "sleep") as sleep:
            discord_bot.schedule_client_session(state)
            assert state.client_bridge_task is not None
            await state.client_bridge_task

        bridge.ensure_session.assert_called_once()
        sleep.assert_not_called()
        self.assertIsNone(state.client_bridge_confirmed_pid)
        self.assertFalse(state.client_bridge_session_enabled)

        discord_bot.schedule_client_session(state)
        await asyncio.sleep(0)
        bridge.ensure_session.assert_called_once()

    async def test_duplicate_schedule_is_suppressed_after_confirmation(self) -> None:
        bridge = FakeBridge()
        state = self.make_state(bridge)
        discord_bot.schedule_client_session(state)
        assert state.client_bridge_task is not None
        await state.client_bridge_task
        discord_bot.schedule_client_session(state)
        await asyncio.sleep(0)
        self.assertEqual(len(bridge.calls), 1)

    async def test_new_vlc_pid_reissues_session_request(self) -> None:
        bridge = FakeBridge()
        state = self.make_state(bridge, pid=100)
        discord_bot.schedule_client_session(state)
        assert state.client_bridge_task is not None
        await state.client_bridge_task

        state.vlc.process = FakeProcess(200)  # type: ignore[union-attr]
        discord_bot.schedule_client_session(state)
        assert state.client_bridge_task is not None
        await state.client_bridge_task

        self.assertEqual([call["vlc_pid"] for call in bridge.calls], [100, 200])
        self.assertEqual(state.client_bridge_confirmed_pid, 200)

    async def test_force_session_reconnects_an_already_confirmed_pid(self) -> None:
        bridge = FakeBridge()
        state = self.make_state(bridge)
        state.client_bridge_confirmed_pid = 100
        state.client_bridge_session_enabled = False

        pid = await discord_bot.force_client_session(state)

        self.assertEqual(pid, 100)
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(state.client_bridge_confirmed_pid, 100)
        self.assertTrue(state.client_bridge_session_enabled)

    async def test_failed_force_session_restores_disabled_state(self) -> None:
        bridge = FakeBridge(failures=1)
        state = self.make_state(bridge)
        state.client_bridge_session_enabled = False

        with self.assertRaises(client_bridge.ClientBridgeError):
            await discord_bot.force_client_session(state)

        self.assertFalse(state.client_bridge_session_enabled)
        self.assertIsNone(state.client_bridge_target_pid)

    async def test_disconnect_clears_confirmation_without_stopping_vlc(self) -> None:
        bridge = FakeBridge()
        state = self.make_state(bridge)
        state.client_bridge_confirmed_pid = 100

        await discord_bot.disconnect_client_session(state)

        self.assertEqual(bridge.disconnect_calls, 1)
        self.assertIsNone(state.client_bridge_confirmed_pid)
        self.assertFalse(state.client_bridge_session_enabled)
        self.assertEqual(discord_bot.active_vlc_process(state)[0], 100)

        state.vlc.process = FakeProcess(200)  # type: ignore[union-attr]
        discord_bot.schedule_client_session(state)
        await asyncio.sleep(0)
        self.assertEqual(bridge.calls, [])

    async def test_ready_does_not_wait_for_bridge_request(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingBridge(FakeBridge):
            def ensure_session(self, **kwargs: object) -> dict[str, object]:
                entered.set()
                release.wait(timeout=5)
                return super().ensure_session(**kwargs)

        bridge = BlockingBridge()
        bot = discord_bot.build_bot(None, 123456789012345678, bridge)  # type: ignore[arg-type]
        guild = SimpleNamespace(id=123456789012345678)
        state = self.make_state(bridge)
        discord_bot.GUILD_STATE[state.guild_id] = state

        async def warmed(_: discord_bot.GuildState) -> None:
            return None

        with (
            patch.object(type(bot), "user", new_callable=unittest.mock.PropertyMock, return_value="bot"),
            patch.object(bot, "get_guild", return_value=guild),
            patch.object(discord_bot, "warm_up_vlc", side_effect=warmed),
        ):
            await asyncio.wait_for(bot.on_ready(), timeout=0.5)
            await asyncio.to_thread(entered.wait, 1)

        self.assertTrue(entered.is_set())
        release.set()
        if state.client_bridge_task is not None:
            await state.client_bridge_task
        discord_bot.GUILD_STATE.pop(state.guild_id, None)

    async def test_ready_infers_guild_from_configured_voice_channel(self) -> None:
        config = client_bridge.ClientBridgeConfig(
            api_url=client_bridge.DEFAULT_API_URL,
            token="secret-token",
            voice_channel_id=234567890123456789,
        )
        bridge = client_bridge.ClientBridge(config)
        bot = discord_bot.build_bot(None, None, bridge)
        guild = SimpleNamespace(id=123456789012345678)
        channel = SimpleNamespace(id=config.voice_channel_id, guild=guild)
        state = discord_bot.GuildState(guild_id=guild.id)
        discord_bot.GUILD_STATE[guild.id] = state

        with (
            patch.object(type(bot), "user", new_callable=unittest.mock.PropertyMock, return_value="bot"),
            patch.object(type(bot), "guilds", new_callable=unittest.mock.PropertyMock, return_value=[guild, SimpleNamespace(id=999)]),
            patch.object(bot, "get_channel", return_value=channel),
            patch.object(bot, "get_guild", return_value=guild),
            patch.object(discord_bot, "warm_up_vlc", new=AsyncMock()) as warmup,
            patch.object(discord_bot, "schedule_client_session") as schedule,
        ):
            await bot.on_ready()

        warmup.assert_awaited_once_with(state)
        schedule.assert_called_once_with(state)
        self.assertIs(state.client_bridge, bridge)
        discord_bot.GUILD_STATE.pop(guild.id, None)
