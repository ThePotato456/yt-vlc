/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    captureSourceHandle,
    command,
    expectedHost,
    safeBearer,
    sanitizedWindowName,
    SerializedCommandQueue,
    validatedStreamPayload,
    validateVoicePayload,
    windowsProcessQuery
} from "./nativeCore";
import type { CommandResult } from "./types";

const voice = {
    guild_id: "123456789012345678",
    channel_id: "234567890123456789",
    self_mute: true,
    self_deaf: true
};
const stream = {
    pid: 4321,
    executable_path: "C:\\VLC\\vlc.exe",
    audio: true,
    resolution: 720,
    fps: 30
};
const success: CommandResult = { ok: true, status: 200, data: {} };

describe("native REST boundary", () => {
    it("accepts only expected loopback Host values", () => {
        assert.equal(expectedHost("127.0.0.1:38423", 38423), true);
        assert.equal(expectedHost("LOCALHOST:38423", 38423), true);
        assert.equal(expectedHost("example.com:38423", 38423), false);
        assert.equal(expectedHost("127.0.0.1:9999", 38423), false);
    });

    it("compares complete bearer values", () => {
        assert.equal(safeBearer("Bearer correct-token", "correct-token"), true);
        assert.equal(safeBearer("Bearer wrong-token", "correct-token"), false);
        assert.equal(safeBearer("Basic correct-token", "correct-token"), false);
    });

    it("redacts URLs and bounds names returned by source diagnostics", () => {
        assert.equal(
            sanitizedWindowName("https://cdn.example/video?token=secret - VLC"),
            "[redacted URL] - VLC"
        );
        assert.equal(sanitizedWindowName(""), "Application window");
        assert.equal(sanitizedWindowName("x".repeat(300)).length, 240);
    });

    it("matches window handles without accepting display sources", () => {
        assert.equal(captureSourceHandle("window:12345:0"), 12345n);
        assert.equal(captureSourceHandle("window:0x3039:0"), 12345n);
        assert.equal(captureSourceHandle("screen:0:0"), null);
        assert.equal(captureSourceHandle("window:not-a-handle:0"), null);
    });

    it("validates voice and exact baseline stream requests", () => {
        assert.doesNotThrow(() => validateVoicePayload(voice));
        assert.deepEqual(validatedStreamPayload({ stream }), stream);
        assert.throws(() => validatedStreamPayload({ stream: { ...stream, audio: false } }));
        assert.throws(() => validatedStreamPayload({ stream: { ...stream, resolution: 1080 } }));
    });

    it("builds a non-interpolatable Windows process query", () => {
        const query = windowsProcessQuery(24252);
        assert.match(query, /Get-Process -Id 24252/);
        assert.doesNotMatch(query, /\$args/);
        assert.throws(() => windowsProcessQuery("24252"));
    });

    it("serializes mocked renderer actions", async () => {
        const queue = new SerializedCommandQueue(4, 1_000);
        const firstResult = queue.enqueue(command("one", "voice.put", voice));
        const secondResult = queue.enqueue(command("two", "session.put", { ...voice, stream }));

        assert.equal((await queue.next(10))?.id, "one");
        assert.equal(await queue.next(1), null);
        queue.complete("one", success);
        assert.equal((await firstResult).status, 200);
        assert.equal((await queue.next(10))?.id, "two");
        queue.complete("two", success);
        assert.equal((await secondResult).status, 200);
    });

    it("bounds the queue and returns sanitized timeouts", async () => {
        const bounded = new SerializedCommandQueue(1, 1_000);
        const first = bounded.enqueue(command("one", "status", {}));
        const overflow = await bounded.enqueue(command("two", "status", {}));
        assert.equal(overflow.error?.code, "queue_full");
        bounded.stop();
        await first;

        const timingOut = new SerializedCommandQueue(1, 5);
        const timeout = await timingOut.enqueue(command("timeout", "status", {}));
        assert.deepEqual(timeout.error, {
            code: "command_timeout",
            message: "Discord did not confirm the command in time",
            retryable: true
        });
    });

    it("does not overlap a later command when an active renderer action times out", async () => {
        const queue = new SerializedCommandQueue(2, 10);
        const first = queue.enqueue(command("one", "session.put", { ...voice, stream }));
        const second = queue.enqueue(command("two", "voice.delete", {}));

        assert.equal((await queue.next(10))?.id, "one");
        assert.equal((await first).error?.code, "command_timeout");
        assert.equal(await queue.next(1), null);

        queue.complete("one", success);
        assert.equal((await queue.next(10))?.id, "two");
        queue.complete("two", success);
        assert.equal((await second).status, 200);
    });
});
