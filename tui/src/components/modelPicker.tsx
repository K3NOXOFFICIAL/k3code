import { Box, Text, useInput, useStdout } from "@k3code/ink";
import { fuzzyRank, fuzzyScoreMulti } from "@k3code/shared/fuzzy";
import type {
  ModelOptionProvider,
  ModelOptionsResult,
} from "@k3code/shared/gateway-events";
import { modelSearchText } from "@k3code/shared/model-search-text";
import { REASONING_EFFORTS } from "@k3code/shared/reasoning-effort";
import { useEffect, useMemo, useState } from "react";

import { providerDisplayNames } from "../domain/providers.js";
import { TUI_SESSION_MODEL_FLAG } from "../domain/slash.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint, useOverlayKeys, windowItems } from "./overlayControls.js";
import { chipRowProps, clampOverlayWidth } from "./overlayPrimitives.js";

const VISIBLE = 12;
const MIN_WIDTH = 40;
const MAX_WIDTH = 90;

type Stage = "provider" | "key" | "model" | "reasoning" | "disconnect";

type ProviderRow = { name: string; provider: ModelOptionProvider };

/** Rows of the effort step (step 3/3): the shared ladder, the off state, then
 *  "keep current" (empty value = no `--reasoning` flag on the emitted command). */
export const REASONING_PICKER_ROWS: ReadonlyArray<{
  label: string;
  value: string;
}> = [
  ...REASONING_EFFORTS.map((level) => ({ label: level, value: level })),
  { label: "none (disable reasoning)", value: "none" },
  { label: "Keep current effort", value: "" },
];

/** False only when the catalog says the picked model has no reasoning control;
 *  unknown capabilities keep the step (a no-op dial beats hiding a real one). */
export function pickerOffersReasoning(
  provider: ModelOptionProvider | undefined,
  model: string,
): boolean {
  return provider?.capabilities?.[model]?.reasoning !== false;
}

/** The `/model` argument the picker emits: model + provider + scope, plus
 *  `--reasoning <level>` when an effort was picked. */
export function modelPickerCommand(
  model: string,
  providerSlug: string,
  persistGlobal: boolean,
  reasoning = "",
): string {
  const scope = persistGlobal ? "--global" : TUI_SESSION_MODEL_FLAG;
  const effort = reasoning ? ` --reasoning ${reasoning}` : "";

  return `${model} --provider ${providerSlug}${effort} ${scope}`;
}

export function providerIndexAfterClearingFilter(
  providerRows: ProviderRow[],
  provider: ModelOptionProvider | undefined,
) {
  if (!provider) {
    return -1;
  }

  return providerRows.findIndex((row) => row.provider.slug === provider.slug);
}

/** Where the model cursor starts: on the session's current model key when
 *  this provider lists it, else on the first row. */
export function initialModelIdx(
  provider: ModelOptionProvider | undefined,
  currentModel: string,
): number {
  return Math.max(0, (provider?.models ?? []).indexOf(currentModel));
}

/** The config `descriptions` the gateway sends per model key (not part of the
 *  generated contract type, so read defensively). */
export function modelDescriptions(
  provider: ModelOptionProvider | undefined,
): Record<string, string> {
  const raw = provider?.descriptions;

  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {};
  }

  return Object.fromEntries(
    Object.entries(raw).filter(
      (e): e is [string, string] => typeof e[1] === "string" && !!e[1],
    ),
  );
}

/** One model row: the key, the model id it sends when that differs, and the
 *  config description (`cheap (gpt-4o-mini) · Fast and cheap`). With
 *  `providerName` the key is prefixed (`openai/cheap`), as in the search list. */
export function modelRowLabel(
  provider: ModelOptionProvider,
  model: string,
  providerName?: string,
): string {
  const id = provider.model_ids?.[model];
  const description = modelDescriptions(provider)[model];
  const head = providerName ? `${providerName}/${model}` : model;
  const withId = id && id !== model ? `${head} (${id})` : head;

  return description ? `${withId} · ${description}` : withId;
}

/** A row of the first screen's search: one model of one provider. */
export type ModelHit = {
  model: string;
  name: string;
  provider: ModelOptionProvider;
};

/** Typing on the first screen searches every provider's models at once: a
 *  flat `provider/model` list, best match first. Each candidate text (the
 *  `provider/key` pair by display name and by slug, the key, and the model id
 *  it sends, the last two with their search aliases) is scored on its own and
 *  the best one counts: in one long haystack the subsequence matcher found
 *  "opus" in "anthropic/claude-sonnet-5". Descriptions are shown but not
 *  searched. */
export function flatModelHits(
  providerRows: ProviderRow[],
  query: string,
): ModelHit[] {
  const hits = providerRows.flatMap(({ name, provider }) =>
    (provider.models ?? []).map((model) => ({ model, name, provider })),
  );

  if (!query.trim()) {
    return hits;
  }

  const scored: { hit: ModelHit; index: number; score: number }[] = [];

  hits.forEach((hit, index) => {
    const id = hit.provider.model_ids?.[hit.model] ?? "";
    const texts = [
      `${hit.name}/${hit.model}`,
      `${hit.provider.slug}/${hit.model}`,
      modelSearchText(hit.model),
      ...(id ? [modelSearchText(id)] : []),
    ];
    let best: number | null = null;

    for (const text of texts) {
      const match = fuzzyScoreMulti(text, query);

      if (match && (best === null || match.score > best)) {
        best = match.score;
      }
    }

    if (best !== null) {
      scored.push({ hit, index, score: best });
    }
  });

  scored.sort((x, y) => y.score - x.score || x.index - y.index);

  return scored.map((x) => x.hit);
}

/** A provider whose key is missing and can be pasted here (`model.save_key`). */
export function pickerNeedsKey(provider: ModelOptionProvider | undefined) {
  return (
    provider?.authenticated === false &&
    provider.auth_type === "api_key" &&
    !!provider.key_env
  );
}

/** Ctrl+D applies: a provider with a saved key (claude-cli and keyless
 *  endpoints have nothing to remove). */
export function pickerCanDisconnect(provider: ModelOptionProvider | undefined) {
  return (
    provider?.auth_type === "api_key" &&
    !!provider.key_env &&
    provider.authenticated !== false
  );
}

export function ModelPicker({
  allowPersistGlobal = true,
  gw,
  initialRefresh = false,
  maxWidth,
  onCancel,
  onSelect,
  sessionId,
  t,
}: ModelPickerProps) {
  const [providers, setProviders] = useState<ModelOptionProvider[]>([]);
  const [currentModel, setCurrentModel] = useState("");
  const [currentProvider, setCurrentProvider] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [persistGlobal, setPersistGlobal] = useState(false);
  const [providerIdx, setProviderIdx] = useState(0);
  const [modelIdx, setModelIdx] = useState(0);
  const [hitIdx, setHitIdx] = useState(0);
  const [reasoningIdx, setReasoningIdx] = useState(0);
  // Model chosen on the model step (or from the search), awaiting the effort pick.
  const [pendingModel, setPendingModel] = useState("");
  const [stage, setStage] = useState<Stage>("provider");
  const [keyInput, setKeyInput] = useState("");
  const [keySaving, setKeySaving] = useState(false);
  const [keyError, setKeyError] = useState("");
  // Type-to-filter query, scoped per stage (cleared on stage change). On the
  // first screen it searches every provider's models (flatModelHits).
  const [filter, setFilter] = useState("");

  const { stdout } = useStdout();
  // Pin the picker to a stable width so the FloatBox parent (which shrinks-
  // to-fit with alignSelf="flex-start") doesn't resize as long provider /
  // model names scroll into view, and so `wrap="truncate-end"` on each row
  // has an actual constraint to truncate against. Optional maxWidth lets
  // grid layouts hand the picker its cell budget.
  const preferredWidth = Math.max(
    MIN_WIDTH,
    Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6),
  );
  const width = clampOverlayWidth(preferredWidth, maxWidth);

  useEffect(() => {
    gw.request<ModelOptionsResult>("model.options", {
      ...(sessionId ? { session_id: sessionId } : {}),
      ...(initialRefresh ? { refresh: true } : {}),
      // The TUI picker shows the full provider universe with setup
      // affordances ("paste KEY to activate"), so opt into unconfigured
      // rows — the backend now defaults to the configured subset for
      // desktop chat pickers (#56974).
      include_unconfigured: true,
    })
      .then((raw) => {
        const r = asRpcResult<ModelOptionsResult>(raw);

        if (!r) {
          setErr("invalid response: model.options");
          setLoading(false);

          return;
        }

        const next = r.providers ?? [];
        const model = String(r.model ?? "");
        const current = Math.max(
          0,
          next.findIndex((p) => p.is_current),
        );
        const only = next.length === 1 ? next[0] : undefined;
        setProviders(next);
        setCurrentModel(model);
        setCurrentProvider(String(r.provider ?? next[current]?.slug ?? ""));
        setProviderIdx(current);
        // One provider: there is nothing to choose on step 1, so open on its
        // models (or its key prompt) with the cursor on the current model.
        setModelIdx(initialModelIdx(next[current], model));
        setStage(!only ? "provider" : pickerNeedsKey(only) ? "key" : "model");
        setErr("");
        setLoading(false);
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e));
        setLoading(false);
      });
  }, [gw, initialRefresh, sessionId]);

  const names = useMemo(() => providerDisplayNames(providers), [providers]);
  const singleProvider = providers.length === 1;

  // Provider rows carry their display name so fuzzy filtering can match on
  // name + slug while keeping the name/provider pairing intact across ranking.
  const providerRows = useMemo(
    () =>
      providers.map((p, i) => ({
        provider: p,
        name: names[i] ?? p.name ?? p.slug,
      })),
    [providers, names],
  );

  // Typing on the first screen switches it to a flat search over every
  // provider's models: `provider/model`, best match first.
  const searching = stage === "provider" && !!filter.trim();
  const hits = useMemo(
    () => (searching ? flatModelHits(providerRows, filter) : []),
    [providerRows, filter, searching],
  );

  const provider = providerRows[providerIdx]?.provider;
  const allModels = useMemo(() => provider?.models ?? [], [provider]);

  const filteredModels = useMemo(() => {
    if (stage !== "model" || !filter.trim()) {
      return allModels;
    }

    // modelSearchText adds aliases for brand-less wire ids (e.g. Kimi
    // Coding `k3` still matches a "kimi" query); the model id it sends
    // counts too.
    return fuzzyRank(allModels, filter, (m) => {
      const id = provider?.model_ids?.[m];

      return id ? `${modelSearchText(m)} ${modelSearchText(id)}` : m;
    }).map((r) => r.item);
  }, [allModels, filter, stage, provider]);

  const models = filteredModels;

  // Keep the active selection within the (possibly filtered) list bounds.
  useEffect(() => {
    if (hitIdx >= hits.length && hits.length > 0) {
      setHitIdx(0);
    }
  }, [hits.length, hitIdx]);

  useEffect(() => {
    if (modelIdx >= models.length && models.length > 0) {
      setModelIdx(0);
    }
  }, [models.length, modelIdx]);

  /** Open a provider's models with the cursor on the current model. */
  const openModels = (idx: number) => {
    setProviderIdx(idx);
    setModelIdx(initialModelIdx(providerRows[idx]?.provider, currentModel));
    setStage("model");
    setFilter("");
  };

  /** Step 3 for a model that may take an effort level, else switch now. */
  const chooseModel = (p: ModelOptionProvider, model: string) => {
    if (pickerOffersReasoning(p, model)) {
      setPendingModel(model);
      setReasoningIdx(0);
      setStage("reasoning");

      return;
    }

    onSelect(
      modelPickerCommand(model, p.slug, allowPersistGlobal && persistGlobal),
    );
  };

  const back = () => {
    // Esc first clears an active filter on the list stages, before navigating.
    if ((stage === "provider" || stage === "model") && filter.trim()) {
      setFilter("");
      setHitIdx(0);

      if (stage === "model") {
        setModelIdx(initialModelIdx(provider, currentModel));
      }

      return;
    }

    if (stage === "reasoning") {
      setStage("model");
      setPendingModel("");
      setReasoningIdx(0);

      return;
    }

    // With one provider there is no provider step to go back to.
    if (
      !singleProvider &&
      (stage === "model" || stage === "key" || stage === "disconnect")
    ) {
      setStage("provider");
      setKeyInput("");
      setKeyError("");
      setKeySaving(false);
      setFilter("");

      return;
    }

    if (singleProvider && stage === "disconnect") {
      setStage("model");

      return;
    }

    onCancel();
  };

  // On the list stages we capture printable keys (including 'q') into the
  // filter, so the shared overlay q/Esc handler must yield to our own handler.
  const listStage =
    stage === "provider" || stage === "model" || stage === "reasoning";
  useOverlayKeys({ disabled: listStage, onBack: back, onClose: onCancel });

  /** Replace one provider row with the gateway's fresh copy of it. */
  const replaceProvider = (fresh: ModelOptionProvider | undefined) => {
    if (fresh) {
      setProviders((prev) =>
        prev.map((p) => (p.slug === fresh.slug ? fresh : p)),
      );
    }
  };

  const startDisconnect = (target: ModelOptionProvider | undefined) => {
    if (!pickerCanDisconnect(target)) {
      return;
    }

    const idx = providerIndexAfterClearingFilter(providerRows, target);

    if (idx >= 0) {
      setProviderIdx(idx);
    }

    setKeyError("");
    setStage("disconnect");
    setFilter("");
  };

  useInput((ch, key) => {
    // Key entry stage handles its own input
    if (stage === "key") {
      if (keySaving) {
        return;
      }

      if (key.return) {
        if (!keyInput.trim() || !provider) {
          return;
        }

        setKeySaving(true);
        setKeyError("");
        // ModelSaveKeyParams: { slug, api_key } (gateway-contract.generated.ts).
        gw.request<{ provider?: ModelOptionProvider }>("model.save_key", {
          slug: provider.slug,
          api_key: keyInput.trim(),
          ...(sessionId ? { session_id: sessionId } : {}),
        })
          .then((raw) => {
            const r = asRpcResult<{ provider?: ModelOptionProvider }>(raw);

            if (!r?.provider) {
              setKeyError("failed to save key");
              setKeySaving(false);

              return;
            }

            replaceProvider(r.provider);
            setKeyInput("");
            setKeySaving(false);
            setStage("model");
            setModelIdx(initialModelIdx(r.provider, currentModel));
          })
          .catch((e: unknown) => {
            setKeyError(rpcErrorMessage(e));
            setKeySaving(false);
          });

        return;
      }

      if (key.backspace || key.delete) {
        setKeyInput((v) => v.slice(0, -1));

        return;
      }

      // ctrl+u clears input
      if (ch === "\u0015" || (key.ctrl && ch === "u")) {
        setKeyInput("");

        return;
      }

      if (ch && !key.ctrl && !key.meta) {
        setKeyInput((v) => v + ch);
      }

      return;
    }

    // Disconnect confirmation stage
    if (stage === "disconnect") {
      if (keySaving) {
        return;
      }

      if (ch.toLowerCase() === "y" || key.return) {
        if (!provider) {
          back();

          return;
        }

        setKeySaving(true);
        setKeyError("");
        gw.request<{ disconnected?: boolean; provider?: ModelOptionProvider }>(
          "model.disconnect",
          {
            slug: provider.slug,
            ...(sessionId ? { session_id: sessionId } : {}),
          },
        )
          .then((raw) => {
            const r = asRpcResult<{
              disconnected?: boolean;
              provider?: ModelOptionProvider;
            }>(raw);

            setKeySaving(false);

            if (!r?.disconnected) {
              setKeyError("the key was not removed");

              return;
            }

            replaceProvider(r.provider);
            back();
          })
          .catch((e: unknown) => {
            // Stay on the confirmation and say why (e.g. the key comes from
            // the daemon's environment): never pretend it was removed.
            setKeySaving(false);
            setKeyError(rpcErrorMessage(e));
          });

        return;
      }

      if (ch.toLowerCase() === "n" || key.escape) {
        setKeyError("");
        back();

        return;
      }

      return;
    }

    // Effort stage (step 3/3): plain arrow list, no filter.
    if (stage === "reasoning") {
      if (key.escape) {
        back();

        return;
      }

      if (ch === "q") {
        onCancel();

        return;
      }

      if (key.upArrow && reasoningIdx > 0) {
        setReasoningIdx((v) => v - 1);

        return;
      }

      if (key.downArrow && reasoningIdx < REASONING_PICKER_ROWS.length - 1) {
        setReasoningIdx((v) => v + 1);

        return;
      }

      if (allowPersistGlobal && key.ctrl && ch === "g") {
        setPersistGlobal((v) => !v);

        return;
      }

      if (key.return && provider && pendingModel) {
        onSelect(
          modelPickerCommand(
            pendingModel,
            provider.slug,
            allowPersistGlobal && persistGlobal,
            REASONING_PICKER_ROWS[reasoningIdx]?.value ?? "",
          ),
        );
      }

      return;
    }

    // List-stage Esc/q handling (overlay keys are disabled while on a list
    // stage so 'q' can be typed into the filter).
    if (key.escape) {
      back();

      return;
    }

    if (ch === "q" && !filter) {
      onCancel();

      return;
    }

    const count =
      stage === "model"
        ? models.length
        : searching
          ? hits.length
          : providerRows.length;
    const sel = stage === "model" ? modelIdx : searching ? hitIdx : providerIdx;
    const setSel =
      stage === "model" ? setModelIdx : searching ? setHitIdx : setProviderIdx;

    if (key.upArrow && sel > 0) {
      setSel((v) => v - 1);

      return;
    }

    if (key.downArrow && sel < count - 1) {
      setSel((v) => v + 1);

      return;
    }

    if (key.return) {
      if (stage === "provider" && searching) {
        const hit = hits[hitIdx];

        if (!hit) {
          return;
        }

        const idx = providerIndexAfterClearingFilter(
          providerRows,
          hit.provider,
        );

        if (idx >= 0) {
          setProviderIdx(idx);
        }

        setFilter("");

        if (pickerNeedsKey(hit.provider)) {
          setStage("key");
          setKeyInput("");
          setKeyError("");

          return;
        }

        setModelIdx(
          Math.max(0, (hit.provider.models ?? []).indexOf(hit.model)),
        );
        chooseModel(hit.provider, hit.model);

        return;
      }

      if (stage === "provider") {
        if (!provider) {
          return;
        }

        if (provider.authenticated === false) {
          // api_key providers: prompt for key inline
          if (pickerNeedsKey(provider)) {
            setStage("key");
            setKeyInput("");
            setKeyError("");
            setFilter("");
          }

          // Other auth types: no-op (warning shown tells them to run k3code model)
          return;
        }

        openModels(providerIdx);

        return;
      }

      const model = models[modelIdx];

      if (provider && model) {
        if (pickerNeedsKey(provider)) {
          setStage("key");
          setKeyInput("");
          setKeyError("");
          setFilter("");

          return;
        }

        chooseModel(provider, model);
      } else if (!singleProvider) {
        setStage("provider");
      }

      return;
    }

    // Backspace removes the last filter character; Esc (above) clears a
    // non-empty filter before navigating back.
    if (key.backspace || key.delete) {
      setFilter((v) => v.slice(0, -1));
      setSel(0);

      return;
    }

    // Ctrl+U clears the filter. (Ctrl held → ch is the key name 'u'.)
    if (key.ctrl && ch === "u") {
      setFilter("");
      setSel(0);

      return;
    }

    // Persist-global toggle moved to Ctrl+G so 'g' can be typed into the
    // filter. With Ctrl held, @k3code/ink reports `ch` as the key name ('g'),
    // not the raw control byte (see input-event.ts: input = ctrl ? name : seq).
    if (allowPersistGlobal && key.ctrl && ch === "g") {
      setPersistGlobal((v) => !v);

      return;
    }

    // Disconnect (Ctrl+D): the highlighted provider (or the search hit's), or
    // the only provider from its model list.
    if (key.ctrl && ch === "d") {
      if (stage === "provider") {
        startDisconnect(searching ? hits[hitIdx]?.provider : provider);
      } else if (singleProvider) {
        startDisconnect(provider);
      }

      return;
    }

    // Any other printable single character extends the filter.
    if (ch && !key.ctrl && !key.meta && ch.length === 1 && ch >= " ") {
      setFilter((v) => v + ch);
      setSel(0);
    }
  });

  if (loading) {
    return <Text color={t.color.muted}>loading models…</Text>;
  }

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  if (!providers.length) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.muted}>no providers available</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  const totalSteps = singleProvider ? 2 : 3;
  const stepOf = (n: number) =>
    `step ${singleProvider ? n - 1 : n}/${totalSteps}`;
  const persistLine = (
    <Text color={t.color.muted} wrap="truncate-end">
      persist:{" "}
      {allowPersistGlobal ? (persistGlobal ? "global" : "session") : "session"}
      {allowPersistGlobal ? " · ^g toggle" : " only"}
    </Text>
  );

  // ── Key entry stage ──────────────────────────────────────────────────
  if (stage === "key" && provider) {
    const masked = keyInput ? "•".repeat(Math.min(keyInput.length, 40)) : "";

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Configure {provider.name}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Paste your API key below (saved 0600 to ~/.config/k3code/env)
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {" "}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {provider.key_env}:
        </Text>

        <Text color={t.color.accent} wrap="truncate-end">
          {"  "}
          {masked || "(empty)"}
          {keySaving ? "" : "▎"}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {" "}
        </Text>

        {keyError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {keyError}
          </Text>
        ) : keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            saving…
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {" "}
          </Text>
        )}

        <OverlayHint t={t}>Enter save · Ctrl+U clear · Esc back</OverlayHint>
      </Box>
    );
  }

  // ── Disconnect confirmation stage ─────────────────────────────────────
  if (stage === "disconnect" && provider) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Disconnect {provider.name}?
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {" "}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          This removes the saved key {provider.key_env} for {provider.name}.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Paste a key again later by selecting it.
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {" "}
        </Text>

        {keyError ? (
          <Text color={t.color.label} wrap="truncate-end">
            error: {keyError}
          </Text>
        ) : null}

        {keySaving ? (
          <Text color={t.color.muted} wrap="truncate-end">
            disconnecting…
          </Text>
        ) : (
          <OverlayHint t={t}>y/Enter confirm · n/Esc cancel</OverlayHint>
        )}
      </Box>
    );
  }

  // ── Provider selection stage, or the search over all models ──────────
  if (stage === "provider") {
    const rows = searching
      ? hits.map((h) => {
          const mark =
            h.provider.authenticated === false
              ? "○"
              : h.provider.slug === currentProvider && h.model === currentModel
                ? "*"
                : " ";

          return `${mark} ${modelRowLabel(h.provider, h.model, h.name)}`;
        })
      : providerRows.map(({ provider: p, name }) => {
          const authMark =
            p.authenticated === false ? "○" : p.is_current ? "*" : "●";
          const modelCount = p.total_models ?? p.models?.length ?? 0;

          const suffix =
            p.authenticated === false
              ? p.auth_type === "api_key"
                ? "(no key)"
                : "(needs setup)"
              : `${modelCount} models`;

          return `${authMark} ${name} · ${suffix}`;
        });

    const sel = searching ? hitIdx : providerIdx;
    const selProvider = searching ? hits[hitIdx]?.provider : provider;
    const { items, offset } = windowItems(rows, sel, VISIBLE);
    const noMatches = searching && rows.length === 0;

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          {searching
            ? "Search models in every provider"
            : `Select provider (${stepOf(1)})`}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {searching
            ? "provider/model · Enter picks it"
            : "Type to search all models · Enter to open a provider"}
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          Current: {currentModel || "(unknown)"}
          {currentProvider ? ` · ${currentProvider}` : ""}
        </Text>
        <Text
          color={filter ? t.color.accent : t.color.muted}
          wrap="truncate-end"
        >
          {filter ? `search: ${filter}▎` : "type to search · ↑/↓ select"}
        </Text>
        <Text color={t.color.label} wrap="truncate-end">
          {selProvider?.warning ? `warning: ${selProvider.warning}` : " "}
        </Text>
        <Text color={t.color.muted} wrap="truncate-end">
          {offset > 0 ? ` ↑ ${offset} more` : " "}
        </Text>

        {noMatches ? (
          <Text color={t.color.muted} wrap="truncate-end">
            no models match
          </Text>
        ) : (
          Array.from({ length: VISIBLE }, (_, i) => {
            const row = items[i];
            const idx = offset + i;
            const p = searching
              ? hits[idx]?.provider
              : providerRows[idx]?.provider;
            const dimmed = p?.authenticated === false;
            const rowKey = searching
              ? `${hits[idx]?.provider.slug}/${hits[idx]?.model}`
              : (p?.slug ?? `row-${idx}`);

            return row ? (
              <Text
                color={dimmed ? t.color.label : t.color.muted}
                {...chipRowProps(t, sel === idx)}
                key={rowKey}
                wrap="truncate-end"
              >
                {sel === idx ? "▸ " : "  "}
                {idx + 1}. {row}
              </Text>
            ) : (
              <Text color={t.color.muted} key={`pad-${i}`} wrap="truncate-end">
                {" "}
              </Text>
            );
          })
        )}

        <Text color={t.color.muted} wrap="truncate-end">
          {offset + VISIBLE < rows.length
            ? ` ↓ ${rows.length - offset - VISIBLE} more`
            : " "}
        </Text>

        {persistLine}
        <OverlayHint t={t}>
          ↑/↓ select · Enter choose · ^d disconnect · Esc clear/back · q close
        </OverlayHint>
      </Box>
    );
  }

  // ── Reasoning effort stage ───────────────────────────────────────────
  if (stage === "reasoning") {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Reasoning effort ({stepOf(3)})
        </Text>

        <Text color={t.color.muted} wrap="truncate-end">
          {pendingModel} · applies with the switch (same scope) · Esc back
        </Text>

        {REASONING_PICKER_ROWS.map((row, idx) => (
          <Text
            color={t.color.muted}
            {...chipRowProps(t, reasoningIdx === idx)}
            key={row.value || "keep"}
            wrap="truncate-end"
          >
            {reasoningIdx === idx ? "▸ " : "  "}
            {idx + 1}. {row.label}
          </Text>
        ))}

        {persistLine}
        <OverlayHint t={t}>
          ↑/↓ select · Enter switch · Esc back · q close
        </OverlayHint>
      </Box>
    );
  }

  // ── Model selection stage ────────────────────────────────────────────
  const { items, offset } = windowItems(models, modelIdx, VISIBLE);
  const noModelMatches = !!filter.trim() && models.length === 0;
  const isCurrent = (row: string) =>
    row === currentModel &&
    (!currentProvider || provider?.slug === currentProvider);

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Select model ({stepOf(2)})
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        {providerRows[providerIdx]?.name || "(unknown provider)"}
        {singleProvider ? " · Esc close" : " · Esc back"}
      </Text>
      <Text color={filter ? t.color.accent : t.color.muted} wrap="truncate-end">
        {filter ? `filter: ${filter}▎` : "type to filter · ↑/↓ select"}
      </Text>
      <Text color={t.color.label} wrap="truncate-end">
        {provider?.warning ? `warning: ${provider.warning}` : " "}
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {offset > 0 ? ` ↑ ${offset} more` : " "}
      </Text>

      {Array.from({ length: VISIBLE }, (_, i) => {
        const row = items[i];
        const idx = offset + i;

        if (!row) {
          return (!allModels.length || noModelMatches) && i === 0 ? (
            <Text color={t.color.muted} key="empty" wrap="truncate-end">
              {noModelMatches
                ? "no models match filter"
                : "no models listed for this provider"}
            </Text>
          ) : (
            <Text color={t.color.muted} key={`pad-${i}`} wrap="truncate-end">
              {" "}
            </Text>
          );
        }

        const prefix = modelIdx === idx ? "▸ " : isCurrent(row) ? "* " : "  ";

        return (
          <Text
            color={t.color.muted}
            {...chipRowProps(t, modelIdx === idx)}
            key={`${provider?.slug ?? "prov"}:${idx}:${row}`}
            wrap="truncate-end"
          >
            {prefix}
            {idx + 1}. {provider ? modelRowLabel(provider, row) : row}
          </Text>
        );
      })}

      <Text color={t.color.muted} wrap="truncate-end">
        {offset + VISIBLE < models.length
          ? ` ↓ ${models.length - offset - VISIBLE} more`
          : " "}
      </Text>

      {persistLine}
      <OverlayHint t={t}>
        {models.length
          ? singleProvider && pickerCanDisconnect(provider)
            ? "↑/↓ select · Enter next · ^d disconnect · Esc clear/close · q close"
            : "↑/↓ select · Enter next · Esc clear/back · q close"
          : "Esc back · q close"}
      </OverlayHint>
    </Box>
  );
}

interface ModelPickerProps {
  allowPersistGlobal?: boolean;
  gw: GatewayClient;
  initialRefresh?: boolean;
  maxWidth?: number;
  onCancel: () => void;
  onSelect: (value: string) => void;
  sessionId: string | null;
  t: Theme;
}
