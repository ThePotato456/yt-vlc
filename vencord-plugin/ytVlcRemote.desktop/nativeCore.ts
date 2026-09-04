/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { timingSafeEqual } from "crypto";

import type { BridgeCommand, CommandKind, CommandResult, VerifiedCaptureSource } from "./types";

export function failure(status: number, code: string, message: string, retryable = false): CommandResult {
    return { ok: false, status, error: { code, message, retryable } };
}

export function expectedHost(host: string | undefined, port: number): boolean {
    const value = host?.toLowerCase();
    return value === `127.0.0.1:${port}` || value === `localhost:${port}`;
}

export function safeBearer(header: string | undefined, token: string): boolean {
    if (!header?.startsWith("Bearer ")) return false;
    const supplied = Buffer.from(header.slice(7), "utf8");
    const expected = Buffer.from(token, "utf8");
    return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

export function sanitizedWindowName(value: unknown): string {
    if (typeof value !== "string" || !value.trim()) return "Application window";
    return value.replace(/https?:\/\/\S+/gi, "[redacted URL]").slice(0, 240);
}

export function captureSourceHandle(sourceId: string): bigint | null {
    const match = /^window:([^:]+):/.exec(sourceId);
    if (!match) return null;
    try {
        return BigInt(match[1]);
    } catch {
        return null;
    }
}

export function positiveInteger(value: unknown, field: string): number {
    if (!Number.isSafeInteger(value) || Number(value) <= 0) {
        throw failure(400, "invalid_request", `${field} must be a positive integer`);
    }
    return Number(value);
}

export function windowsProcessQuery(pidValue: unknown): string {
    const pid = positiveInteger(pidValue, "pid");
    return [
        "$ErrorActionPreference='Stop'",
        "Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Collections.Generic;",
        "using System.Runtime.InteropServices;",
        "using System.Text;",
        "public sealed class YtVlcWindowInfo { public long Handle { get; set; } public string Title { get; set; } }",
        "public static class YtVlcWindowLookup {",
        "  private delegate bool EnumWindowsProc(IntPtr handle, IntPtr state);",
        "  [DllImport(\"user32.dll\")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr state);",
        "  [DllImport(\"user32.dll\")] private static extern bool IsWindowVisible(IntPtr handle);",
        "  [DllImport(\"user32.dll\")] private static extern IntPtr GetWindow(IntPtr handle, uint command);",
        "  [DllImport(\"user32.dll\")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);",
        "  [DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] private static extern int GetWindowTextLength(IntPtr handle);",
        "  [DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr handle, StringBuilder value, int maximum);",
        "  public static YtVlcWindowInfo[] Find(int expectedProcessId) {",
        "    var found = new List<YtVlcWindowInfo>();",
        "    EnumWindows((handle, state) => {",
        "      uint processId; GetWindowThreadProcessId(handle, out processId);",
        "      if (processId != (uint)expectedProcessId || !IsWindowVisible(handle) || GetWindow(handle, 4) != IntPtr.Zero) return true;",
        "      int length = GetWindowTextLength(handle); if (length <= 0) return true;",
        "      var title = new StringBuilder(length + 1); GetWindowText(handle, title, title.Capacity);",
        "      found.Add(new YtVlcWindowInfo { Handle = handle.ToInt64(), Title = title.ToString() });",
        "      return true;",
        "    }, IntPtr.Zero);",
        "    return found.ToArray();",
        "  }",
        "}",
        "'@",
        `$p=Get-Process -Id ${pid}`,
        `$windows=@([YtVlcWindowLookup]::Find(${pid}))`,
        "[pscustomobject]@{Id=$p.Id;Path=$p.Path;Windows=$windows}|ConvertTo-Json -Depth 4 -Compress"
    ].join("\n");
}

export function discordSnowflake(value: unknown, field: string): string {
    if (typeof value !== "string" || !/^\d{15,22}$/.test(value) || value === "0") {
        throw failure(400, "invalid_request", `${field} must be a Discord snowflake`);
    }
    return value;
}

export function validateVoicePayload(payload: Record<string, unknown>): void {
    discordSnowflake(payload.guild_id, "guild_id");
    discordSnowflake(payload.channel_id, "channel_id");
    if (typeof payload.self_mute !== "boolean" || typeof payload.self_deaf !== "boolean") {
        throw failure(400, "invalid_request", "self_mute and self_deaf must be booleans");
    }
}

export function validatedStreamPayload(payload: Record<string, unknown>): Record<string, unknown> {
    const candidate = payload.stream ?? payload;
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
        throw failure(400, "invalid_request", "stream must be an object");
    }
    const value = candidate as Record<string, unknown>;
    positiveInteger(value.pid, "pid");
    if (typeof value.executable_path !== "string" || !value.executable_path.trim()) {
        throw failure(400, "invalid_request", "executable_path must be a non-empty string");
    }
    if (value.audio !== true || value.resolution !== 720 || value.fps !== 30) {
        throw failure(400, "invalid_request", "Only audio-enabled 720p/30 FPS streaming is supported");
    }
    return value;
}

interface PendingCommand {
    command: BridgeCommand;
    resolve: (result: CommandResult) => void;
    timer: ReturnType<typeof setTimeout>;
    timedOut: boolean;
}

export class SerializedCommandQueue {
    private pending: PendingCommand[] = [];
    private active: PendingCommand | null = null;
    private waiters: Array<(command: BridgeCommand | null) => void> = [];

    constructor(
        private readonly capacity: number,
        private readonly commandTimeoutMs: number
    ) { }

    enqueue(command: BridgeCommand): Promise<CommandResult> {
        if (this.pending.length + (this.active ? 1 : 0) >= this.capacity) {
            return Promise.resolve(failure(503, "queue_full", "The bridge command queue is full", true));
        }
        return new Promise(resolveCommand => {
            const item: PendingCommand = {
                command,
                resolve: resolveCommand,
                timedOut: false,
                timer: setTimeout(() => {
                    item.timedOut = true;
                    if (this.active !== item) {
                        this.pending = this.pending.filter(candidate => candidate !== item);
                    }
                    resolveCommand(failure(504, "command_timeout", "Discord did not confirm the command in time", true));
                    this.wake();
                }, this.commandTimeoutMs)
            };
            this.pending.push(item);
            this.wake();
        });
    }

    next(pollTimeoutMs: number): Promise<BridgeCommand | null> {
        if (this.active) return Promise.resolve(null);
        if (this.pending.length) {
            this.active = this.pending.shift()!;
            return Promise.resolve(this.active.command);
        }
        return new Promise(resolvePoll => {
            const finish = (command: BridgeCommand | null) => {
                clearTimeout(timer);
                resolvePoll(command);
            };
            const timer = setTimeout(() => {
                this.waiters = this.waiters.filter(waiter => waiter !== finish);
                resolvePoll(null);
            }, pollTimeoutMs);
            this.waiters.push(finish);
        });
    }

    complete(id: string, result: CommandResult): void {
        if (!this.active || this.active.command.id !== id) return;
        const item = this.active;
        this.active = null;
        clearTimeout(item.timer);
        if (!item.timedOut) item.resolve(result);
        this.wake();
    }

    stop(): void {
        const result = failure(503, "bridge_stopped", "The Discord client bridge stopped", true);
        if (this.active) {
            clearTimeout(this.active.timer);
            if (!this.active.timedOut) this.active.resolve(result);
            this.active = null;
        }
        for (const item of this.pending.splice(0)) {
            clearTimeout(item.timer);
            if (!item.timedOut) item.resolve(result);
        }
        for (const waiter of this.waiters.splice(0)) waiter(null);
    }

    private wake(): void {
        if (this.active || !this.pending.length || !this.waiters.length) return;
        this.active = this.pending.shift()!;
        this.waiters.shift()!(this.active.command);
    }
}

export function command(
    id: string,
    kind: CommandKind,
    payload: Record<string, unknown>,
    source?: VerifiedCaptureSource
): BridgeCommand {
    return { id, kind, payload, source };
}
