import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, describe, expect, it, vi } from "vitest";

import { patchOverlayState, resetOverlayState } from "../app/overlayStore.js";
import { FloatBox } from "../components/appChrome.js";
import { ProviderPicker } from "../components/providerPicker.js";
import type { GatewayClient } from "../gatewayClient.js";
import { DEFAULT_THEME } from "../theme.js";

import { waitFor } from "./waitFor.js";

// The /provider popup's "+ Add new provider" form, driven through the picker
// (the form only ever runs inside it) with a gateway fake routed by method.

class FakeInput extends EventEmitter {
  chunks: string[] = [];
  isRaw = false;
  isTTY = true;
  readableLength = 0;

  read() {
    const next = this.chunks.shift() ?? null;
    this.readableLength = this.chunks.length;

    return next;
  }

  ref = vi.fn();

  send(...chunks: string[]) {
    this.chunks.push(...chunks);
    this.readableLength = this.chunks.length;
    this.emit("readable");
  }

  setEncoding = vi.fn();

  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled;
  });

  unref = vi.fn();
}

const settle = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));

const DOWN = "\u001b[B";
const ENTER = "\r";
const ESC = "\u001b";
const CTRL_U = "\u0015";

const KEY = "sk-ZZTOPSECRET987";

const PRESETS = [
  {
    base_url: "https://api.openai.com/v1",
    id: "openai",
    key_env: "OPENAI_API_KEY",
    key_set: false,
    keyless: false,
    kind: "openai",
    label: "OpenAI",
    name: "openai",
  },
  {
    base_url: "https://api.anthropic.com",
    id: "anthropic",
    key_env: "ANTHROPIC_API_KEY",
    key_set: true,
    keyless: false,
    kind: "anthropic",
    label: "Anthropic",
    name: "anthropic",
  },
  {
    base_url: "http://localhost:11434/v1",
    id: "local",
    key_env: "LOCAL_API_KEY",
    key_set: false,
    keyless: true,
    kind: "openai",
    label: "Ollama",
    name: "local",
  },
  {
    base_url: "",
    id: "custom",
    key_env: "",
    key_set: false,
    keyless: false,
    kind: "openai",
    label: "any OpenAI-compatible endpoint",
    name: "endpoint",
  },
];

const MAIN = {
  authenticated: true,
  is_current: true,
  name: "Main",
  slug: "main",
};

const PROBE_OK = {
  detail: "",
  hint: "",
  models: ["gpt-a", "gpt-b", "gpt-c"],
  ms: 12,
  ok: true,
  suggested: "gpt-b",
};

const PROBE_FAIL = {
  detail: "connection refused",
  hint: "is the server running?",
  models: [],
  ms: 3,
  ok: false,
  suggested: "",
};

type Params = Record<string, unknown>;

function mount(
  opts: {
    add?: (params: Params) => Promise<unknown>;
    probe?: () => Promise<unknown>;
  } = {},
  size: { columns: number; rows: number } = { columns: 80, rows: 24 },
) {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";
  let added = "";

  Object.assign(stdout, { ...size, isTTY: false });
  // the picker sizes itself from useStdout(), which is process.stdout: pin it to the same terminal
  const realSize = Object.fromEntries(
    (["columns", "rows"] as const).map((k) => [
      k,
      Object.getOwnPropertyDescriptor(process.stdout, k),
    ]),
  );

  for (const k of ["columns", "rows"] as const) {
    Object.defineProperty(process.stdout, k, {
      configurable: true,
      value: size[k],
    });
  }

  const restoreSize = () => {
    for (const k of ["columns", "rows"] as const) {
      const d = realSize[k];

      if (d) {
        Object.defineProperty(process.stdout, k, d);
      } else {
        delete (process.stdout as unknown as Record<string, unknown>)[k];
      }
    }
  };

  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const request = vi.fn((method: string, params: Params = {}) => {
    switch (method) {
      case "model.options":
        return Promise.resolve({
          providers: added ? [MAIN, { name: added, slug: added }] : [MAIN],
        });

      case "provider.presets":
        return Promise.resolve({ presets: PRESETS });

      case "provider.probe":
        return opts.probe ? opts.probe() : Promise.resolve(PROBE_OK);

      case "provider.add":
        if (opts.add) {
          return opts.add(params);
        }

        added = String(params.name);

        return Promise.resolve({
          backup: "",
          key_env: "OPENAI_API_KEY",
          provider: { name: added, slug: added },
        });

      default:
        return Promise.reject(new Error(`unexpected ${method}`));
    }
  });

  const onCancel = vi.fn();
  const onSelect = vi.fn<(slug: string, saveDefault: boolean) => void>();

  const instance = renderSync(
    <FloatBox color={DEFAULT_THEME.color.border}>
      <ProviderPicker
        gw={{ request } as unknown as GatewayClient}
        onCancel={onCancel}
        onSelect={onSelect}
        sessionId="sid-1"
        t={DEFAULT_THEME}
      />
    </FloatBox>,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  const all = () => stripAnsi(output);

  // the output is every frame in a row: the last painted box is the screen now
  const last = () => {
    const text = all();

    return text.slice(text.lastIndexOf("╔"), text.lastIndexOf("╝") + 1);
  };

  const calls = (method: string) =>
    request.mock.calls.filter(([m]) => m === method).map(([, p]) => p ?? {});

  /** Send one key or chunk, then wait for the screen to show `expectText`. */
  const press = async (chunk: string, expectText?: string) => {
    stdin.send(chunk);

    if (expectText) {
      await waitFor(() => expect(last()).toContain(expectText));
    } else {
      await settle();
    }
  };

  return {
    all,
    calls,
    last,
    onCancel,
    onSelect,
    press,
    request,
    stop: () => {
      instance.unmount();
      instance.cleanup();
      restoreSize();
    },
  };
}

/** From the list into the form's first step (the add row is the last row). */
async function openForm(t: ReturnType<typeof mount>) {
  await waitFor(() => expect(t.last()).toContain("Add new provider"));
  await t.press("2", "Add provider · kind");
  await waitFor(() => expect(t.last()).toContain("openai · OpenAI"));
}

/** The secret travels only as `api_key` of the probe and the add; no other field or method carries it. */
function expectKeyOnlyInSecretField(t: ReturnType<typeof mount>) {
  for (const [method, params = {}] of t.request.mock.calls) {
    const { api_key: secret, ...rest } = params;

    expect(JSON.stringify(rest)).not.toContain("TOPSECRET");

    if (secret !== undefined) {
      expect(["provider.probe", "provider.add"]).toContain(method);
      expect(secret).toBe(KEY);
    }
  }
}

afterEach(() => {
  resetOverlayState();
});

describe("ProviderAddForm (in the /provider popup)", () => {
  it("walks preset → name → URL → key → test → model → save, sends the exact provider.add, then offers the new block", async () => {
    const t = mount();

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    expect(t.last()).toContain("openai");
    await t.press(CTRL_U, "(empty)");
    await t.press("myai", "myai");
    await t.press(ENTER, "Add provider · base URL");
    expect(t.last()).toContain("https://api.openai.com/v1");
    await t.press(ENTER, "Add provider · API key");
    await t.press(KEY, "•".repeat(KEY.length));
    await t.press(ENTER, "Add provider · default model");

    // the probe's suggested model is preselected, not the first one listed
    expect(t.last()).toContain("connection OK · 3 models");
    expect(t.last()).toContain("▸ gpt-b");
    expect(t.calls("provider.probe")).toEqual([
      {
        api_key: KEY,
        base_url: "https://api.openai.com/v1",
        kind: "openai",
        preset: "openai",
      },
    ]);

    await t.press(ENTER, "Add provider · save");
    expect(t.last()).toContain("myai · openai · gpt-b");
    expect(t.calls("provider.add")).toEqual([]);

    await t.press(ENTER, "added myai · Enter use now · Esc later");
    expect(t.calls("provider.add")).toEqual([
      {
        api_key: KEY,
        base_url: "https://api.openai.com/v1",
        kind: "openai",
        model: "gpt-b",
        name: "myai",
        preset: "openai",
        session_id: "sid-1",
      },
    ]);

    // back in the list: refetched, the new block is there and highlighted
    expect(t.calls("model.options")).toHaveLength(2);
    expect(t.last()).toMatch(/▸ 2\. .*myai/);

    await t.press(ENTER);
    await waitFor(() => expect(t.onSelect).toHaveBeenCalledTimes(1));
    expect(t.onSelect).toHaveBeenCalledWith("myai", false);

    expectKeyOnlyInSecretField(t);
    t.stop();
    // masked in every frame painted, whole or in part
    expect(t.all()).not.toContain(KEY);
    expect(t.all()).not.toContain("TOPSECRET");
  });

  it("Esc steps back one step at a time and leaves the form from the first step without writing", async () => {
    const t = mount();

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    await t.press(ENTER, "Add provider · base URL");
    await t.press(ESC, "Add provider · name");
    await t.press(ESC, "Add provider · kind");
    await t.press(ESC, "Select provider");

    expect(t.onCancel).not.toHaveBeenCalled();
    expect(t.calls("provider.probe")).toEqual([]);
    expect(t.calls("provider.add")).toEqual([]);

    // the list is the picker again: Esc there closes the popup
    await t.press(ESC);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));
    t.stop();
  });

  it("refuses a name an existing block has, regardless of case", async () => {
    const t = mount();

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    await t.press(CTRL_U, "(empty)");
    await t.press("MAIN", "MAIN");
    await t.press(ENTER, "a provider named MAIN already exists");

    expect(t.last()).toContain("Add provider · name");
    t.stop();
  });

  it("shows the gateway's duplicate-name error in the form and stays there", async () => {
    const t = mount({
      add: () =>
        Promise.reject(new Error("a provider named myai already exists")),
    });

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    await t.press(CTRL_U, "(empty)");
    await t.press("myai", "myai");
    await t.press(ENTER, "Add provider · base URL");
    await t.press(ENTER, "Add provider · API key");
    await t.press(KEY, "•");
    await t.press(ENTER, "Add provider · default model");
    await t.press(ENTER, "Add provider · save");
    await t.press(ENTER, "a provider named myai already exists");

    expect(t.last()).toContain("Add provider · save");
    expect(t.calls("provider.add")).toHaveLength(1);
    expect(t.calls("model.options")).toHaveLength(1);
    expect(t.onSelect).not.toHaveBeenCalled();
    t.stop();
    expect(t.all()).not.toContain("TOPSECRET");
  });

  it("after a failed test, saves only after an explicit confirm", async () => {
    const t = mount({ probe: () => Promise.resolve(PROBE_FAIL) });

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    await t.press(ENTER, "Add provider · base URL");
    await t.press(ENTER, "Add provider · API key");
    await t.press(KEY, "•");
    await t.press(ENTER, "FAIL: connection refused");
    expect(t.last()).toContain("is the server running?");

    // Enter on a failed test does not move on
    await t.press(ENTER);
    await settle(80);
    expect(t.last()).toContain("Add provider · test connection");

    await t.press("s", "Add provider · save untested?");
    await t.press("n", "Add provider · test connection");
    await t.press("s", "Add provider · save untested?");
    await t.press("y", "Add provider · default model");
    expect(t.last()).toContain("untested");
    await t.press("my-model", "my-model");
    await t.press(ENTER, "Add provider · save");
    expect(t.last()).toContain("untested");
    expect(t.calls("provider.add")).toEqual([]);

    await t.press(ENTER, "added openai");
    expect(t.calls("provider.add")).toEqual([
      {
        api_key: KEY,
        base_url: "https://api.openai.com/v1",
        kind: "openai",
        model: "my-model",
        name: "openai",
        preset: "openai",
        session_id: "sid-1",
      },
    ]);
    // stepping back to the test showed the last result; it did not probe again
    expect(t.calls("provider.probe")).toHaveLength(1);
    t.stop();
  });

  it("reuses the preset's saved key when asked: the probe and the add carry reuse_key, never a key", async () => {
    const t = mount();

    await openForm(t);
    await t.press(DOWN, "▸ anthropic · Anthropic");
    await t.press(ENTER, "Add provider · name");
    await t.press(ENTER, "Add provider · base URL");
    await t.press(ENTER, "ANTHROPIC_API_KEY already holds a key");
    await t.press("y", "Add provider · default model");

    expect(t.calls("provider.probe")).toEqual([
      {
        base_url: "https://api.anthropic.com",
        kind: "anthropic",
        preset: "anthropic",
        reuse_key: true,
      },
    ]);

    await t.press(ENTER, "key: reuse ANTHROPIC_API_KEY");
    await t.press(ENTER, "added anthropic");
    expect(t.calls("provider.add")).toEqual([
      {
        base_url: "https://api.anthropic.com",
        kind: "anthropic",
        model: "gpt-b",
        name: "anthropic",
        preset: "anthropic",
        reuse_key: true,
        session_id: "sid-1",
      },
    ]);
    t.stop();
  });

  it("asks for no key for the keyless preset", async () => {
    const t = mount();

    await openForm(t);
    await t.press(DOWN, "▸ anthropic");
    await t.press(DOWN, "▸ local");
    await t.press(ENTER, "Add provider · name");
    await t.press(ENTER, "Add provider · base URL");
    await t.press(ENTER, "Add provider · default model");

    expect(t.calls("provider.probe")).toEqual([
      {
        base_url: "http://localhost:11434/v1",
        kind: "openai",
        preset: "local",
      },
    ]);
    t.stop();
  });

  it("ignores every key while a prompt waits for an answer", async () => {
    const t = mount();

    await openForm(t);
    await t.press(ENTER, "Add provider · name");
    patchOverlayState({
      approval: { command: "ls", description: "", requestId: "a" },
    } as never);
    await settle();
    await t.press(ESC);
    await t.press(ENTER);
    await settle(150);
    expect(t.last()).toContain("Add provider · name");

    resetOverlayState();
    await settle();
    await t.press(ENTER, "Add provider · base URL");
    t.stop();
  });

  it("fits 60x15 at every step", async () => {
    const t = mount(
      {
        probe: () =>
          Promise.resolve({
            ...PROBE_OK,
            models: Array.from(
              { length: 30 },
              (_, i) => `vendor/a-rather-long-model-identifier-${i}`,
            ),
            suggested: "vendor/a-rather-long-model-identifier-7",
          }),
      },
      { columns: 60, rows: 15 },
    );

    const fits = () => {
      const lines = t.last().split("\n");

      // box + its top margin + the status rule under it
      expect(lines.length + 2).toBeLessThanOrEqual(15);

      // the renderer clips at 60 columns: a box that is too wide loses its right border
      for (const line of lines) {
        expect([...line].length).toBeLessThanOrEqual(60);
        expect(line).toMatch(/[║╗╝]$/);
      }
    };

    await openForm(t);
    fits();
    await t.press(ENTER, "Add provider · name");
    fits();
    await t.press(ENTER, "Add provider · base URL");
    fits();
    await t.press(ENTER, "Add provider · API key");
    await t.press(KEY, "•");
    fits();
    await t.press(ENTER, "Add provider · default model");
    expect(t.last()).toContain("▸ vendor/a-rather-long-model-identifier-7");
    fits();
    await t.press(ENTER, "Add provider · save");
    fits();
    t.stop();
    expect(t.all()).not.toContain("TOPSECRET");
  });
});
