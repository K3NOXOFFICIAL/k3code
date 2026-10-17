import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import { ActiveSessionSwitcher } from "../components/activeSessionSwitcher.js";
import type { GatewayClient } from "../gatewayClient.js";
import { DEFAULT_THEME } from "../theme.js";

// #170 /resume picker glue: `session.active_list` also lists the daemon's
// running sub-agents (origin "subagent"), which share the agent strip but are
// not sessions. The /resume picker showed them as live sessions, and picking
// one sent `session.activate` with a sub-agent id: "unknown session". The
// strip and the agent view already leave them out.

const gw = {
  request: async (method: string) =>
    method === "session.active_list"
      ? {
          sessions: [
            {
              current: true,
              id: "live-1",
              model: "default",
              status: "completed",
              title: "My session",
            },
            {
              id: "sub-7",
              model: "cheap",
              origin: "subagent",
              status: "running",
              title: "CHILD scan the repo",
            },
          ],
        }
      : { sessions: [] },
} as unknown as GatewayClient;

const stdinStub = () =>
  Object.assign(new EventEmitter(), {
    isTTY: true,
    read: () => null,
    readableLength: 0,
    ref: () => {},
    setEncoding: () => {},
    setRawMode: () => {},
    unref: () => {},
  }) as unknown as NodeJS.ReadStream;

describe("/resume picker", () => {
  it("does not list running sub-agents as sessions", async () => {
    const stdout = Object.assign(new PassThrough(), {
      columns: 100,
      isTTY: false,
      rows: 30,
    });
    let out = "";

    stdout.on("data", (chunk) => {
      out += chunk.toString();
    });

    const view = renderSync(
      <ActiveSessionSwitcher
        currentSessionId="live-1"
        gw={gw}
        onCancel={() => {}}
        onClose={async () => null}
        onNew={() => {}}
        onNewPrompt={() => {}}
        onResume={() => {}}
        onSelect={() => {}}
        t={DEFAULT_THEME}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdinStub(),
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );

    await new Promise((resolve) => setTimeout(resolve, 300));
    view.unmount();
    view.cleanup();

    const text = stripAnsi(out);

    expect(text).toContain("My session");
    expect(text).not.toContain("CHILD scan the repo");
  });
});
