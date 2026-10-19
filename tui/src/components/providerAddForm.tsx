import { Box, Text, useInput } from "@k3code/ink";
import { useEffect, useMemo, useState } from "react";

import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint, windowItems } from "./overlayControls.js";
import { chipRowProps } from "./overlayPrimitives.js";

/** One row of `provider.presets` (the setup wizard's presets plus the two custom kinds). */
export interface ProviderPreset {
  base_url: string;
  id: string;
  key_env: string;
  key_set: boolean;
  keyless: boolean;
  kind: string;
  label: string;
  name: string;
}

interface ProbeResult {
  detail: string;
  hint: string;
  models: string[];
  ms: number;
  ok: boolean;
  suggested: string;
}

type Step =
  | "confirm"
  | "key"
  | "model"
  | "name"
  | "preset"
  | "reuse"
  | "review"
  | "test"
  | "url";

const VISIBLE_PRESETS = 6;
const VISIBLE_MODELS = 4;
const NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;

/** A block name the gateway accepts and no existing block has (names are unique regardless of case). */
export function providerNameError(
  name: string,
  existing: readonly string[],
): string {
  if (!NAME_RE.test(name)) {
    return "name: a letter, then letters, digits, - or _";
  }

  return existing.some((n) => n.toLowerCase() === name.toLowerCase())
    ? `a provider named ${name} already exists`
    : "";
}

/** An http(s) URL with a host, as `provider.add` checks it. */
export function baseUrlError(url: string): string {
  try {
    const u = new URL(url);

    if ((u.protocol === "http:" || u.protocol === "https:") && u.hostname) {
      return /\s/.test(url) ? "no spaces in the URL" : "";
    }
  } catch {
    // fall through
  }

  return "URL must start with http:// or https://";
}

const trimUrl = (url: string) => url.trim().replace(/\/+$/, "");

/** Text-field keys, the key stage's pattern: printable append, Backspace, Ctrl+U clear. */
function editText(
  ch: string,
  key: {
    backspace?: boolean;
    ctrl?: boolean;
    delete?: boolean;
    meta?: boolean;
  },
  set: (f: (v: string) => string) => void,
): void {
  if (key.backspace || key.delete) {
    set((v) => v.slice(0, -1));
  } else if (ch === "\u0015" || (key.ctrl && ch === "u")) {
    set(() => "");
  } else if (ch && !key.ctrl && !key.meta) {
    set((v) => v + ch.replace(/[\r\n]/g, ""));
  }
}

/**
 * /provider's "+ Add new provider" form (in the popup, no slash command):
 * preset -> name -> base URL -> key (or reuse the preset's saved key) ->
 * live test -> default model -> save through `provider.add`. Esc steps
 * back one step and leaves the form from the first; nothing is written
 * before the last Enter. The key is only ever shown masked.
 */
export function ProviderAddForm({
  existingNames,
  gw,
  locked,
  onCancel,
  onSaved,
  sessionId,
  t,
  width,
}: ProviderAddFormProps) {
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [step, setStep] = useState<Step>("preset");
  const [presetIdx, setPresetIdx] = useState(0);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [reuse, setReuse] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [query, setQuery] = useState("");
  const [modelIdx, setModelIdx] = useState(0);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    gw.request<{ presets?: ProviderPreset[] }>("provider.presets", {})
      .then((raw) => {
        setPresets(
          asRpcResult<{ presets?: ProviderPreset[] }>(raw)?.presets ?? [],
        );
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)));
  }, [gw]);

  const preset = presets[presetIdx];
  const offersReuse =
    !!preset?.key_set &&
    !preset.keyless &&
    trimUrl(url) === trimUrl(preset.base_url);

  const models = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = probe?.ok ? probe.models : [];

    return q ? all.filter((m) => m.toLowerCase().includes(q)) : all;
  }, [probe, query]);

  // `useReuse` is passed, not read from state: the reuse answer and the probe it starts land in one key event.
  const endpoint = (useReuse: boolean) => ({
    base_url: trimUrl(url),
    kind: preset?.kind ?? "openai",
    preset: preset?.id ?? "custom",
    ...(useReuse
      ? { reuse_key: true }
      : apiKey.trim()
        ? { api_key: apiKey.trim() }
        : {}),
  });

  const runProbe = (useReuse: boolean) => {
    setProbe(null);
    setBusy(true);
    setErr("");
    gw.request<ProbeResult>("provider.probe", endpoint(useReuse))
      .then((raw) => {
        const r = asRpcResult<ProbeResult>(raw);

        setBusy(false);

        if (!r) {
          setErr("invalid response: provider.probe");

          return;
        }

        setProbe(r);

        if (r.ok) {
          setQuery("");
          setModelIdx(Math.max(0, r.models.indexOf(r.suggested)));
          setStep("model");
        }
      })
      .catch((e: unknown) => {
        setBusy(false);
        setErr(rpcErrorMessage(e));
      });
  };

  const save = () => {
    setBusy(true);
    setErr("");
    gw.request<{ provider?: { slug?: string } }>("provider.add", {
      ...endpoint(reuse),
      model,
      name: name.trim(),
      ...(sessionId ? { session_id: sessionId } : {}),
    })
      .then((raw) => {
        const slug = asRpcResult<{ provider?: { slug?: string } }>(raw)
          ?.provider?.slug;

        setBusy(false);
        setApiKey("");
        onSaved(slug || name.trim());
      })
      .catch((e: unknown) => {
        setBusy(false);
        setErr(rpcErrorMessage(e));
      });
  };

  /** The input step before the test: key, reuse question or URL. */
  const inputBefore = (): Step =>
    preset?.keyless ? "url" : reuse ? "reuse" : "key";

  const go = (next: Step) => {
    setErr("");
    setStep(next);
  };

  /** Forward into the test step: probe now. Stepping back to it keeps the last result. */
  const startTest = (useReuse: boolean) => {
    go("test");
    runProbe(useReuse);
  };

  // Like the picker: while a prompt waits for an answer the form ignores every key.
  useInput((ch, key) => {
    if (busy || locked) {
      return;
    }

    switch (step) {
      case "preset":
        if (key.escape) {
          onCancel();
        } else if (key.upArrow && presetIdx > 0) {
          setPresetIdx((v) => v - 1);
        } else if (key.downArrow && presetIdx < presets.length - 1) {
          setPresetIdx((v) => v + 1);
        } else if (key.return && preset) {
          setName(preset.name);
          setUrl(preset.base_url);
          setApiKey("");
          setReuse(false);
          go("name");
        }

        return;

      case "name":
        if (key.escape) {
          go("preset");
        } else if (key.return) {
          const e = providerNameError(name.trim(), existingNames);

          if (e) {
            setErr(e);
          } else {
            go("url");
          }
        } else {
          editText(ch, key, setName);
        }

        return;

      case "url":
        if (key.escape) {
          go("name");
        } else if (key.return) {
          const e = baseUrlError(url.trim());

          if (e) {
            setErr(e);
          } else {
            setReuse(false);

            if (preset?.keyless) {
              startTest(false);
            } else {
              go(offersReuse ? "reuse" : "key");
            }
          }
        } else {
          editText(ch, key, setUrl);
        }

        return;

      case "reuse":
        if (key.escape) {
          go("url");
        } else if (ch === "y" || ch === "Y") {
          setReuse(true);
          startTest(true);
        } else if (ch === "n" || ch === "N") {
          setReuse(false);
          go("key");
        }

        return;

      case "key":
        if (key.escape) {
          setApiKey("");
          go(offersReuse ? "reuse" : "url");
        } else if (key.return) {
          if (apiKey.trim()) {
            startTest(false);
          }
        } else {
          editText(ch, key, setApiKey);
        }

        return;

      case "test":
        if (key.escape) {
          setProbe(null);
          go(inputBefore());
        } else if (ch === "r") {
          runProbe(reuse);
        } else if (probe?.ok && key.return) {
          go("model");
        } else if (probe && !probe.ok && ch === "s") {
          go("confirm");
        }

        return;

      case "confirm":
        if (ch === "y" || ch === "Y") {
          setQuery("");
          go("model");
        } else if (key.escape || ch === "n" || ch === "N") {
          go("test");
        }

        return;

      case "model":
        if (key.escape) {
          go("test");
        } else if (key.upArrow && modelIdx > 0) {
          setModelIdx((v) => v - 1);
        } else if (key.downArrow && modelIdx < models.length - 1) {
          setModelIdx((v) => v + 1);
        } else if (key.return) {
          const pick = models[modelIdx] ?? query.trim();

          if (pick) {
            setModel(pick);
            go("review");
          }
        } else if (!key.upArrow && !key.downArrow) {
          editText(ch, key, setQuery);
          setModelIdx(0);
        }

        return;

      case "review":
        if (key.escape) {
          go("model");
        } else if (key.return) {
          save();
        }

        return;
    }
  });

  const title = (label: string) => (
    <Text bold color={t.color.accent} wrap="truncate-end">
      Add provider · {label}
    </Text>
  );

  const field = (value: string) => (
    <Text color={t.color.accent} wrap="truncate-end">
      {"  "}
      {value || "(empty)"}
      {busy ? "" : "▎"}
    </Text>
  );

  const status = err ? (
    <Text color={t.color.label} wrap="truncate-end">
      error: {err}
    </Text>
  ) : busy ? (
    <Text color={t.color.muted} wrap="truncate-end">
      {step === "test" ? "testing…" : "saving…"}
    </Text>
  ) : null;

  const body = (() => {
    switch (step) {
      case "preset": {
        const { items, offset } = windowItems(
          presets.map((p) => `${p.id} · ${p.label}`),
          presetIdx,
          VISIBLE_PRESETS,
        );

        return (
          <>
            {title("kind")}
            {!presets.length && !err ? (
              <Text color={t.color.muted}>loading presets…</Text>
            ) : null}
            {items.map((label, i) => (
              <Text
                color={t.color.muted}
                {...chipRowProps(t, offset + i === presetIdx)}
                key={label}
                wrap="truncate-end"
              >
                {offset + i === presetIdx ? "▸ " : "  "}
                {label}
              </Text>
            ))}
            {status}
            <OverlayHint t={t}>{"↑/↓ · Enter next · Esc cancel"}</OverlayHint>
          </>
        );
      }

      case "name":
        return (
          <>
            {title("name")}
            {field(name)}
            {status}
            <OverlayHint t={t}>Enter next · Esc back</OverlayHint>
          </>
        );

      case "url":
        return (
          <>
            {title("base URL")}
            {field(url)}
            {status}
            <OverlayHint t={t}>Enter next · ^U clear · Esc back</OverlayHint>
          </>
        );

      case "reuse":
        return (
          <>
            {title("API key")}
            <Text wrap="truncate-end">
              {preset?.key_env} already holds a key.
            </Text>
            <Text wrap="truncate-end">Reuse it for {name}?</Text>
            {status}
            <OverlayHint t={t}>y reuse · n new key · Esc back</OverlayHint>
          </>
        );

      case "key":
        return (
          <>
            {title("API key")}
            {field(apiKey ? "•".repeat(Math.min(apiKey.length, 40)) : "")}
            <Text color={t.color.muted} wrap="truncate-end">
              paste it; stored 0600, never shown
            </Text>
            {status}
            <OverlayHint t={t}>Enter test · Esc back</OverlayHint>
          </>
        );

      case "test":
        return (
          <>
            {title("test connection")}
            <Text wrap="truncate-end">{trimUrl(url)}</Text>
            {probe ? (
              <Text
                color={probe.ok ? t.color.accent : t.color.label}
                wrap="truncate-end"
              >
                {probe.ok
                  ? `OK · ${probe.models.length} models (${probe.ms} ms)`
                  : `FAIL: ${probe.detail}`}
              </Text>
            ) : null}
            {probe && !probe.ok ? (
              <Text color={t.color.muted} wrap="truncate-end">
                {probe.hint}
              </Text>
            ) : null}
            {status}
            <OverlayHint t={t}>
              {probe?.ok
                ? "Enter model · r retest · Esc back"
                : "r retry · s save anyway · Esc back"}
            </OverlayHint>
          </>
        );

      case "confirm":
        return (
          <>
            {title("save untested?")}
            <Text wrap="truncate-end">
              The endpoint did not answer the test.
            </Text>
            <Text wrap="truncate-end">Save {name} anyway?</Text>
            <OverlayHint t={t}>y save anyway · n back</OverlayHint>
          </>
        );

      case "model": {
        const { items, offset } = windowItems(models, modelIdx, VISIBLE_MODELS);

        return (
          <>
            {title("default model")}
            <Text
              color={probe?.ok ? t.color.accent : t.color.muted}
              wrap="truncate-end"
            >
              {probe?.ok
                ? `connection OK · ${probe.models.length} models (${probe.ms} ms)`
                : "untested"}
            </Text>
            <Text wrap="truncate-end">
              {"search: "}
              {query}
              {"▎"}
            </Text>
            {items.map((m, i) => (
              <Text
                color={t.color.muted}
                {...chipRowProps(t, offset + i === modelIdx)}
                key={m}
                wrap="truncate-end"
              >
                {offset + i === modelIdx ? "▸ " : "  "}
                {m}
              </Text>
            ))}
            {!models.length ? (
              <Text color={t.color.muted} wrap="truncate-end">
                {probe?.ok ? "no match: Enter uses it" : "type the model id"}
              </Text>
            ) : null}
            {status}
            <OverlayHint t={t}>{"↑/↓ · Enter pick · Esc back"}</OverlayHint>
          </>
        );
      }

      case "review":
        return (
          <>
            {title("save")}
            <Text wrap="truncate-end">
              {name} · {preset?.kind} · {model}
            </Text>
            <Text wrap="truncate-end">{trimUrl(url)}</Text>
            <Text color={t.color.muted} wrap="truncate-end">
              {preset?.keyless
                ? "no key"
                : reuse
                  ? `key: reuse ${preset?.key_env}`
                  : "key: saved 0600"}
              {probe?.ok ? "" : " · untested"}
            </Text>
            {status}
            <OverlayHint t={t}>Enter save · Esc back</OverlayHint>
          </>
        );
    }
  })();

  return (
    <Box flexDirection="column" width={width}>
      {body}
    </Box>
  );
}

interface ProviderAddFormProps {
  existingNames: readonly string[];
  gw: GatewayClient;
  locked: boolean;
  onCancel: () => void;
  onSaved: (slug: string) => void;
  sessionId: string | null;
  t: Theme;
  width: number;
}
