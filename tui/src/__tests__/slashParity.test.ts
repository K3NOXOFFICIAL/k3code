import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { parseSlashCommand } from "@k3code/shared/slash";
import { describe, expect, it } from "vitest";

import { findSlashCommand, SLASH_COMMANDS } from "../app/slash/registry.js";

type CommandRoute = "fallback" | "local" | "native";

interface CommandRegistryLoad {
  error?: string;
  names: string[];
}

// k3code M1: the core-side command registry is `k3code.commands.builtin.
// build_registry()`. Commands the TUI handles locally need no route assert;
// the gateway-side ones (compact/effort/exit/rename/resume/stop) must dispatch
// natively via command.dispatch — never as a slash-worker prompt fallback.
// /compact and /stop have no local TUI command: the generic slash.exec path
// runs the gateway's own command and shows its `output`.
const NATIVE_MUTATING_COMMANDS = new Set([
  "compact",
  "effort",
  "exit",
  "rename",
  "resume",
  "stop",
]);

const MUTATING_COMMANDS = [
  "clear",
  "compact",
  "effort",
  "exit",
  "help",
  "model",
  "rename",
  "resume",
  "stop",
] as const;

const loadCommandRegistryNames = (): CommandRegistryLoad => {
  const here = dirname(fileURLToPath(import.meta.url));

  try {
    const names = JSON.parse(
      execFileSync(
        process.env.PYTHON ?? "python3",
        [
          "-c",
          "import json; from k3code.commands.builtin import build_registry; print(json.dumps(build_registry().names()))",
        ],
        {
          cwd: resolve(here, "../../.."),
          encoding: "utf8",
          // Pipe stderr: a missing k3code install prints a python traceback
          // that would otherwise leak into the test runner's output. The
          // traceback still lands on the error object for the skip reason.
          stdio: ["ignore", "pipe", "pipe"],
        },
      ),
    ) as string[];

    return { names: [...new Set(names)] };
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : String(error),
      names: [],
    };
  }
};

const commandRegistry = loadCommandRegistryNames();
const registryIt = commandRegistry.error ? it.skip : it;
const skipReason = commandRegistry.error
  ? commandRegistry.error.split("\n")[0]
  : "";

const LOCAL_COMMAND_NAMES = new Set(
  SLASH_COMMANDS.flatMap((command) =>
    [command.name, ...(command.aliases ?? [])].map((name) =>
      name.toLowerCase(),
    ),
  ),
);

const classifyRoute = (name: string): CommandRoute => {
  const normalized = name.toLowerCase();

  if (NATIVE_MUTATING_COMMANDS.has(normalized)) {
    return "native";
  }

  if (LOCAL_COMMAND_NAMES.has(normalized)) {
    return "local";
  }

  return "fallback";
};

describe("slash parity matrix", () => {
  if (commandRegistry.error) {
    it.skip(`Python command registry unavailable: ${skipReason}`, () => {});
  }

  registryIt("keeps every mutating command off slash-worker fallback", () => {
    const routes = Object.fromEntries(
      commandRegistry.names.map((name) => [name, classifyRoute(name)]),
    );

    for (const name of MUTATING_COMMANDS) {
      expect(
        routes[name],
        `missing command in registry: ${name}`,
      ).toBeDefined();
      expect(
        routes[name],
        `mutating command must not fallback: ${name}`,
      ).not.toBe("fallback");
    }
  });

  it("/q alias resolves to queue, not quit (#31983)", () => {
    // Regression for #31983: the TUI `quit` command used to carry alias `q`,
    // which collided with the Python-side `/queue` alias. TUI-local commands
    // dispatch before the backend, so `/q` resolved to /quit (session.die)
    // instead of queueing a prompt.
    const cmd = findSlashCommand("q");
    expect(cmd, "/q must resolve to a command").toBeDefined();
    expect(cmd!.name).toBe("queue");
  });
});

describe("parseSlashCommand argument fidelity", () => {
  it("keeps a multi-line argument byte-for-byte", () => {
    const arg = "first line\nsecond line\n\n  indented tail";

    expect(parseSlashCommand(`/pr-triage ${arg}`)).toEqual({
      arg,
      name: "pr-triage",
    });
  });

  it("preserves runs of spaces inside the argument", () => {
    expect(parseSlashCommand("/goal ship   it").arg).toBe("ship   it");
  });

  it("still splits the command name off a single separator", () => {
    expect(parseSlashCommand("/cron add daily")).toEqual({
      arg: "add daily",
      name: "cron",
    });
    expect(parseSlashCommand("/exit")).toEqual({ arg: "", name: "exit" });
    expect(parseSlashCommand("/exit ")).toEqual({ arg: "", name: "exit" });
  });
});
