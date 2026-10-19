import { Box, Text, useInput, useStdout } from "@k3code/ink";
import type { ModelOptionsResult } from "@k3code/shared/gateway-events";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";
import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import {
  isAddProviderRow,
  initialProviderIdx,
  pickerNeedsKey,
  providerPickerRows,
  providerRowLabel,
  type ProviderPickerEntry,
} from "../domain/providers.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint, windowItems } from "./overlayControls.js";
import { ProviderAddForm } from "./providerAddForm.js";
import { chipRowProps, clampOverlayWidth } from "./overlayPrimitives.js";

const VISIBLE = 12;
const MIN_WIDTH = 40;
const MAX_WIDTH = 90;

/**
 * /provider: its own popup menu (issue #421). Lists the provider blocks from
 * `model.options`, marks the active one (`*`), and switches the session to
 * the highlighted block on Enter. `Ctrl+G` toggles `--save` (also write it
 * as `default_provider`); `/provider <name> [--save]` typed still goes to
 * the gateway. "+ Add new provider" opens the add form (providerAddForm.tsx)
 * in the popup; after a save the list comes back with the new block
 * highlighted, to switch to now (Enter) or later (Esc). Stays shut while a
 * prompt waits for an answer.
 */
type Stage = "add" | "key" | "list";

export function ProviderPicker({
  gw,
  maxWidth,
  onCancel,
  onNotice,
  onSelect,
  sessionId,
  t,
}: ProviderPickerProps) {
  const [providers, setProviders] = useState<ProviderPickerEntry[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [idx, setIdx] = useState(0);
  const [saveDefault, setSaveDefault] = useState(false);
  const [stage, setStage] = useState<Stage>("list");
  const [keyInput, setKeyInput] = useState("");
  const [keySaving, setKeySaving] = useState(false);
  const [keyError, setKeyError] = useState("");
  const [added, setAdded] = useState("");
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();

  const width = clampOverlayWidth(
    Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6)),
    maxWidth,
  );

  const fetchOptions = (focus = "") => {
    setLoading(true);
    gw.request<ModelOptionsResult>("model.options", {
      ...(sessionId ? { session_id: sessionId } : {}),
      include_unconfigured: true,
    })
      .then((raw) => {
        const r = asRpcResult<ModelOptionsResult>(raw);

        if (!r) {
          setErr("invalid response: model.options");
          setLoading(false);

          return;
        }

        const next = (r.providers ?? []) as ProviderPickerEntry[];
        setProviders(next);
        const at = focus ? next.findIndex((p) => p.slug === focus) : -1;
        setIdx(at >= 0 ? at : initialProviderIdx(next));
        setErr("");
        setLoading(false);
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e));
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchOptions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gw, sessionId]);

  const rows = useMemo(() => providerPickerRows(providers), [providers]);

  const keyEntry = rows[idx]?.entry;

  // Own key handling (no useOverlayKeys): while a prompt is open the popup
  // ignores every key so the prompt owns the keyboard, like TunePicker.
  // Key stage (inline API-key paste, ModelPicker pattern): raw useInput,
  // printable append, backspace delete, Ctrl+U clear, Enter submit, Esc back.
  useInput((ch, key) => {
    if (locked || stage === "add") {
      return;
    }

    if (stage === "key") {
      if (keySaving) {
        return;
      }

      if (key.escape) {
        setKeyInput("");
        setKeyError("");
        setStage("list");

        return;
      }

      if (key.return) {
        if (!keyInput.trim() || !keyEntry || isAddProviderRow(keyEntry)) {
          return;
        }

        setKeySaving(true);
        setKeyError("");
        gw.request<{ provider?: ProviderPickerEntry }>("model.save_key", {
          slug: keyEntry.slug,
          api_key: keyInput.trim(),
          ...(sessionId ? { session_id: sessionId } : {}),
        })
          .then((raw) => {
            const r = asRpcResult<{ provider?: ProviderPickerEntry }>(raw);

            if (!r?.provider) {
              setKeyError("failed to save key");
              setKeySaving(false);

              return;
            }

            setKeyInput("");
            setKeySaving(false);
            setStage("list");
            fetchOptions();
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

      if (ch === "" || (key.ctrl && ch === "u")) {
        setKeyInput("");

        return;
      }

      if (ch && !key.ctrl && !key.meta) {
        setKeyInput((v) => v + ch);
      }

      return;
    }

    if (key.escape) {
      onCancel();

      return;
    }

    if (ch === "q" && !key.ctrl && !key.meta) {
      onCancel();

      return;
    }

    if (key.upArrow && idx > 0) {
      setIdx((v) => v - 1);

      return;
    }

    if (key.downArrow && idx < rows.length - 1) {
      setIdx((v) => v + 1);

      return;
    }

    if (key.ctrl && ch === "g") {
      setSaveDefault((v) => !v);

      return;
    }

    if (key.return) {
      const row = rows[idx];

      if (!row) {
        return;
      }

      if (isAddProviderRow(row.entry)) {
        setAdded("");
        setStage("add");

        return;
      }

      if (pickerNeedsKey(row.entry)) {
        setKeyInput("");
        setKeyError("");
        setStage("key");

        return;
      }

      if (row.entry.authenticated === false) {
        // Other auth types: no-op (the row warning names the remedy).
        return;
      }

      onSelect(row.entry.slug, saveDefault);

      return;
    }

    const n = parseInt(ch, 10);

    if (n >= 1 && n <= rows.length) {
      const row = rows[n - 1];

      if (!row) {
        return;
      }

      if (isAddProviderRow(row.entry)) {
        setAdded("");
        setStage("add");

        return;
      }

      if (pickerNeedsKey(row.entry)) {
        setKeyInput("");
        setKeyError("");
        setStage("key");
        setIdx(n - 1);

        return;
      }

      if (row.entry.authenticated === false) {
        return;
      }

      onSelect(row.entry.slug, saveDefault);
    }
  });

  if (loading) {
    return <Text color={t.color.muted}>loading providers…</Text>;
  }

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  if (!rows.length) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.muted}>no providers configured</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  if (stage === "add") {
    return (
      <ProviderAddForm
        existingNames={providers.map((p) => p.slug)}
        gw={gw}
        locked={locked}
        onCancel={() => setStage("list")}
        onClosedNotice={onNotice}
        onSaved={(slug) => {
          setStage("list");
          setAdded(slug);
          fetchOptions(slug);
        }}
        sessionId={sessionId}
        t={t}
        width={width}
      />
    );
  }

  if (stage === "key" && keyEntry && !isAddProviderRow(keyEntry)) {
    const masked = keyInput
      ? "\u2022".repeat(Math.min(keyInput.length, 40))
      : "";

    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent} wrap="truncate-end">
          Paste API key for {keyEntry.name} ({keyEntry.key_env}):
        </Text>

        <Text color={t.color.accent} wrap="truncate-end">
          {"  "}
          {masked || "(empty)"}
          {keySaving ? "" : "\u258e"}
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
            saving\u2026
          </Text>
        ) : (
          <Text color={t.color.muted} wrap="truncate-end">
            {" "}
          </Text>
        )}

        <OverlayHint t={t}>Enter save \u00b7 Esc back</OverlayHint>
      </Box>
    );
  }

  const { items, offset } = windowItems(
    rows.map((r) => providerRowLabel(r)),
    idx,
    VISIBLE,
  );
  const current = rows[idx]?.entry;

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Select provider
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        Switch this session to another provider block
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {offset > 0 ? ` ↑ ${offset} more` : " "}
      </Text>

      {Array.from({ length: Math.min(VISIBLE, rows.length) }, (_, i) => {
        const label = items[i];
        const rowIdx = offset + i;

        return label ? (
          <Text
            color={t.color.muted}
            {...chipRowProps(t, rowIdx === idx)}
            key={`${rows[rowIdx]?.entry.slug ?? rowIdx}`}
            wrap="truncate-end"
          >
            {rowIdx === idx ? "▸ " : "  "}
            {rowIdx + 1}. {label}
          </Text>
        ) : null;
      })}

      <Text color={t.color.muted} wrap="truncate-end">
        {offset + VISIBLE < rows.length
          ? ` ↓ ${rows.length - offset - VISIBLE} more`
          : " "}
      </Text>

      {added && current?.slug === added ? (
        <Text color={t.color.accent} wrap="truncate-end">
          added {added} · Enter use now · Esc later
        </Text>
      ) : null}

      {current?.warning ? (
        <Text color={t.color.label} wrap="truncate-end">
          warning: {current.warning}
        </Text>
      ) : null}

      <Text color={t.color.muted} wrap="truncate-end">
        persist: {saveDefault ? "session + default" : "session only"} · ^g
        toggle save
      </Text>
      <OverlayHint t={t}>
        ↑/↓ select · Enter switch/setup · ^g save toggle · Esc/q close
      </OverlayHint>
    </Box>
  );
}

interface ProviderPickerProps {
  gw: GatewayClient;
  maxWidth?: number;
  onCancel: () => void;
  /** A system line for an add-form save that ended after the popup closed (Ctrl+C while saving). */
  onNotice?: (text: string) => void;
  onSelect: (slug: string, saveDefault: boolean) => void;
  sessionId: string | null;
  t: Theme;
}
