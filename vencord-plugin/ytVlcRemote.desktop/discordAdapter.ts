/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { findByCodeLazy, findByPropsLazy, findStoreLazy } from "@webpack";
import {
    ApplicationStreamingStore,
    ChannelStore,
    MediaEngineStore,
    PermissionsBits,
    PermissionStore,
    SelectedChannelStore,
    UserStore
} from "@webpack/common";

import type { BridgeCommand, CommandResult, VerifiedCaptureSource } from "./types";

const ChannelActions = findByPropsLazy("selectChannel", "selectVoiceChannel") as {
    selectVoiceChannel(channelId: string | null): Promise<void> | void;
};
const VoiceActions = findByPropsLazy("toggleSelfMute") as {
    toggleSelfMute(): void;
    toggleSelfDeaf(): void;
};
const startStreamWithSource = findByCodeLazy("no user or channel", "Starting stream for source id") as (
    source: { id: string; name: string; },
    options: Record<string, unknown>
) => Promise<[boolean, string | undefined]>;
const stopStream = findByCodeLazy('type:"STREAM_STOP"') as (streamKey: string) => Promise<void> | void;
const ApplicationStreamingSettingsStore = findStoreLazy("ApplicationStreamingSettingsStore") as {
    getState(): {
        resolution?: number;
        fps?: number;
        soundshareEnabled?: boolean;
    };
};
const StreamPresets = findByPropsLazy("PRESET_VIDEO", "PRESET_CUSTOM", "PRESET_AUTO") as {
    PRESET_CUSTOM: number;
};

const CONFIRM_TIMEOUT_MS = 12_000;
const CONFIRM_INTERVAL_MS = 100;
const SESSION_STREAM_SETTLE_MS = 1_500;

class AdapterError extends Error {
    constructor(
        readonly status: number,
        readonly code: string,
        message: string,
        readonly retryable = false
    ) {
        super(message);
    }
}

function requiredString(value: unknown, field: string): string {
    if (typeof value !== "string" || !value) throw new AdapterError(400, "invalid_request", `${field} is required`);
    return value;
}

async function waitFor(predicate: () => boolean, code: string, message: string): Promise<void> {
    const deadline = Date.now() + CONFIRM_TIMEOUT_MS;
    while (Date.now() < deadline) {
        if (predicate()) return;
        await new Promise(resolve => setTimeout(resolve, CONFIRM_INTERVAL_MS));
    }
    throw new AdapterError(504, code, message, true);
}

function currentStream(): any | null {
    const selectedChannelId = SelectedChannelStore.getVoiceChannelId?.();
    if (!selectedChannelId) return null;
    let stream = ApplicationStreamingStore.getCurrentUserActiveStream?.() ?? null;
    if (!stream) {
        const currentUserId = UserStore.getCurrentUser?.()?.id;
        stream = currentUserId
            ? ApplicationStreamingStore.getAnyStreamForUser?.(currentUserId) ?? null
            : null;
    }
    return stream?.channelId === selectedChannelId ? stream : null;
}

function streamKey(stream: any): string {
    if (!stream?.channelId || !stream?.ownerId || !stream?.guildId) {
        throw new AdapterError(503, "discord_api_unavailable", "Discord's stream state is unavailable", true);
    }
    return `${stream.streamType ?? "guild"}:${stream.guildId}:${stream.channelId}:${stream.ownerId}`;
}

function currentStatus(): Record<string, unknown> {
    const stream = currentStream();
    const metadata = ApplicationStreamingStore.getStreamerActiveStreamMetadata?.();
    const source = MediaEngineStore.getGoLiveSource?.();
    const settings = ApplicationStreamingSettingsStore.getState?.() ?? {};
    return {
        voice: {
            channel_id: SelectedChannelStore.getVoiceChannelId?.() ?? null,
            self_mute: Boolean(MediaEngineStore.isSelfMute?.()),
            self_deaf: Boolean(MediaEngineStore.isSelfDeaf?.())
        },
        stream: stream ? {
            active: true,
            guild_id: stream.guildId ?? null,
            channel_id: stream.channelId ?? null,
            source_id: source?.desktopSource?.id ?? null,
            source_pid: metadata?.pid ?? source?.desktopSource?.sourcePid ?? null,
            source_name: metadata?.sourceName ?? null,
            resolution: source?.quality?.resolution ?? settings.resolution ?? null,
            fps: source?.quality?.frameRate ?? settings.fps ?? null
        } : { active: false }
    };
}

function validateChannel(payload: Record<string, unknown>): { guildId: string; channelId: string; channel: any; } {
    const guildId = requiredString(payload.guild_id, "guild_id");
    const channelId = requiredString(payload.channel_id, "channel_id");
    const channel = ChannelStore.getChannel(channelId);
    if (!channel || channel.guild_id !== guildId) {
        throw new AdapterError(404, "voice_channel_unavailable", "The requested voice channel is unavailable");
    }
    if (channel.type !== 2) {
        throw new AdapterError(400, "unsupported_channel", "Only ordinary guild voice channels are supported");
    }
    for (const [permission, label] of [
        [PermissionsBits.VIEW_CHANNEL, "View Channel"],
        [PermissionsBits.CONNECT, "Connect"]
    ] as const) {
        if (!PermissionStore.can(permission, channel)) {
            throw new AdapterError(403, "missing_permission", `The account lacks ${label} permission`);
        }
    }
    return { guildId, channelId, channel };
}

async function setMuteState(mute: boolean, deaf: boolean): Promise<void> {
    if (deaf && !mute) throw new AdapterError(400, "invalid_request", "A deafened account must also be muted");
    if (Boolean(MediaEngineStore.isSelfDeaf?.()) !== deaf) VoiceActions.toggleSelfDeaf();
    await waitFor(
        () => Boolean(MediaEngineStore.isSelfDeaf?.()) === deaf,
        "voice_state_timeout",
        "Discord did not confirm the self-deafen state"
    );
    if (Boolean(MediaEngineStore.isSelfMute?.()) !== mute) VoiceActions.toggleSelfMute();
    await waitFor(
        () => Boolean(MediaEngineStore.isSelfMute?.()) === mute,
        "voice_state_timeout",
        "Discord did not confirm the self-mute state"
    );
}

async function joinVoice(payload: Record<string, unknown>): Promise<void> {
    const { channelId } = validateChannel(payload);
    if (SelectedChannelStore.getVoiceChannelId?.() !== channelId) {
        await Promise.resolve(ChannelActions.selectVoiceChannel(channelId));
    }
    await waitFor(
        () => SelectedChannelStore.getVoiceChannelId?.() === channelId,
        "voice_join_timeout",
        "Discord did not confirm the voice connection"
    );
    await setMuteState(Boolean(payload.self_mute), Boolean(payload.self_deaf));
}

async function leaveVoice(): Promise<void> {
    if (SelectedChannelStore.getVoiceChannelId?.()) {
        await Promise.resolve(ChannelActions.selectVoiceChannel(null));
    }
    await waitFor(
        () => !SelectedChannelStore.getVoiceChannelId?.(),
        "voice_leave_timeout",
        "Discord did not confirm leaving voice"
    );
}

async function stopSharing(): Promise<void> {
    const stream = currentStream();
    if (!stream) return;
    await Promise.resolve(stopStream(streamKey(stream)));
    await waitFor(() => !currentStream(), "stream_stop_timeout", "Discord did not confirm stopping the stream");
}

async function startSharing(source?: VerifiedCaptureSource): Promise<void> {
    if (!source) throw new AdapterError(400, "invalid_request", "A verified VLC source is required");
    const selectedChannelId = SelectedChannelStore.getVoiceChannelId?.();
    if (!selectedChannelId) throw new AdapterError(409, "voice_not_connected", "Join a guild voice channel before streaming");
    const channel = ChannelStore.getChannel(selectedChannelId);
    if (!channel || channel.type !== 2 || !channel.guild_id) {
        throw new AdapterError(409, "unsupported_channel", "The current call is not an ordinary guild voice channel");
    }
    if (!PermissionStore.can(PermissionsBits.STREAM, channel)) {
        throw new AdapterError(403, "missing_permission", "The account lacks Stream permission");
    }
    if (currentStream()) await stopSharing();
    const [started] = await startStreamWithSource({ id: source.id, name: source.name }, {
        preset: StreamPresets.PRESET_CUSTOM,
        resolution: 720,
        fps: 30,
        soundshareEnabled: true,
        previewDisabled: true
    });
    if (!started) {
        throw new AdapterError(
            503,
            "discord_api_unavailable",
            "Discord could not start the verified VLC window stream",
            true
        );
    }
    await waitFor(() => {
        const stream = currentStream();
        const metadata = ApplicationStreamingStore.getStreamerActiveStreamMetadata?.();
        const desktopSource = MediaEngineStore.getGoLiveSource?.()?.desktopSource;
        const livePid = metadata?.pid ?? desktopSource?.sourcePid;
        return Boolean(
            stream
            && stream.channelId === selectedChannelId
            && desktopSource?.id === source.id
            && Number(livePid) === source.pid
        );
    }, "stream_start_timeout", "Discord did not confirm the VLC stream");

    try {
        await waitFor(() => {
            return ApplicationStreamingSettingsStore.getState?.().soundshareEnabled === true;
        }, "stream_audio_timeout", "Discord did not confirm application audio sharing");
    } catch (error) {
        await stopSharing();
        throw error;
    }

    const quality = ApplicationStreamingSettingsStore.getState?.();
    if (quality?.resolution !== 720 || quality?.fps !== 30 || quality?.soundshareEnabled !== true) {
        await stopSharing();
        throw new AdapterError(
            409,
            "quality_unavailable",
            "Discord did not accept 720p/30 FPS for this account",
            false
        );
    }
}

function success(): CommandResult {
    return { ok: true, status: 200, data: currentStatus() };
}

export async function executeCommand(command: BridgeCommand): Promise<CommandResult> {
    try {
        switch (command.kind) {
            case "status":
                return success();
            case "voice.put":
                await joinVoice(command.payload);
                return success();
            case "voice.delete":
                await leaveVoice();
                return success();
            case "stream.put":
                await startSharing(command.source);
                return success();
            case "stream.delete":
                await stopSharing();
                return success();
            case "session.put": {
                const { guildId, channelId } = validateChannel(command.payload);
                await joinVoice(command.payload);
                await new Promise(resolve => setTimeout(resolve, SESSION_STREAM_SETTLE_MS));
                if (SelectedChannelStore.getVoiceChannelId?.() !== channelId) {
                    throw new AdapterError(409, "voice_state_changed", "The voice channel changed during setup", true);
                }
                await startSharing(command.source);
                const stream = currentStream();
                if (!stream || stream.guildId !== guildId || stream.channelId !== channelId) {
                    throw new AdapterError(409, "stream_state_changed", "The stream changed during setup", true);
                }
                return success();
            }
        }
    } catch (error) {
        if (error instanceof AdapterError) {
            return {
                ok: false,
                status: error.status,
                error: { code: error.code, message: error.message, retryable: error.retryable }
            };
        }
        return {
            ok: false,
            status: 503,
            error: {
                code: "discord_api_unavailable",
                message: "Discord's internal voice or Go Live API is unavailable",
                retryable: true
            }
        };
    }
}

export function adapterAvailable(): boolean {
    try {
        const functions: unknown[] = [
            ChannelActions?.selectVoiceChannel,
            VoiceActions?.toggleSelfMute,
            VoiceActions?.toggleSelfDeaf,
            startStreamWithSource,
            stopStream,
            ApplicationStreamingStore?.getCurrentUserActiveStream,
            ApplicationStreamingStore?.getAnyStreamForUser,
            MediaEngineStore?.getGoLiveSource,
            ApplicationStreamingSettingsStore?.getState,
            UserStore?.getCurrentUser
        ];
        return functions.every(value => typeof value === "function");
    } catch {
        return false;
    }
}
