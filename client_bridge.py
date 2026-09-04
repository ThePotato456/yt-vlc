"""Authenticated localhost client for the Discord Canary Vencord bridge."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://127.0.0.1:38423"
MAX_RESPONSE_BYTES = 64 * 1024


class ClientBridgeError(RuntimeError):
    """A sanitized failure returned by, or while reaching, the client bridge."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ClientBridgeConfig:
    api_url: str
    token: str
    voice_channel_id: int


def _validated_api_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError(
            "DISCORD_CLIENT_API_URL must be an http:// loopback URL with a port"
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
        or port is None
    ):
        raise RuntimeError(
            "DISCORD_CLIENT_API_URL must be an http:// loopback URL with a port"
        )
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise RuntimeError(
            "DISCORD_CLIENT_API_URL must use a numeric loopback address"
        ) from error
    if address != ipaddress.ip_address("127.0.0.1"):
        raise RuntimeError("DISCORD_CLIENT_API_URL must use 127.0.0.1")
    return value.rstrip("/")


def load_client_bridge_config() -> ClientBridgeConfig | None:
    """Load the optional bridge configuration without exposing its token."""
    api_url = os.environ.get("DISCORD_CLIENT_API_URL", "").strip()
    token = os.environ.get("DISCORD_CLIENT_API_TOKEN", "").strip()
    channel = os.environ.get("DISCORD_VOICE_CHANNEL_ID", "").strip()
    if not token and not channel:
        return None
    if not token or not channel:
        raise RuntimeError(
            "DISCORD_CLIENT_API_TOKEN and DISCORD_VOICE_CHANNEL_ID must both be set"
        )
    if not 32 <= len(token) <= 256:
        raise RuntimeError("DISCORD_CLIENT_API_TOKEN must be 32 to 256 characters")
    try:
        channel_id = int(channel)
    except ValueError as error:
        raise RuntimeError("DISCORD_VOICE_CHANNEL_ID must be a Discord snowflake") from error
    if channel_id <= 0 or str(channel_id) != channel:
        raise RuntimeError("DISCORD_VOICE_CHANNEL_ID must be a Discord snowflake")
    return ClientBridgeConfig(
        api_url=_validated_api_url(api_url or DEFAULT_API_URL),
        token=token,
        voice_channel_id=channel_id,
    )


class ClientBridge:
    """Small synchronous REST client intended to run through ``asyncio.to_thread``."""

    def __init__(self, config: ClientBridgeConfig, *, timeout: float = 50.0) -> None:
        self.api_url = config.api_url
        self._token = config.token
        self.voice_channel_id = config.voice_channel_id
        self.timeout = timeout

    def __repr__(self) -> str:
        return (
            f"ClientBridge(api_url={self.api_url!r}, token='[redacted]', "
            f"voice_channel_id={self.voice_channel_id!r})"
        )

    def ensure_session(
        self,
        *,
        guild_id: int,
        vlc_pid: int,
        vlc_executable: str | Path,
    ) -> dict[str, object]:
        payload = {
            "guild_id": str(guild_id),
            "channel_id": str(self.voice_channel_id),
            "self_mute": True,
            "self_deaf": True,
            "stream": {
                "pid": vlc_pid,
                "executable_path": str(Path(vlc_executable).resolve()),
                "audio": True,
                "resolution": 720,
                "fps": 30,
            },
        }
        return self._request("PUT", "/v1/session", payload)

    def disconnect_session(self) -> dict[str, object]:
        """Stop the client stream and leave voice without touching VLC."""
        try:
            self._request("DELETE", "/v1/stream")
        except ClientBridgeError:
            # Leaving voice also terminates an application stream. Still issue
            # the leave request when explicit stream cleanup could not confirm.
            pass
        return self._request("DELETE", "/v1/voice")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except HTTPError as error:
            try:
                raw = error.read(MAX_RESPONSE_BYTES + 1)
                status = error.code
            finally:
                error.close()
        except (OSError, URLError, TimeoutError) as error:
            raise ClientBridgeError(
                "bridge_unavailable",
                "Discord client bridge is unavailable",
            ) from error

        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientBridgeError(
                "invalid_response",
                "Discord client bridge returned an oversized response",
            )
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientBridgeError(
                "invalid_response",
                "Discord client bridge returned invalid JSON",
            ) from error
        if not isinstance(result, dict):
            raise ClientBridgeError(
                "invalid_response",
                "Discord client bridge returned an invalid response",
            )
        if 200 <= status < 300 and result.get("ok") is True:
            return result

        error_data = result.get("error")
        code = "request_failed"
        message = "Discord client bridge rejected the session request"
        retryable = status >= 500 or status in {408, 409, 429}
        if isinstance(error_data, dict):
            raw_code = error_data.get("code")
            raw_message = error_data.get("message")
            if isinstance(raw_code, str) and raw_code.isascii() and len(raw_code) <= 64:
                code = raw_code
            if isinstance(raw_message, str) and len(raw_message) <= 240:
                message = raw_message
            retryable_value = error_data.get("retryable")
            if isinstance(retryable_value, bool):
                retryable = retryable_value

        # Do not allow an untrusted server response to echo the bearer token.
        if self._token:
            message = message.replace(self._token, "[redacted]")
        raise ClientBridgeError(code, message, retryable=retryable)
