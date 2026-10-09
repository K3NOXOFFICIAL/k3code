import { Box, Text, useInput, useStdout } from "@k3code/ink";
import type { SessionListResult } from "@k3code/shared/gateway-events";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";

import { useAgentRoster } from "../app/agentRoster.js";
import { $uiState } from "../app/uiStore.js";
import { relativeSessionAge } from "../components/activeSessionSwitcher.js";
import { listRowStyle } from "../components/overlayPrimitives.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import { fmtDuration } from "../lib/subagentTree.js";
import { compactPreview } from "../lib/text.js";
import type { Theme } from "../theme.js";

import { GLYPH, LABEL, stateColor } from "./agentStrip.js";
import { $stripSessions } from "./agentStripStore.js";
import {
  buildViewRows,
  IDLE_VIEW_NAV,
  type PastSessionRow,
  reduceViewKey,
  selectedIndex,
  VIEW_GROUP_LABEL,
  VIEW_GROUPS,
  type ViewGroup,
  type ViewNav,
  type ViewRow,
  visibleWindow,
} from "./agentViewStore.js";

export const AGENT_VIEW_HINT = "↑↓ select · ⏎ attach · x stop · n new · ← back";

const COUNT_LABEL: Record<ViewGroup, string> = {
  finished: "finished",
  idle: "idle",
  input: "need input",
  past: "earlier",
  working: "working",
};

const CURRENT_TAG = " (this session)";
const CONFIRM_TAG = "  stop? y/n";
const MODEL_MAX = 24;

type Line =
  | { group: ViewGroup; type: "header" }
  | { index: number; row: ViewRow; type: "row" };

const stateText = (row: ViewRow) =>
  row.kind === "past"
    ? relativeSessionAge(row.lastActive ?? undefined) || "earlier"
    : row.elapsedSeconds == null
      ? LABEL[row.state]
      : `${LABEL[row.state]} ${fmtDuration(row.elapsedSeconds)}`;

export interface AgentViewViewProps {
  cols: number;
  /** Short terminal: no spacer lines and no scroll indicators, so every line goes to the list. */
  compact?: boolean;
  error?: null | string;
  /** Lines available to the grouped list (headers, rows and scroll indicators). */
  height: number;
  loading?: boolean;
  nav: ViewNav;
  rows: readonly ViewRow[];
  t: Theme;
}

/** Presentation only: every session grouped by state, the selection kept in view. */
export function AgentViewView({
  cols,
  compact = false,
  error = null,
  height,
  loading = false,
  nav,
  rows,
  t,
}: AgentViewViewProps) {
  const index = Math.max(0, Math.min(nav.index, rows.length - 1));
  const lines: Line[] = [];

  for (const group of VIEW_GROUPS) {
    const inGroup = rows.filter((r) => r.group === group);

    if (inGroup.length) {
      lines.push({ group, type: "header" });

      for (const row of inGroup) {
        lines.push({ index: rows.indexOf(row), row, type: "row" });
      }
    }
  }

  const selLine = lines.findIndex((l) => l.type === "row" && l.index === index);
  // On overflow two lines go to the ↑/↓ indicators (not in compact mode) so the list never outgrows `height`.
  const overflow = !compact && lines.length > height;
  const inner = overflow ? Math.max(1, height - 2) : Math.max(1, height);
  const [prevStart, setPrevStart] = useState(0);
  const win = visibleWindow(lines.length, selLine, inner, prevStart);

  if (win.start !== prevStart) {
    setPrevStart(win.start);
  }

  const rowsIn = (from: number, to: number) =>
    lines.slice(from, to).filter((l) => l.type === "row").length;
  const above = overflow ? rowsIn(0, win.start) : 0;
  const below = overflow ? rowsIn(win.end, lines.length) : 0;

  const counts = VIEW_GROUPS.map(
    (g) => [g, rows.filter((r) => r.group === g).length] as const,
  )
    .filter(([, n]) => n > 0)
    .map(([g, n]) => `${n} ${COUNT_LABEL[g]}`)
    .join(" · ");

  const stateW = Math.max(0, ...rows.map((r) => stateText(r).length));
  const modelW = Math.min(
    MODEL_MAX,
    Math.max(0, ...rows.map((r) => r.model?.length ?? 0)),
  );

  const renderRow = (row: ViewRow, i: number) => {
    const selected = i === index;
    const style = listRowStyle(t, selected);
    const confirm = nav.confirmKey === row.key;
    const tag = row.current ? CURRENT_TAG : "";
    const fixed =
      4 +
      2 +
      stateW +
      (modelW ? 2 + modelW : 0) +
      (confirm ? CONFIRM_TAG.length : 0);
    const titleW = Math.max(8, cols - fixed - 1);
    const title = compactPreview(row.title, Math.max(1, titleW - tag.length));
    const ink = selected ? (style.color ?? t.color.text) : t.color.text;

    return (
      <Text
        backgroundColor={style.backgroundColor}
        bold={selected}
        key={row.key}
        wrap="truncate-end"
      >
        <Text color={selected ? t.color.accent : t.color.muted}>
          {selected ? "› " : "  "}
        </Text>
        {row.kind === "past" ? (
          <Text color={t.color.muted}>· </Text>
        ) : (
          <Text color={stateColor(row.state, t)}>{GLYPH[row.state]} </Text>
        )}
        <Text color={ink}>{title}</Text>
        <Text color={t.color.muted}>
          {tag.padEnd(Math.max(0, titleW - title.length))}
        </Text>
        <Text
          color={t.color.muted}
        >{`  ${stateText(row).padEnd(stateW)}`}</Text>
        {modelW ? (
          <Text color={t.color.muted}>
            {`  ${compactPreview(row.model ?? "", modelW).padEnd(modelW)}`}
          </Text>
        ) : null}
        {confirm ? <Text color={t.color.warn}>{CONFIRM_TAG}</Text> : null}
      </Text>
    );
  };

  return (
    <Box flexDirection="column" width={cols}>
      <Text wrap="truncate-end">
        <Text bold color={t.color.accent}>
          Agents
        </Text>
        {counts ? <Text color={t.color.muted}>{`  ${counts}`}</Text> : null}
        {loading ? <Text color={t.color.muted}> · loading…</Text> : null}
      </Text>
      {error ? (
        <Text color={t.color.error} wrap="truncate-end">
          {error}
        </Text>
      ) : null}
      {compact ? null : <Text> </Text>}
      {!rows.length ? (
        <Text color={t.color.muted} wrap="truncate-end">
          {loading
            ? "Loading sessions…"
            : "No sessions yet - press n to start one"}
        </Text>
      ) : (
        <Box flexDirection="column">
          {above > 0 ? (
            <Text color={t.color.muted} wrap="truncate-end">
              {`  ↑ ${above} more`}
            </Text>
          ) : null}
          {lines.slice(win.start, win.end).map((l) =>
            l.type === "header" ? (
              <Text
                bold
                color={t.color.text}
                key={`group:${l.group}`}
                wrap="truncate-end"
              >
                {VIEW_GROUP_LABEL[l.group]}
              </Text>
            ) : (
              renderRow(l.row, l.index)
            ),
          )}
          {below > 0 ? (
            <Text color={t.color.muted} wrap="truncate-end">
              {`  ↓ ${below} more`}
            </Text>
          ) : null}
        </Box>
      )}
      {compact ? null : <Text> </Text>}
      <Text color={t.color.muted} wrap="truncate-end">
        {AGENT_VIEW_HINT}
      </Text>
    </Box>
  );
}

export interface AgentViewPaneProps {
  gw: GatewayClient;
  onActivate: (row: ViewRow) => void;
  onClose: () => void;
  onNew: () => void;
  onStop: (row: ViewRow) => void;
}

/** Connected full-screen agent view: live sessions and sub-agents plus this project's earlier sessions. */
export function AgentViewPane({
  gw,
  onActivate,
  onClose,
  onNew,
  onStop,
}: AgentViewPaneProps) {
  const { stdout } = useStdout();
  const { info, sid, theme } = useStore($uiState);
  const sessions = useStore($stripSessions);
  const subagents = useAgentRoster();
  const [past, setPast] = useState<PastSessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<null | string>(null);
  const [navState, setNav] = useState<ViewNav>(IDLE_VIEW_NAV);
  // The selection follows its row's key: the 1.5 s session poll can reorder or insert rows above it.
  const [selKey, setSelKey] = useState<null | string>(null);
  const [now, setNow] = useState(Date.now);
  // The session's own workspace, not the TUI's launch directory: that one would show another project's history.
  const currentCwd = info?.cwd;

  const termRows = stdout?.rows ?? 24;
  // The real width, however narrow: a wider layout would wrap every line.
  const cols = Math.max(1, (stdout?.columns ?? 80) - 1);
  // Under 8 rows the spacers and scroll indicators go, so the list keeps what little room there is.
  const compact = termRows < 8;
  // header, spacer, spacer, footer (+ the error line); compact: header and footer only
  const height = Math.max(1, termRows - (compact ? 2 : 4) - (error ? 1 : 0));

  useEffect(() => {
    let stopped = false;

    // The gateway filters by project (`cwd`) before applying the limit; buildViewRows re-checks it.
    gw.request<SessionListResult>("session.list", {
      ...(currentCwd ? { cwd: currentCwd } : {}),
      limit: 50,
    })
      .then((raw) => {
        if (stopped) {
          return;
        }

        const r = asRpcResult<SessionListResult>(raw);

        if (r) {
          setPast(r.sessions ?? []);
        } else {
          setError("invalid response: session.list");
        }
      })
      .catch((e: unknown) => {
        if (!stopped) {
          setError(rpcErrorMessage(e));
        }
      })
      .finally(() => {
        if (!stopped) {
          setLoading(false);
        }
      });

    return () => {
      stopped = true;
    };
  }, [currentCwd, gw]);

  const rows = useMemo(
    () =>
      buildViewRows({
        currentCwd,
        currentSid: sid,
        nowMs: now,
        past,
        sessions,
        subagents,
      }),
    [currentCwd, now, past, sessions, sid, subagents],
  );
  const nav: ViewNav = {
    ...navState,
    index: selectedIndex(rows, selKey, navState.index),
  };
  const working = rows.some((r) => r.state === "working");

  useEffect(() => {
    if (!working) {
      return;
    }

    const timer = setInterval(() => setNow(Date.now()), 1000);

    return () => clearInterval(timer);
  }, [working]);

  useInput((ch, key) => {
    const r = reduceViewKey(
      nav,
      rows,
      {
        ch,
        ctrl: key.ctrl,
        down: key.downArrow,
        end: key.end,
        escape: key.escape,
        home: key.home,
        left: key.leftArrow,
        meta: key.meta,
        pageDown: key.pageDown,
        pageUp: key.pageUp,
        return: key.return,
        up: key.upArrow,
      },
      Math.max(1, height - 2),
    );

    setNav(r.nav);
    setSelKey(rows[r.nav.index]?.key ?? null);

    // Side effects run here, never inside a state updater, so a stop cannot fire twice.
    switch (r.effect?.type) {
      case "activate":
        return onActivate(r.effect.row);
      case "stop":
        return onStop(r.effect.row);
      case "new":
        return onNew();
      case "close":
        return onClose();
    }
  });

  return (
    <AgentViewView
      cols={cols}
      compact={compact}
      error={error}
      height={height}
      loading={loading}
      nav={nav}
      rows={rows}
      t={theme}
    />
  );
}
