import { Box, Text, useInput, useStdout } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useRef, useState } from "react";

import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import {
  initSettingsState,
  type SettingsSnapshot,
  type SettingsState,
  settingsKey,
  settingsSnapshot,
  settingsWindow,
  visibleItems,
} from "../domain/settings.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint } from "./overlayControls.js";
import { chipRowProps } from "./overlayPrimitives.js";

type Local = Parameters<typeof settingsSnapshot>[1];

interface SettingsPanelProps {
  gw: GatewayClient;
  /** TUI-held values the backend view doesn't know. */
  local: Local;
  maxWidth?: number;
  onApply: (command: string) => void;
  onClose: () => void;
  sessionId: null | string;
  t: Theme;
}

/**
 * /settings: Config tab (↑/↓ move, ←/→ or Enter change, `/` search, Tab → Status,
 * Esc/q close). Each change dispatches its slash command via `onApply`. Ignores
 * keys while an approval / question prompt is open.
 */
export function SettingsPanel({
  gw,
  local,
  maxWidth,
  onApply,
  onClose,
  sessionId,
  t,
}: SettingsPanelProps) {
  const [state, setState] = useState<null | SettingsState>(null);
  const [err, setErr] = useState("");
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();
  const live = useRef<null | SettingsState>(null);
  const localRef = useRef(local);

  localRef.current = local;

  useEffect(() => {
    let current = true;

    gw.request<unknown>(
      "settings.get",
      sessionId ? { session_id: sessionId } : {},
    )
      .then((raw) => {
        if (!current) {
          return;
        }

        const snap: SettingsSnapshot = settingsSnapshot(
          asRpcResult(raw),
          localRef.current,
        );

        live.current = initSettingsState(snap);
        setState(live.current);
      })
      .catch((e: unknown) => current && setErr(rpcErrorMessage(e)));

    return () => {
      current = false;
    };
  }, [gw, sessionId]);

  useInput((ch, key) => {
    if (locked) {
      return;
    }

    const cur = live.current;

    if (!cur) {
      if (key.escape || ch === "q") {
        onClose();
      }

      return;
    }

    const step = settingsKey(cur, ch, key);

    if (step.state !== cur) {
      live.current = step.state;
      setState(step.state);
    }

    if (step.action.type === "close") {
      onClose();
    } else if (step.action.type === "apply") {
      onApply(step.action.command);
    }
  });

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc close</OverlayHint>
      </Box>
    );
  }

  if (!state) {
    return <Text color={t.color.muted}>loading settings…</Text>;
  }

  const width = Math.min(
    maxWidth ?? 80,
    Math.max(40, (stdout?.columns ?? 80) - 4),
  );
  const rows = Math.max(6, Math.min(16, (stdout?.rows ?? 24) - 10));
  const tab = (id: "config" | "status", label: string) => (
    <Text
      bold={state.tab === id}
      color={state.tab === id ? t.color.accent : t.color.muted}
    >
      {state.tab === id ? `[${label}]` : ` ${label} `}
    </Text>
  );

  return (
    <Box flexDirection="column" width={width}>
      <Box>
        <Text bold color={t.color.accent}>
          Settings{"  "}
        </Text>
        {tab("config", "Config")}
        <Text> </Text>
        {tab("status", "Status")}
      </Box>
      {state.tab === "status" ? (
        <StatusBody rows={rows} state={state} t={t} />
      ) : (
        <ConfigBody rows={rows} state={state} t={t} />
      )}
      <OverlayHint t={t}>
        {state.tab === "status"
          ? "↑/↓ scroll · Tab config · Esc close"
          : state.searching
            ? "type to filter · Enter done · Esc clear"
            : "↑/↓ move · ←/→ change · Enter cycle · / search · Tab status · Esc close"}
      </OverlayHint>
    </Box>
  );
}

function StatusBody({
  rows,
  state,
  t,
}: {
  rows: number;
  state: SettingsState;
  t: Theme;
}) {
  const start = settingsWindow(
    state.statusScroll,
    state.statusLines.length,
    rows,
  );

  return (
    <Box flexDirection="column">
      {state.statusLines.slice(start, start + rows).map((l, i) => (
        <Text color={t.color.text} key={start + i} wrap="truncate-end">
          {l || " "}
        </Text>
      ))}
    </Box>
  );
}

function ConfigBody({
  rows,
  state,
  t,
}: {
  rows: number;
  state: SettingsState;
  t: Theme;
}) {
  const items = visibleItems(state);
  const cursor = Math.min(state.cursor, Math.max(0, items.length - 1));
  const start = settingsWindow(cursor, items.length, rows);
  const labelW = Math.min(
    22,
    Math.max(12, ...items.map((i) => i.label.length)) + 1,
  );

  return (
    <Box flexDirection="column">
      <Text color={state.searching ? t.color.accent : t.color.muted}>
        {state.searching || state.query
          ? `search: ${state.query}${state.searching ? "▌" : ""}`
          : " "}
      </Text>
      {items.length === 0 ? (
        <Text color={t.color.muted}>no settings match</Text>
      ) : (
        items.slice(start, start + rows).map((it, k) => {
          const idx = start + k;
          const active = idx === cursor;
          const val =
            it.kind === "bool"
              ? it.value === "on"
                ? "● on"
                : "○ off"
              : `‹ ${it.value} ›`;
          const line = `${active ? "›" : " "} ${it.label.padEnd(labelW)}${val}`;

          return (
            <Text
              key={it.id}
              wrap="truncate-end"
              {...(active ? chipRowProps(t, true) : {})}
              color={active ? undefined : t.color.text}
            >
              {line}
            </Text>
          );
        })
      )}
      <Text color={t.color.muted} wrap="truncate-end">
        {items[cursor]
          ? `${items[cursor]!.section} · ${items[cursor]!.help}`
          : " "}
      </Text>
    </Box>
  );
}
