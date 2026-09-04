/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

export type CommandKind =
    | "status"
    | "voice.put"
    | "voice.delete"
    | "stream.put"
    | "stream.delete"
    | "session.put";

export interface BridgeError {
    code: string;
    message: string;
    retryable: boolean;
}

export interface CommandResult {
    ok: boolean;
    status: number;
    data?: Record<string, unknown>;
    error?: BridgeError;
}

export interface VerifiedCaptureSource {
    id: string;
    name: string;
    pid: number;
    executablePath: string;
    windowHandle: string;
}

export interface BridgeCommand {
    id: string;
    kind: CommandKind;
    payload: Record<string, unknown>;
    source?: VerifiedCaptureSource;
}

export interface CaptureSourceSummary {
    id: string;
    name: string;
    kind: "window";
}
