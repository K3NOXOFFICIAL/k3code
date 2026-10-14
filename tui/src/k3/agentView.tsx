import { Box, Text, useInput, useStdout } from "@k3code/ink";
import type { SessionListResult } from "@k3code/shared/gateway-events";
import { useStore } from "@nanostores/react";
import { type ReactNode, useEffect, useMemo, useState } from "react";

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
import { settleViewAction } from "./agentViewActions.js";
import {
  $agentViewDraft,
  buildViewRows,
  type ConfirmAction,
  defaultViewKey,
  getAgentViewDraft,
  IDLE_VIEW_NAV,
  type PastSessionRow,
  reduceViewKey,
  routeViewInput,
  selectedIndex,
  setAgentViewDraft,
  VIEW_GROUP_LABEL,
  VIEW_GROUPS,
  type ViewAction,
  type ViewGroup,
  type ViewNav,
  type ViewNotice,
  type ViewRow,
  visibleWindow,
  withNewAgentRow,
} from "./agentViewStore.js";

/** Fits 80 columns. */
export const AGENT_VIEW_HINT =
  "type to send · ⏎ attach/send · ↑↓ select · ^N new · ^X stop · esc back";

/** The hint with the view's commands, shown instead when the terminal is wide enough for it. */
export const AGENT_VIEW_HINT_WIDE = `${AGENT_VIEW_HINT} · /rename /delete /close /bg · other /cmd !cmd run in this session`;

const COUNT_LABEL: Record<ViewGroup, string> = {
  completed: "completed",
  input: "need input",
  new: "",
  working: "working",
};

const CURRENT_TAG = " (this session)";
const CONFIRM_TAG: Record<ConfirmAction, string> = {
  delete: "  delete? y/n",
  stop: "  stop? y/n",
};
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
  /** Short terminal: no spacer lines, scroll indicators or hint, so every line goes to the list. */
  compact?: boolean;
  /** The composer line, under the list. */
  composer?: ReactNode;
  error?: null | string;
  /** Lines available to the grouped list (headers, rows and scroll indicators). */
  height: number;
  loading?: boolean;
  nav: ViewNav;
  notice?: null | ViewNotice;
  rows: readonly ViewRow[];
  t: Theme;
}

/** Presentation only: every session grouped by state, the selection kept in view. */
export function AgentViewView({
  cols,
  compact = false,
  composer = null,
  error = null,
  height,
  loading = false,
  nav,
  notice = null,
  rows,
  t,
}: AgentViewViewProps) {
  const index = Math.max(0, Math.min(nav.index, rows.length - 1));
  const lines: Line[] = [];

  for (const group of VIEW_GROUPS) {
    const inGroup = rows.filter((r) => r.group === group);

    if (inGroup.length) {
      // "+ New agent" stands alone above the groups.
      if (VIEW_GROUP_LABEL[group]) {
        lines.push({ group, type: "header" });
      }

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
    .filter(([g, n]) => n > 0 && COUNT_LABEL[g])
    .map(([g, n]) => `${n} ${COUNT_LABEL[g]}`)
    .join(" · ");

  const stateW = Math.max(
    0,
    ...rows.filter((r) => r.kind !== "new").map((r) => stateText(r).length),
  );
  const modelW = Math.min(
    MODEL_MAX,
    Math.max(0, ...rows.map((r) => r.model?.length ?? 0)),
  );

  const renderRow = (row: ViewRow, i: number) => {
    const selected = i === index;
    const style = listRowStyle(t, selected);
    const confirm =
      nav.confirmKey === row.key
        ? CONFIRM_TAG[nav.confirmAction ?? "stop"]
        : "";

    if (row.kind === "new") {
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
          <Text color={t.color.accent}>{row.title}</Text>
        </Text>
      );
    }

    const tag = row.current ? CURRENT_TAG : "";
    const fixed = 4 + 2 + stateW + (modelW ? 2 + modelW : 0) + confirm.length;
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
        {confirm ? <Text color={t.color.warn}>{confirm}</Text> : null}
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
            : "No sessions yet - press Ctrl+N to start one"}
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
      {notice ? (
        <Text
          color={notice.tone === "error" ? t.color.error : t.color.muted}
          wrap="truncate-end"
        >
          {notice.text}
        </Text>
      ) : null}
      {composer}
      {compact && composer ? null : (
        <Text color={t.color.muted} wrap="truncate-end">
          {cols >= AGENT_VIEW_HINT_WIDE.length
            ? AGENT_VIEW_HINT_WIDE
            : AGENT_VIEW_HINT}
        </Text>
      )}
    </Box>
  );
}

/** The composer's placeholder: what Enter does with text on this row. */
export const targetLabel = (row: undefined | ViewRow): string =>
  !row || row.kind === "new"
    ? "prompt for a new background agent"
    : row.kind === "agent"
      ? `steer ${row.title}`
      : row.kind === "past"
        ? `resume ${row.title} and send`
        : row.current
          ? "message this session"
          : `message ${row.title}`;

export interface AgentViewPaneProps {
  gw: GatewayClient;
  onActivate: (row: ViewRow) => void;
  onClose: () => void;
  /** Lines the view does not run itself (other `/cmd`, `!cmd`): the caller closes the view and submits them. */
  onMain?: (text: string) => void;
  onNew: () => void;
  /** May resolve to a notice (a failed stop). */
  onStop: (row: ViewRow) => Promise<null | ViewNotice> | void;
  // Composer sends and view commands; each resolves to the notice the view shows. Info clears the draft, error keeps it.
  onCloseSession?: (row: ViewRow) => Promise<ViewNotice>;
  onDelete?: (row: ViewRow) => Promise<ViewNotice>;
  onDispatch?: (text: string) => Promise<ViewNotice>;
  onRename?: (row: ViewRow, title: string) => Promise<ViewNotice>;
  onResumeAndSend?: (row: ViewRow, text: string) => Promise<ViewNotice>;
  onSendCurrent?: (text: string) => Promise<ViewNotice>;
  onSendTo?: (row: ViewRow, text: string) => Promise<ViewNotice>;
  onSteer?: (row: ViewRow, text: string) => Promise<ViewNotice>;
}

const UNAVAILABLE: ViewNotice = {
  text: "not available here",
  tone: "error",
};

/** Connected full-screen agent view: live sessions and sub-agents plus this project's earlier sessions. */
export function AgentViewPane({
  gw,
  onActivate,
  onClose,
  onCloseSession,
  onDelete,
  onDispatch,
  onMain,
  onNew,
  onRename,
  onResumeAndSend,
  onSendCurrent,
  onSendTo,
  onSteer,
  onStop,
}: AgentViewPaneProps) {
  const { stdout } = useStdout();
  const { info, sid, theme } = useStore($uiState);
  const sessions = useStore($stripSessions);
  const subagents = useAgentRoster();
  const draft = useStore($agentViewDraft);
  const [past, setPast] = useState<PastSessionRow[]>([]);
  const [pastReload, setPastReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<null | string>(null);
  const [notice, setNotice] = useState<null | ViewNotice>(null);
  const [navState, setNav] = useState<ViewNav>(IDLE_VIEW_NAV);
  // The selection follows its row's key: the 1.5 s session poll can reorder or insert rows above it. Until the user
  // moves, it rests on this session's row, where they came from.
  const [selKey, setSelKey] = useState<null | string>(null);
  const [now, setNow] = useState(Date.now);
  // The session's own workspace, not the TUI's launch directory: that one would show another project's history.
  const currentCwd = info?.cwd;

  const termRows = stdout?.rows ?? 24;
  // The real width, however narrow: a wider layout would wrap every line.
  const cols = Math.max(1, (stdout?.columns ?? 80) - 1);
  // Under 8 rows the spacers, scroll indicators and hint go, so the list keeps what little room there is.
  const compact = termRows < 8;
  // header, spacer, spacer, composer, hint (+ the error and notice lines); compact: header and composer only
  const height = Math.max(
    1,
    termRows - (compact ? 2 : 5) - (error ? 1 : 0) - (notice ? 1 : 0),
  );

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
  }, [currentCwd, gw, pastReload]);

  const rows = useMemo(
    () =>
      withNewAgentRow(
        buildViewRows({
          currentCwd,
          currentSid: sid,
          nowMs: now,
          past,
          sessions,
          subagents,
        }),
      ),
    [currentCwd, now, past, sessions, sid, subagents],
  );
  const nav: ViewNav = {
    ...navState,
    index: selectedIndex(rows, selKey ?? defaultViewKey(rows), navState.index),
  };
  const selected = rows[nav.index];
  const working = rows.some((r) => r.state === "working");

  useEffect(() => {
    if (!working) {
      return;
    }

    const timer = setInterval(() => setNow(Date.now()), 1000);

    return () => clearInterval(timer);
  }, [working]);

  // Info clears the draft (it was sent); an error keeps it so it can be fixed and sent again.
  const report = (
    pending: Promise<ViewNotice> | undefined,
    after?: (n: ViewNotice) => void,
  ) => {
    if (!pending) {
      return setNotice(UNAVAILABLE);
    }

    void settleViewAction(pending).then((n) => {
      setNotice(n);

      if (n.tone === "info") {
        after?.(n);
      }
    });
  };
  const reloadPast = () => setPastReload((n) => n + 1);

  const activate = (row: undefined | ViewRow) =>
    !row || row.kind === "new"
      ? onNew()
      : row.current
        ? onClose()
        : onActivate(row);

  const run = (a: ViewAction) => {
    switch (a.type) {
      case "notice":
        return setNotice(a.notice);
      case "none":
        return setAgentViewDraft("");
      case "confirm":
        setAgentViewDraft("");
        setSelKey(a.row.key);

        return setNav({
          confirmAction: a.action,
          confirmKey: a.row.key,
          index: rows.indexOf(a.row),
        });
      case "new":
        setAgentViewDraft("");

        return activate(undefined);
      case "activate":
        setAgentViewDraft("");

        return activate(a.row);
      case "main":
        setAgentViewDraft("");

        return onMain ? onMain(a.text) : setNotice(UNAVAILABLE);
      case "dispatch":
        return report(onDispatch?.(a.text), (n) => {
          if (n.sessionId) {
            setSelKey(`session:${n.sessionId}`);
          }
        });
      case "sendCurrent":
        return report(onSendCurrent?.(a.text));
      case "sendTo":
        return report(onSendTo?.(a.row, a.text));
      case "steer":
        return report(onSteer?.(a.row, a.text));
      case "resumeAndSend":
        return report(onResumeAndSend?.(a.row, a.text));
      case "rename":
        return report(onRename?.(a.row, a.title), reloadPast);
      case "closeSession":
        return report(onCloseSession?.(a.row));
    }
  };

  // Enter in the composer: an empty line attaches to the selected row (this session's row closes the view), text is
  // routed by `routeViewInput`.
  const submit = (value: string) => {
    const text = value.trim();

    if (!text) {
      return activate(selected);
    }

    if (selected) {
      run(routeViewInput(text, selected));
    }
  };

  // The composer's TextInput subscribed first (child effects run first), so it has already applied this key and
  // flushed its value to the draft atom when a non-text key arrives; read the atom, not the render's `draft`.
  useInput((ch, key) => {
    if (!key.return) {
      // Enter's notice comes from the composer's submit, which ran just before this handler.
      setNotice(null);
    }

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
        right: key.rightArrow,
        up: key.upArrow,
      },
      Math.max(1, height - 2),
      getAgentViewDraft(),
    );

    if (!r.consumed) {
      return;
    }

    setNav(r.nav);
    setSelKey(rows[r.nav.index]?.key ?? null);

    // Side effects run here, never inside a state updater, so a stop cannot fire twice.
    switch (r.effect?.type) {
      case "activate":
        return activate(r.effect.row);
      case "stop": {
        const pending = onStop(r.effect.row);

        return void pending?.then((n) => n && setNotice(n));
      }
      case "delete":
        return report(onDelete?.(r.effect.row), reloadPast);
      case "dispatch":
        return run({ text: r.effect.text, type: "dispatch" });
      case "clearDraft":
        return setAgentViewDraft("");
      case "new":
        setAgentViewDraft("");

        return onNew();
      case "close":
        return onClose();
    }
  });

  return (
    <AgentViewView
      cols={cols}
      compact={compact}
      composer={
        <Box>
          <Text color={theme.color.accent}>{"› "}</Text>
          <TextInput
            columns={Math.max(1, cols - 2)}
            focus={!nav.confirmKey}
            ignoreVerticalArrows
            onChange={setAgentViewDraft}
            onSubmit={submit}
            placeholder={targetLabel(selected)}
            placeholderColor={theme.color.muted}
            value={draft}
          />
        </Box>
      }
      error={error}
      height={height}
      loading={loading}
      nav={nav}
      notice={notice}
      rows={rows}
      t={theme}
    />
  );
}
