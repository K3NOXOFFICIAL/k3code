import { Box, Text, useInput, useStdout } from "@k3code/ink";
import type { ModelOptionsResult } from "@k3code/shared/gateway-events";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";
import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import {
  initialProviderIdx,
  providerPickerRows,
  providerRowLabel,
  type ProviderPickerEntry,
} from "../domain/providers.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint, windowItems } from "./overlayControls.js";
import { chipRowProps, clampOverlayWidth } from "./overlayPrimitives.js";

const VISIBLE = 12;
const MIN_WIDTH = 40;
const MAX_WIDTH = 90;

/**
 * /provider: its own popup menu (issue #421). Lists the provider blocks from
 * `model.options`, marks the active one (`*`), and switches the session to
 * the highlighted block on Enter. `Ctrl+G` toggles `--save` (also write it
 * as `default_provider`); `/provider <name> [--save]` typed still goes to
 * the gateway. Stays shut while a prompt waits for an answer.
 */
export function ProviderPicker({
  gw,
  maxWidth,
  onCancel,
  onSelect,
  sessionId,
  t,
}: ProviderPickerProps) {
  const [providers, setProviders] = useState<ProviderPickerEntry[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [idx, setIdx] = useState(0);
  const [saveDefault, setSaveDefault] = useState(false);
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();

  const width = clampOverlayWidth(
    Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6)),
    maxWidth,
  );

  useEffect(() => {
    gw.request<ModelOptionsResult>("model.options", {
      ...(sessionId ? { session_id: sessionId } : {}),
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
        setIdx(initialProviderIdx(next));
        setErr("");
        setLoading(false);
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e));
        setLoading(false);
      });
  }, [gw, sessionId]);

  const rows = useMemo(() => providerPickerRows(providers), [providers]);

  // Own key handling (no useOverlayKeys): while a prompt is open the popup
  // ignores every key so the prompt owns the keyboard, like TunePicker.
  useInput((ch, key) => {
    if (locked) {
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

      if (row) {
        onSelect(row.entry.slug, saveDefault);
      }

      return;
    }

    const n = parseInt(ch, 10);

    if (n >= 1 && n <= rows.length) {
      const row = rows[n - 1];

      if (row) {
        onSelect(row.entry.slug, saveDefault);
      }
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
        ↑/↓ select · Enter switch · ^g save toggle · Esc/q close
      </OverlayHint>
    </Box>
  );
}

interface ProviderPickerProps {
  gw: GatewayClient;
  maxWidth?: number;
  onCancel: () => void;
  onSelect: (slug: string, saveDefault: boolean) => void;
  sessionId: string | null;
  t: Theme;
}
