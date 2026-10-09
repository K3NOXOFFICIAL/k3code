import { Box, Text } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";

import { useAgentRoster } from "../app/agentRoster.js";
import { SectionRule } from "../components/sectionRule.js";
import { $uiState } from "../app/uiStore.js";
import type { LiveSessionStatus, SessionActiveItem } from "../gatewayTypes.js";
import { fmtDuration } from "../lib/subagentTree.js";
import { compactPreview } from "../lib/text.js";
import type { Theme } from "../theme.js";
import type { SubagentProgress } from "../types.js";

import {
  $stripSessions,
  STRIP_MAX_ROWS,
  type StripRow,
  type StripState,
} from "./agentStripStore.js";

/** How long a finished sub-agent stays in the strip after it ends. */
export const FINISHED_LINGER_MS = 60_000;

export const GLYPH: Record<StripState, string> = {
  done: "✓",
  failed: "✗",
  idle: "○",
  input: "●",
  working: "◐",
};
export const LABEL: Record<StripState, string> = {
  done: "completed",
  failed: "failed",
  idle: "idle",
  input: "needs input",
  working: "working",
};

export const stateColor = (state: StripState, t: Theme): string =>
  state === "done"
    ? t.color.statusGood
    : state === "failed"
      ? t.color.error
      : state === "input"
        ? t.color.warn
        : state === "idle"
          ? t.color.muted
          : t.color.accent;

const sessionState = (s: LiveSessionStatus | string): StripState =>
  s === "waiting" || s === "needs_input"
    ? "input"
    : s === "working" || s === "starting" || s === "running" || s === "queued"
      ? "working"
      : s === "failed"
        ? "failed"
        : "done";

const agentState = (status: string): StripState =>
  status === "completed" || status === "done"
    ? "done"
    : status === "failed" ||
        status === "error" ||
        status === "timeout" ||
        status === "interrupted" ||
        status === "cancelled"
      ? "failed"
      : "working";

/** One live session as a row; shared by the strip and the agent view (which also shows the current session). */
export const sessionRow = (s: SessionActiveItem, nowMs: number): StripRow => ({
  activity:
    s.preview?.trim() ||
    (s.status === "waiting" || s.status === "needs_input"
      ? "waiting for input"
      : s.status),
  elapsedSeconds:
    s.started_at != null ? Math.max(0, nowMs / 1000 - s.started_at) : null,
  id: s.id,
  key: `session:${s.id}`,
  kind: "session",
  state: sessionState(s.status),
  title: s.title?.trim() || s.preview?.trim() || s.id.slice(0, 8),
});

/** Merge background sessions and live sub-agents into strip rows (needs-input first, then working, then finished). */
export function buildStripRows(
  subagents: readonly SubagentProgress[],
  sessions: readonly SessionActiveItem[],
  nowMs: number,
  currentSid: null | string = null,
): StripRow[] {
  const rows: StripRow[] = [];

  for (const s of sessions) {
    // Sub-agents of the current turn already come from the in-turn roster (`subagents`); as session rows they were
    // drawn a second time as "completed", and Enter / x on them hit session.activate / session.interrupt with a
    // sub-agent id ("unknown session", a stop that silently did nothing).
    if (s.current || s.id === currentSid || s.origin === "subagent") {
      continue;
    }

    rows.push(sessionRow(s, nowMs));
  }

  for (const a of subagents) {
    const state = agentState(a.status);

    // Like Claude Code, a finished agent leaves the list shortly after it ends; the gateway keeps finished handles
    // for /agents, so without this every past turn's children piled up here as "done".
    if (
      (state === "done" || state === "failed") &&
      a.startedAt != null &&
      a.durationSeconds != null &&
      nowMs - (a.startedAt + a.durationSeconds * 1000) > FINISHED_LINGER_MS
    ) {
      continue;
    }

    rows.push({
      activity:
        a.notes.at(-1) ||
        a.tools.at(-1) ||
        (state === "working" ? "starting…" : a.status),
      elapsedSeconds:
        a.durationSeconds ??
        (a.startedAt != null
          ? Math.max(0, (nowMs - a.startedAt) / 1000)
          : null),
      id: a.id,
      key: `agent:${a.id}`,
      kind: "agent",
      state,
      title: a.goal || "agent",
    });
  }

  const rank: Record<StripState, number> = {
    input: 0,
    working: 1,
    idle: 2,
    failed: 3,
    done: 4,
  };

  return rows
    .map((r, i) => ({ i, r }))
    .sort((a, b) => rank[a.r.state] - rank[b.r.state] || a.i - b.i)
    .map((x) => x.r);
}

export interface AgentStripViewProps {
  cols: number;
  rows: readonly StripRow[];
  t: Theme;
}

/**
 * Read-only list under the prompt: one row per session/agent, ≤6 rows then `+N more`; hidden when empty. It never takes
 * focus (↑/↓ stay with the prompt history); `←` on an empty prompt opens the agent view, where rows are selected.
 */
export function AgentStripView({ cols, rows, t }: AgentStripViewProps) {
  if (!rows.length) {
    return null;
  }

  const shown = rows.slice(0, STRIP_MAX_ROWS);
  const more = rows.length - shown.length;

  return (
    <Box flexDirection="column" flexShrink={0} width={cols}>
      {/* The section header doubles as the key hint: the rows are managed in the agent view. */}
      <SectionRule
        cols={cols}
        label={`agents (${rows.length}) · ← agent view`}
        t={t}
      />
      {shown.map((row) => {
        const elapsed =
          row.elapsedSeconds == null
            ? ""
            : ` ${fmtDuration(row.elapsedSeconds)}`;
        const head = `  ${GLYPH[row.state]} `;
        const tail = `${elapsed} · ${LABEL[row.state]}`;
        const titleW = Math.max(
          8,
          Math.floor((cols - head.length - tail.length) * 0.4),
        );
        const actW = Math.max(0, cols - head.length - tail.length - titleW - 4);

        return (
          <Text key={row.key} wrap="truncate-end">
            <Text color={stateColor(row.state, t)}>{head}</Text>
            <Text color={t.color.text}>
              {compactPreview(row.title, titleW)}
            </Text>
            <Text color={t.color.muted}>{tail}</Text>
            {actW >= 8 ? (
              <Text
                color={t.color.muted}
              >{` · ${compactPreview(row.activity, actW)}`}</Text>
            ) : null}
          </Text>
        );
      })}
      {more > 0 ? <Text color={t.color.muted}>{`  +${more} more`}</Text> : null}
    </Box>
  );
}

/** Connected strip, mounted directly below the composer. */
export function AgentStrip({ cols }: { cols: number }) {
  const { sid, theme } = useStore($uiState);
  const sessions = useStore($stripSessions);
  const subagents = useAgentRoster();
  const [now, setNow] = useState(Date.now);
  const rows = useMemo(
    () => buildStripRows(subagents, sessions, now, sid),
    [subagents, sessions, now, sid],
  );
  // tick while something runs, and while a finished agent row still has to expire (FINISHED_LINGER_MS)
  const live = rows.some(
    (r) => r.state === "working" || (r.kind === "agent" && r.state !== "input"),
  );

  useEffect(() => {
    if (!live) {
      return;
    }

    const timer = setInterval(() => setNow(Date.now()), 1000);

    return () => clearInterval(timer);
  }, [live]);

  return <AgentStripView cols={cols} rows={rows} t={theme} />;
}
