import { Box, Text, useInput, useStdout } from "@k3code/ink";
import type { SessionListResult } from "@k3code/shared/gateway-events";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";

import { useAgentRoster } from "../app/agentRoster.js";
import { $uiState } from "../app/uiStore.js";
import { relativeSessionAge } from "../components/activeSessionSwitcher.js";
import { listRowStyle } from "../components/overlayPrimitives.js";
import { TextInput } from "../components/textInput.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import { fmtDuration } from "../lib/subagentTree.js";
import { compactPreview } from "../lib/text.js";
import type { Theme } from "../theme.js";

import { GLYPH, LABEL, stateColor } from "./agentStrip.js";
import { $stripSessions } from "./agentStripStore.js";
import {
  $collapsedGroups,
  $pastSessionsCache,
  buildViewRows,
  IDLE_VIEW_NAV,
  type PastSessionRow,
  reduceViewKey,
  selectedIndex,
  toggleGroup,
  VIEW_GROUP_LABEL,
  type ViewGroup,
  type ViewItem,
  viewItems,
  type ViewNav,
  type ViewRow,
  visibleWindow,
} from "./agentViewStore.js";

export const AGENT_VIEW_HINT =
  "↑↓ select · →/⏎ attach · ⏎/space fold group · x stop · n new · ←/esc back";
export const NEW_SESSION_PLACEHOLDER =
  "first message for the new session (Enter to start, Esc to cancel)";

const COUNT_LABEL: Record<ViewGroup, string> = {
  completed: "completed",
  input: "needs input",
  working: "working",
};

/** Header summary order: what runs first, then what waits, then what is done. */
const SUMMARY_ORDER: readonly ViewGroup[] = ["working", "input", "completed"];

const CURRENT_TAG = " (this session)";
const CONFIRM_TAG = "  stop? y/n";
const MODEL_MAX = 24;

const stateText = (row: ViewRow) =>
  row.kind === "past"
    ? relativeSessionAge(row.lastActive ?? undefined) || "earlier"
    : row.elapsedSeconds == null
      ? LABEL[row.state]
      : `${LABEL[row.state]} ${fmtDuration(row.elapsedSeconds)}`;

/** The inline prompt for a new session, shown in place of the footer hint while `n` is active. */
export interface NewSessionDraft {
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  value: string;
}

export interface AgentViewViewProps {
  collapsed?: ReadonlySet<ViewGroup>;
  cols: number;
  /** Short terminal: no spacer lines and no scroll indicators, so every line goes to the list. */
  compact?: boolean;
  draft?: NewSessionDraft | null;
  error?: null | string;
  /** Lines available to the grouped list (headers, rows and scroll indicators). */
  height: number;
  loading?: boolean;
  nav: ViewNav;
  rows: readonly ViewRow[];
  t: Theme;
}

const NO_GROUPS: ReadonlySet<ViewGroup> = new Set();

/** Presentation only: every session grouped by state under a foldable header, the selection kept in view. */
export function AgentViewView({
  collapsed = NO_GROUPS,
  cols,
  compact = false,
  draft = null,
  error = null,
  height,
  loading = false,
  nav,
  rows,
  t,
}: AgentViewViewProps) {
  const lines: ViewItem[] = viewItems(rows, collapsed);
  const index = Math.max(0, Math.min(nav.index, lines.length - 1));
  // On overflow two lines go to the ↑/↓ indicators (not in compact mode) so the list never outgrows `height`.
  const overflow = !compact && lines.length > height;
  const inner = overflow ? Math.max(1, height - 2) : Math.max(1, height);
  const [prevStart, setPrevStart] = useState(0);
  const win = visibleWindow(lines.length, index, inner, prevStart);

  if (win.start !== prevStart) {
    setPrevStart(win.start);
  }

  const above = overflow ? win.start : 0;
  const below = overflow ? lines.length - win.end : 0;

  const counts = SUMMARY_ORDER.map(
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

  const renderHeader = (
    line: Extract<ViewItem, { type: "header" }>,
    selected: boolean,
  ) => {
    const style = listRowStyle(t, selected);
    const empty = line.count === 0;

    return (
      <Text
        backgroundColor={style.backgroundColor}
        bold={selected || !empty}
        key={line.key}
        wrap="truncate-end"
      >
        <Text color={selected ? t.color.accent : t.color.muted}>
          {selected ? "› " : "  "}
        </Text>
        <Text color={selected ? t.color.accent : t.color.muted}>
          {line.collapsed ? "▸ " : "▾ "}
        </Text>
        <Text
          color={
            selected
              ? (style.color ?? t.color.text)
              : empty
                ? t.color.muted
                : t.color.text
          }
          dimColor={empty && !selected}
        >
          {`${VIEW_GROUP_LABEL[line.group]} (${line.count})`}
        </Text>
      </Text>
    );
  };

  const renderRow = (row: ViewRow, selected: boolean) => {
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
    // Earlier sessions are dimmed unless selected, so the live rows above them stand out.
    const ink = selected
      ? (style.color ?? t.color.text)
      : row.kind === "past"
        ? t.color.muted
        : t.color.text;

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
      {!rows.length && !loading ? (
        <Text color={t.color.muted} wrap="truncate-end">
          No sessions yet - press n to start one
        </Text>
      ) : null}
      <Box flexDirection="column">
        {above > 0 ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {`  ↑ ${above} more`}
          </Text>
        ) : null}
        {lines
          .slice(win.start, win.end)
          .map((l, i) =>
            l.type === "header"
              ? renderHeader(l, win.start + i === index)
              : renderRow(l.row, win.start + i === index),
          )}
        {below > 0 ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {`  ↓ ${below} more`}
          </Text>
        ) : null}
      </Box>
      {compact ? null : <Text> </Text>}
      {draft ? (
        <Box>
          <Text color={t.color.accent}>new › </Text>
          <TextInput
            color={t.color.text}
            columns={Math.max(10, cols - 6)}
            onChange={draft.onChange}
            onSubmit={draft.onSubmit}
            placeholder={NEW_SESSION_PLACEHOLDER}
            placeholderColor={t.color.muted}
            value={draft.value}
          />
        </Box>
      ) : (
        <Text color={t.color.muted} wrap="truncate-end">
          {AGENT_VIEW_HINT}
        </Text>
      )}
    </Box>
  );
}

export interface AgentViewPaneProps {
  gw: GatewayClient;
  onActivate: (row: ViewRow) => void;
  onClose: () => void;
  /** An empty draft: start an empty session, as before. */
  onNew: () => void;
  /** A typed draft: start a session and send the text as its first prompt. */
  onNewPrompt: (prompt: string) => void;
  onStop: (row: ViewRow) => void;
}

/** Connected full-screen agent view: live sessions and sub-agents plus this project's earlier sessions. */
export function AgentViewPane({
  gw,
  onActivate,
  onClose,
  onNew,
  onNewPrompt,
  onStop,
}: AgentViewPaneProps) {
  const { stdout } = useStdout();
  const { info, sid, theme } = useStore($uiState);
  const sessions = useStore($stripSessions);
  const subagents = useAgentRoster();
  const collapsed = useStore($collapsedGroups);
  const cache = useStore($pastSessionsCache);
  // The session's own workspace, not the TUI's launch directory: that one would show another project's history.
  const currentCwd = info?.cwd;
  // The view opens on what the last fetch of this project brought and refreshes behind it, so the live rows and the
  // cached history show at once instead of a "Loading…" screen while the daemon answers.
  const cached = currentCwd && cache?.cwd === currentCwd ? cache.rows : null;
  const [past, setPast] = useState<PastSessionRow[]>(cached ?? []);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<null | string>(null);
  const [navState, setNav] = useState<ViewNav>(IDLE_VIEW_NAV);
  // The selection follows its line's key: the 1.5 s session poll can reorder or insert rows above it. Until the user
  // moves, it rests on this session's row, where they came from.
  const [selKey, setSelKey] = useState<null | string>(null);
  const [now, setNow] = useState(Date.now);
  // `n`: the first message of a new session is typed here; null while no draft is open.
  const [draft, setDraft] = useState<null | string>(null);

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
          const rows = r.sessions ?? [];
          setPast(rows);

          if (currentCwd) {
            $pastSessionsCache.set({ cwd: currentCwd, rows });
          }
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
  const items = useMemo(() => viewItems(rows, collapsed), [collapsed, rows]);
  // Before the user moves: this session's row, else the first visible row (an empty leading header is no place to
  // start); the current row is hidden when its group is folded.
  const curKey = rows.find((r) => r.current)?.key;
  const restKey =
    items.find((it) => it.type === "row" && it.key === curKey)?.key ??
    items.find((it) => it.type === "row")?.key ??
    null;
  const nav: ViewNav = {
    ...navState,
    index: selectedIndex(items, selKey ?? restKey, navState.index),
  };
  const working = rows.some((r) => r.state === "working");

  useEffect(() => {
    if (!working) {
      return;
    }

    const timer = setInterval(() => setNow(Date.now()), 1000);

    return () => clearInterval(timer);
  }, [working]);

  const submitDraft = (value: string) => {
    const prompt = value.trim();
    setDraft(null);

    // An empty Enter starts an empty session, as `n` did before the draft existed.
    if (prompt) {
      onNewPrompt(prompt);
    } else {
      onNew();
    }
  };

  useInput((ch, key) => {
    // While the draft is open the TextInput owns the keys; only Esc (which it leaves alone) cancels it here.
    if (draft != null) {
      if (key.escape) {
        setDraft(null);
      }

      return;
    }

    const r = reduceViewKey(
      nav,
      items,
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
        right: key.rightArrow,
        up: key.upArrow,
      },
      Math.max(1, height - 2),
    );

    setNav(r.nav);
    setSelKey(items[r.nav.index]?.key ?? null);

    // Side effects run here, never inside a state updater, so a stop cannot fire twice.
    switch (r.effect?.type) {
      case "activate":
        return onActivate(r.effect.row);
      case "stop":
        return onStop(r.effect.row);
      case "toggle":
        return toggleGroup(r.effect.group);
      case "new":
        return setDraft("");
      case "close":
        return onClose();
    }
  });

  return (
    <AgentViewView
      collapsed={collapsed}
      cols={cols}
      compact={compact}
      draft={
        draft == null
          ? null
          : { onChange: setDraft, onSubmit: submitDraft, value: draft }
      }
      error={error}
      height={height}
      loading={loading}
      nav={nav}
      rows={rows}
      t={theme}
    />
  );
}
