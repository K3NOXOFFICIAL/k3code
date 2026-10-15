import { EventEmitter } from "node:events";

import { renderSync } from "@k3code/ink";
import type { ModelOptionProvider } from "@k3code/shared/gateway-events";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import {
  flatModelHits,
  initialModelIdx,
  ModelPicker,
  modelRowLabel,
  pickerCanDisconnect,
  pickerNeedsKey,
  pickerOffersReasoning,
} from "../components/modelPicker.js";
import type { GatewayClient } from "../gatewayClient.js";
import { DARK_THEME } from "../theme.js";

// #365: the picker searched one provider at a time, opened its model list on
// the first row instead of the current model, never showed the model ids or
// the config descriptions the gateway sends, offered the effort step for every
// model, showed a provider step with one provider, and sent model.save_key
// params the gateway did not read.

const openai: ModelOptionProvider = {
  authenticated: true,
  auth_type: "api_key",
  capabilities: {
    cheap: { fast: false, reasoning: false },
    default: { fast: false, reasoning: true },
  },
  descriptions: { cheap: "Fast and cheap" },
  is_current: true,
  key_env: "OPENAI_API_KEY",
  model_ids: { cheap: "gpt-4o-mini", default: "gpt-5.1", plain: "llama3" },
  models: ["cheap", "default", "plain"],
  name: "openai",
  slug: "openai",
};

const anthropic: ModelOptionProvider = {
  authenticated: true,
  auth_type: "api_key",
  capabilities: { deep: { fast: false, reasoning: true } },
  descriptions: { deep: "Hardest problems" },
  key_env: "ANTHROPIC_API_KEY",
  model_ids: { deep: "claude-opus-5", default: "claude-sonnet-5" },
  models: ["deep", "default"],
  name: "anthropic",
  slug: "anthropic",
};

const nokey: ModelOptionProvider = {
  authenticated: false,
  auth_type: "api_key",
  key_env: "OTHER_API_KEY",
  model_ids: { default: "other-1" },
  models: ["default"],
  name: "other",
  slug: "other",
  warning: "paste OTHER_API_KEY to activate",
};

const rows = (...ps: ModelOptionProvider[]) =>
  ps.map((p) => ({ name: p.name, provider: p }));

describe("ModelPicker helpers", () => {
  it("searches every provider's models as provider/model", () => {
    const all = flatModelHits(rows(openai, anthropic), "");

    expect(all.map((h) => `${h.provider.slug}/${h.model}`)).toEqual([
      "openai/cheap",
      "openai/default",
      "openai/plain",
      "anthropic/deep",
      "anthropic/default",
    ]);

    const opus = flatModelHits(rows(openai, anthropic), "opus");

    // the model id it sends is searched too: `deep` is claude-opus-5
    expect(opus.map((h) => `${h.provider.slug}/${h.model}`)).toEqual([
      "anthropic/deep",
    ]);

    const scoped = flatModelHits(rows(openai, anthropic), "anth def");

    expect(scoped[0] && `${scoped[0].provider.slug}/${scoped[0].model}`).toBe(
      "anthropic/default",
    );
  });

  it("starts the model cursor on the current model", () => {
    expect(initialModelIdx(openai, "plain")).toBe(2);
    expect(initialModelIdx(openai, "deep")).toBe(0); // not listed: first row
    expect(initialModelIdx(undefined, "plain")).toBe(0);
  });

  it("labels a row with its model id and description", () => {
    expect(modelRowLabel(openai, "cheap")).toBe(
      "cheap (gpt-4o-mini) · Fast and cheap",
    );
    expect(modelRowLabel(anthropic, "deep", "anthropic")).toBe(
      "anthropic/deep (claude-opus-5) · Hardest problems",
    );
    expect(modelRowLabel({ ...openai, model_ids: null }, "plain")).toBe(
      "plain",
    );
  });

  it("offers the effort step only where reasoning is supported or unknown", () => {
    expect(pickerOffersReasoning(openai, "cheap")).toBe(false);
    expect(pickerOffersReasoning(openai, "default")).toBe(true);
    expect(pickerOffersReasoning(openai, "plain")).toBe(true); // unknown
  });

  it("knows which providers take a pasted key or can be disconnected", () => {
    expect(pickerNeedsKey(nokey)).toBe(true);
    expect(pickerNeedsKey(openai)).toBe(false);
    expect(pickerCanDisconnect(openai)).toBe(true);
    expect(pickerCanDisconnect(nokey)).toBe(false);
    expect(
      pickerCanDisconnect({ ...openai, auth_type: "none", key_env: null }),
    ).toBe(false);
  });
});

// ── rendered picker, driven by keys ─────────────────────────────────────

class FakeTty extends EventEmitter {
  columns = 100;
  rows = 40;
  isTTY: boolean;
  constructor(isTTY = true) {
    super();
    this.isTTY = isTTY;
  }
  frames: string[] = [];
  private pending: string[] = [];
  ref(): void {}
  unref(): void {}
  read(): string | null {
    return this.pending.shift() ?? null;
  }
  send(chunk: string): void {
    this.pending.push(chunk);
    this.emit("readable");
  }
  setEncoding(): this {
    return this;
  }
  setRawMode(): this {
    return this;
  }
  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void) {
    this.frames.push(
      typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"),
    );
    cb?.();

    return true;
  }
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 20));
const DOWN = "\x1b[B";
const ENTER = "\r";
const ESC = "\x1b";

type Call = { method: string; params: Record<string, unknown> };

async function drive(
  providers: ModelOptionProvider[],
  current: { model: string; provider: string },
  keys: string[],
  replies: Record<string, unknown> = {},
) {
  const calls: Call[] = [];
  const selected: string[] = [];
  let cancelled = false;
  const gw = {
    request: async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params });

      if (method === "model.options") {
        return { model: current.model, provider: current.provider, providers };
      }

      const reply = replies[method];

      if (reply instanceof Error) {
        throw reply; // the gateway answered with an error
      }

      return reply ?? {};
    },
  } as unknown as GatewayClient;
  // a non-TTY stdout gets every frame whole (a TTY one gets cell diffs)
  const stdout = new FakeTty(false);
  const stdin = new FakeTty();
  const view = renderSync(
    React.createElement(ModelPicker, {
      gw,
      onCancel: () => {
        cancelled = true;
      },
      onSelect: (v: string) => selected.push(v),
      sessionId: "s1",
      t: DARK_THEME,
    }),
    {
      patchConsole: false,
      stderr: new FakeTty() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  try {
    await tick();

    for (const k of keys) {
      stdin.send(k);
      await tick();

      if (k === ESC) {
        // a lone Escape is held for ink's 50 ms escape-sequence timeout
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      }
    }

    const frames = stdout.frames.map((f) => stripAnsi(f));

    return {
      calls,
      cancelled,
      frames,
      screen: frames.join("\n"),
      selected,
    };
  } finally {
    view.unmount();
    view.cleanup();
  }
}

/** The last whole frame that shows `marker`. */
const lastFrame = (frames: string[], marker: string) =>
  frames.filter((f) => f.includes(marker)).at(-1) ?? "";

describe("ModelPicker", () => {
  it("typing on the first screen searches all providers and picks provider/model", async () => {
    const r = await drive(
      [openai, anthropic],
      { model: "default", provider: "openai" },
      [..."opus", ENTER, ENTER],
    );
    const search = lastFrame(r.frames, "Search models in every provider");

    expect(search).toContain(
      "anthropic/deep (claude-opus-5) · Hardest problems",
    );
    // deep supports reasoning: step 3, then Enter picks the first effort
    expect(r.selected).toEqual([
      "deep --provider anthropic --reasoning minimal --tui-session",
    ]);
  });

  it("opens a provider's models on the current model and shows ids and descriptions", async () => {
    const r = await drive(
      [openai, anthropic],
      { model: "plain", provider: "openai" },
      [ENTER],
    );
    const models = lastFrame(r.frames, "Select model (step 2/3)");

    expect(models).toContain("▸ 3. plain (llama3)");
    expect(models).toContain("cheap (gpt-4o-mini) · Fast and cheap");
  });

  it("skips the effort step for a model without reasoning control", async () => {
    const r = await drive(
      [openai, anthropic],
      { model: "cheap", provider: "openai" },
      [ENTER, ENTER],
    );

    expect(r.selected).toEqual(["cheap --provider openai --tui-session"]);
    expect(r.screen).not.toContain("Reasoning effort");
  });

  it("skips the provider step when there is one provider", async () => {
    const r = await drive([openai], { model: "default", provider: "openai" }, [
      DOWN,
      ENTER,
    ]);

    expect(r.screen).not.toContain("Select provider");
    expect(r.screen).toContain("Select model (step 1/2)");
    // default -> plain (unknown reasoning: the effort step is still offered)
    expect(r.screen).toContain("Reasoning effort (step 2/2)");
    expect(r.selected).toEqual([]);

    const back = await drive(
      [openai],
      { model: "default", provider: "openai" },
      [ESC],
    );

    expect(back.cancelled).toBe(true); // Esc closes: no provider step behind it
  });

  it("sends model.save_key with the contract's param names", async () => {
    const saved = { ...nokey, authenticated: true, warning: null };
    const r = await drive(
      [openai, nokey],
      { model: "default", provider: "openai" },
      [DOWN, ENTER, ..."sk-test", ENTER],
      { "model.save_key": { provider: saved } },
    );
    const save = r.calls.find((c) => c.method === "model.save_key");

    expect(save?.params).toEqual({
      api_key: "sk-test",
      session_id: "s1",
      slug: "other",
    });
    expect(r.screen).not.toContain("sk-test"); // the key is masked
    expect(lastFrame(r.frames, "Select model")).toContain("default (other-1)");
  });

  it("stays on the disconnect prompt and shows the gateway's refusal", async () => {
    const r = await drive(
      [openai, anthropic],
      { model: "default", provider: "openai" },
      ["\x04", "y"],
      {
        "model.disconnect": new Error(
          "OPENAI_API_KEY is set in the daemon's environment",
        ),
      },
    );
    const prompt = lastFrame(r.frames, "Disconnect openai?");

    expect(r.calls.map((c) => c.method)).toContain("model.disconnect");
    expect(prompt).toContain("error:");
    expect(prompt).not.toContain("Select provider");
  });
});
