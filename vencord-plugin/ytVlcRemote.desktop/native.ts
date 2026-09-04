/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { execFile } from "child_process";
import { randomUUID } from "crypto";
import { desktopCapturer, type IpcMainInvokeEvent } from "electron";
import { realpath } from "fs/promises";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "http";
import { basename, resolve } from "path";
import { promisify } from "util";

import {
    captureSourceHandle,
    command,
    expectedHost,
    failure,
    positiveInteger,
    safeBearer,
    SerializedCommandQueue,
    validatedStreamPayload,
    validateVoicePayload
} from "./nativeCore";
import type { BridgeCommand, CaptureSourceSummary, CommandKind, CommandResult, VerifiedCaptureSource } from "./types";

const execFileAsync = promisify(execFile);
const BODY_LIMIT = 16 * 1024;
const QUEUE_LIMIT = 16;
const COMMAND_TIMEOUT_MS = 45_000;
const POLL_TIMEOUT_MS = 25_000;
const POWERSHELL_PROCESS_QUERY = [
    "$ErrorActionPreference='Stop'",
    "$p=Get-Process -Id ([int]$args[0])",
    "[pscustomobject]@{Id=$p.Id;Path=$p.Path;MainWindowHandle=[string]$p.MainWindowHandle;MainWindowTitle=$p.MainWindowTitle}|ConvertTo-Json -Compress"
].join(";");

interface ProcessIdentity {
    Id: number;
    Path: string;
    MainWindowHandle: string;
    MainWindowTitle: string;
}

let server: Server | null = null;
let configuredPort = 0;
let bearerToken = "";
const commands = new SerializedCommandQueue(QUEUE_LIMIT, COMMAND_TIMEOUT_MS);

function sendJson(response: ServerResponse, status: number, body: unknown): void {
    const encoded = Buffer.from(JSON.stringify(body));
    response.writeHead(status, {
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": encoded.length,
        "X-Content-Type-Options": "nosniff"
    });
    response.end(encoded);
}

function normalizedHost(request: IncomingMessage): boolean {
    return expectedHost(request.headers.host, configuredPort);
}

function authorized(request: IncomingMessage): boolean {
    return safeBearer(request.headers.authorization, bearerToken);
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
    if (!request.headers["content-type"]?.toLowerCase().startsWith("application/json")) {
        throw failure(415, "unsupported_media_type", "Content-Type must be application/json");
    }
    const declaredLength = Number(request.headers["content-length"] ?? 0);
    if (!Number.isFinite(declaredLength) || declaredLength < 0 || declaredLength > BODY_LIMIT) {
        throw failure(413, "body_too_large", "JSON request body exceeds 16 KiB");
    }
    const chunks: Buffer[] = [];
    let length = 0;
    for await (const chunk of request) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        length += bytes.length;
        if (length > BODY_LIMIT) {
            throw failure(413, "body_too_large", "JSON request body exceeds 16 KiB");
        }
        chunks.push(bytes);
    }
    if (!length) return {};
    try {
        const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        return parsed as Record<string, unknown>;
    } catch {
        throw failure(400, "invalid_json", "Request body must be a JSON object");
    }
}

async function canonicalPath(value: string): Promise<string> {
    return (await realpath(resolve(value))).toLocaleLowerCase("en-US");
}

async function verifyVlcSource(value: Record<string, unknown>): Promise<VerifiedCaptureSource> {
    const pid = positiveInteger(value.pid, "pid");
    const expectedPath = String(value.executable_path);
    let identity: ProcessIdentity;
    try {
        const { stdout } = await execFileAsync(
            "powershell.exe",
            ["-NoProfile", "-NonInteractive", "-Command", POWERSHELL_PROCESS_QUERY, String(pid)],
            { windowsHide: true, timeout: 5_000, maxBuffer: 32 * 1024 }
        );
        identity = JSON.parse(stdout) as ProcessIdentity;
    } catch {
        throw failure(404, "vlc_unavailable", "The requested VLC process is unavailable", true);
    }
    if (identity.Id !== pid || !identity.Path || basename(identity.Path).toLowerCase() !== "vlc.exe") {
        throw failure(400, "invalid_vlc_process", "The PID does not identify vlc.exe");
    }
    let actualCanonical: string;
    let expectedCanonical: string;
    try {
        [actualCanonical, expectedCanonical] = await Promise.all([
            canonicalPath(identity.Path),
            canonicalPath(expectedPath)
        ]);
    } catch {
        throw failure(400, "invalid_vlc_path", "The VLC executable path could not be verified");
    }
    if (actualCanonical !== expectedCanonical) {
        throw failure(409, "vlc_identity_mismatch", "The VLC PID does not match executable_path");
    }
    let handle: bigint;
    try {
        handle = BigInt(identity.MainWindowHandle);
    } catch {
        handle = 0n;
    }
    if (handle <= 0n) {
        throw failure(409, "vlc_window_unavailable", "VLC does not have a capturable main window", true);
    }
    const sources = await desktopCapturer.getSources({
        types: ["window"],
        thumbnailSize: { width: 0, height: 0 },
        fetchWindowIcons: false
    });
    const source = sources.find(candidate => captureSourceHandle(candidate.id) === handle);
    if (!source) {
        throw failure(404, "vlc_capture_source_unavailable", "Discord cannot capture the verified VLC window", true);
    }
    return {
        id: source.id,
        name: source.name,
        pid,
        executablePath: identity.Path,
        windowHandle: handle.toString()
    };
}

async function captureSources(): Promise<CaptureSourceSummary[]> {
    const sources = await desktopCapturer.getSources({
        types: ["window"],
        thumbnailSize: { width: 0, height: 0 },
        fetchWindowIcons: false
    });
    return sources.map(source => ({ id: source.id, name: source.name, kind: "window" }));
}

async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (!normalizedHost(request)) {
        sendJson(response, 400, failure(400, "invalid_host", "Unexpected Host header"));
        return;
    }
    if (request.headers.origin) {
        sendJson(response, 403, failure(403, "origin_forbidden", "Browser Origin requests are not accepted"));
        return;
    }
    const url = new URL(request.url ?? "/", `http://127.0.0.1:${configuredPort}`);
    if (url.pathname === "/v1/health" && request.method === "GET") {
        sendJson(response, 200, { ok: true, data: { service: "yt-vlc-remote", version: 1 } });
        return;
    }
    if (!authorized(request)) {
        sendJson(response, 401, failure(401, "unauthorized", "Bearer authentication is required"));
        return;
    }
    if (url.search) {
        sendJson(response, 400, failure(400, "invalid_request", "Query parameters are not supported"));
        return;
    }
    if (url.pathname === "/v1/capture-sources" && request.method === "GET") {
        sendJson(response, 200, { ok: true, data: { sources: await captureSources() } });
        return;
    }

    const routeKey = `${request.method} ${url.pathname}`;
    const kind = ({
        "GET /v1/status": "status",
        "PUT /v1/voice": "voice.put",
        "DELETE /v1/voice": "voice.delete",
        "PUT /v1/stream": "stream.put",
        "DELETE /v1/stream": "stream.delete",
        "PUT /v1/session": "session.put"
    } as Record<string, CommandKind | undefined>)[routeKey];
    if (!kind) {
        sendJson(response, 404, failure(404, "not_found", "Unknown REST endpoint"));
        return;
    }
    let payload: Record<string, unknown> = {};
    if (request.method === "PUT") payload = await readJson(request);
    let source: VerifiedCaptureSource | undefined;
    if (kind === "voice.put" || kind === "session.put") validateVoicePayload(payload);
    if (kind === "stream.put" || kind === "session.put") source = await verifyVlcSource(validatedStreamPayload(payload));
    const result = await commands.enqueue(command(randomUUID(), kind, payload, source));
    sendJson(response, result.status, result);
}

export async function startServer(_: IpcMainInvokeEvent, port: number, token: string): Promise<void> {
    if (process.platform !== "win32") throw new Error("ytVlcRemote only supports Windows");
    if (basename(process.execPath).toLowerCase() !== "discordcanary.exe") {
        throw new Error("ytVlcRemote only supports Discord Canary");
    }
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid bridge port");
    if (typeof token !== "string" || token.length < 32 || token.length > 256) throw new Error("Invalid bridge token");
    if (server) return;
    configuredPort = port;
    bearerToken = token;
    server = createServer((request, response) => {
        route(request, response).catch((reason: unknown) => {
            const result = reason && typeof reason === "object" && "status" in reason
                ? reason as CommandResult
                : failure(500, "internal_error", "The bridge could not process the request", true);
            if (!response.headersSent) sendJson(response, result.status, result);
            else response.destroy();
        });
    });
    server.requestTimeout = 50_000;
    server.headersTimeout = 10_000;
    try {
        await new Promise<void>((resolveStart, rejectStart) => {
            server!.once("error", rejectStart);
            server!.listen(port, "127.0.0.1", () => {
                server!.off("error", rejectStart);
                resolveStart();
            });
        });
    } catch {
        server?.close();
        server = null;
        bearerToken = "";
        throw new Error("The localhost bridge port is unavailable");
    }
}

export async function stopServer(_: IpcMainInvokeEvent): Promise<void> {
    commands.stop();
    const current = server;
    server = null;
    bearerToken = "";
    if (current) await new Promise<void>(resolveClose => current.close(() => resolveClose()));
}

export function rotateToken(_: IpcMainInvokeEvent, token: string): void {
    if (typeof token !== "string" || token.length < 32 || token.length > 256) throw new Error("Invalid bridge token");
    bearerToken = token;
}

export async function nextCommand(_: IpcMainInvokeEvent): Promise<BridgeCommand | null> {
    return commands.next(POLL_TIMEOUT_MS);
}

export function completeCommand(_: IpcMainInvokeEvent, id: string, result: CommandResult): void {
    const safeResult = result && typeof result === "object" && Number.isInteger(result.status)
        ? result
        : failure(500, "invalid_renderer_response", "Discord returned an invalid command result", true);
    commands.complete(id, safeResult);
}
