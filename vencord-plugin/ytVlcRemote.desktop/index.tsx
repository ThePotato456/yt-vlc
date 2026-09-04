/*
 * Vencord, a Discord client mod
 * Copyright (c) 2026 yt-vlc contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { definePluginSettings } from "@api/Settings";
import { Button } from "@components/Button";
import definePlugin, { OptionType, type PluginNative } from "@utils/types";
import { showToast, Toasts } from "@webpack/common";

import { adapterAvailable, executeCommand } from "./discordAdapter";
import type { BridgeCommand, CommandResult } from "./types";

const Native = VencordNative.pluginHelpers.YtVlcRemote as PluginNative<typeof import("./native")>;

let running = false;

function generateToken(): string {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const encoded = btoa(String.fromCharCode(...bytes));
    return encoded.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function TokenControls() {
    return (
        <div style={{ display: "flex", gap: "8px" }}>
            <Button onClick={async () => {
                await navigator.clipboard.writeText(settings.store.apiToken);
                showToast("REST bridge token copied", Toasts.Type.SUCCESS);
            }}>
                Copy token
            </Button>
            <Button variant="dangerPrimary" onClick={() => {
                const token = generateToken();
                settings.store.apiToken = token;
                void Native.rotateToken(token);
                showToast("REST bridge token regenerated", Toasts.Type.SUCCESS);
            }}>
                Regenerate token
            </Button>
        </div>
    );
}

const settings = definePluginSettings({
    port: {
        type: OptionType.NUMBER,
        description: "IPv4 loopback REST port (restart Discord after changing)",
        default: 38423,
        restartNeeded: true
    },
    apiToken: {
        type: OptionType.STRING,
        description: "Generated REST bearer token",
        default: "",
        hidden: true
    },
    tokenControls: {
        type: OptionType.COMPONENT,
        component: TokenControls
    }
});

async function commandLoop(): Promise<void> {
    while (running) {
        let command: BridgeCommand | null = null;
        try {
            command = await Native.nextCommand();
        } catch {
            if (running) await new Promise(resolve => setTimeout(resolve, 1_000));
            continue;
        }
        if (!running || !command) continue;
        let result: CommandResult;
        if (!adapterAvailable()) {
            result = {
                ok: false,
                status: 503,
                error: {
                    code: "discord_api_unavailable",
                    message: "Discord's internal voice or Go Live API is unavailable",
                    retryable: true
                }
            };
        } else {
            result = await executeCommand(command);
        }
        await Native.completeCommand(command.id, result);
    }
}

export default definePlugin({
    name: "YtVlcRemote",
    description: "Authenticated localhost control of Canary voice and exact VLC application sharing",
    authors: [{ name: "yt-vlc", id: 0n }],
    settings,

    async start() {
        if (!settings.store.apiToken) settings.store.apiToken = generateToken();
        try {
            await Native.startServer(settings.store.port, settings.store.apiToken);
        } catch {
            showToast("YtVlcRemote could not bind its localhost port", Toasts.Type.FAILURE);
            return;
        }
        running = true;
        void commandLoop();
    },

    async stop() {
        running = false;
        await Native.stopServer();
    }
});
