import { Box, Text } from '@k3code/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { useAgentRoster } from '../app/agentRoster.js'
import { $uiState } from '../app/uiStore.js'
import type { LiveSessionStatus, SessionActiveItem } from '../gatewayTypes.js'
import { fmtDuration } from '../lib/subagentTree.js'
import { compactPreview } from '../lib/text.js'
import type { Theme } from '../theme.js'
import type { SubagentProgress } from '../types.js'

import {
  $stripNav,
  $stripRows,
  $stripSessions,
  STRIP_MAX_ROWS,
  type StripRow,
  type StripState
} from './agentStripStore.js'

const GLYPH: Record<StripState, string> = { done: '✓', failed: '✗', input: '●', working: '◐' }
const LABEL: Record<StripState, string> = { done: 'completed', failed: 'failed', input: 'needs input', working: 'working' }

const color = (state: StripState, t: Theme): string =>
  state === 'done' ? t.color.statusGood : state === 'failed' ? t.color.error : state === 'input' ? t.color.warn : t.color.accent

const sessionState = (s: LiveSessionStatus | string): StripState =>
  s === 'waiting' || s === 'needs_input'
    ? 'input'
    : s === 'working' || s === 'starting' || s === 'running' || s === 'queued'
      ? 'working'
      : s === 'failed'
        ? 'failed'
        : 'done'

const agentState = (status: string): StripState =>
  status === 'completed' || status === 'done'
    ? 'done'
    : status === 'failed' || status === 'error' || status === 'timeout' || status === 'interrupted' || status === 'cancelled'
      ? 'failed'
      : 'working'

/** Merge background sessions and live sub-agents into strip rows (needs-input first, then working, then finished). */
export function buildStripRows(
  subagents: readonly SubagentProgress[],
  sessions: readonly SessionActiveItem[],
  nowMs: number,
  currentSid: null | string = null
): StripRow[] {
  const rows: StripRow[] = []

  for (const s of sessions) {
    // Sub-agents of the current turn already come from the in-turn roster (`subagents`); as session rows they were
    // drawn a second time as "completed", and Enter / x on them hit session.activate / session.interrupt with a
    // sub-agent id ("unknown session", a stop that silently did nothing).
    if (s.current || s.id === currentSid || s.origin === 'subagent') {
      continue
    }

    rows.push({
      activity:
        s.preview?.trim() || (s.status === 'waiting' || s.status === 'needs_input' ? 'waiting for input' : s.status),
      elapsedSeconds: s.started_at != null ? Math.max(0, nowMs / 1000 - s.started_at) : null,
      id: s.id,
      key: `session:${s.id}`,
      kind: 'session',
      state: sessionState(s.status),
      title: s.title?.trim() || s.preview?.trim() || s.id.slice(0, 8)
    })
  }

  for (const a of subagents) {
    const state = agentState(a.status)

    rows.push({
      activity: a.notes.at(-1) || a.tools.at(-1) || (state === 'working' ? 'starting…' : a.status),
      elapsedSeconds:
        a.durationSeconds ?? (a.startedAt != null ? Math.max(0, (nowMs - a.startedAt) / 1000) : null),
      id: a.id,
      key: `agent:${a.id}`,
      kind: 'agent',
      state,
      title: a.goal || 'agent'
    })
  }

  const rank: Record<StripState, number> = { input: 0, working: 1, failed: 2, done: 3 }

  return rows
    .map((r, i) => ({ i, r }))
    .sort((a, b) => rank[a.r.state] - rank[b.r.state] || a.i - b.i)
    .map(x => x.r)
}

export interface AgentStripViewProps {
  cols: number
  confirmKey?: null | string
  focused?: boolean
  index?: number
  rows: readonly StripRow[]
  t: Theme
}

/** Presentation only: one row per session/agent, ≤6 rows then `+N more`; hidden when empty. */
export function AgentStripView({ cols, confirmKey = null, focused = false, index = 0, rows, t }: AgentStripViewProps) {
  if (!rows.length) {
    return null
  }

  const shown = rows.slice(0, STRIP_MAX_ROWS)
  const more = rows.length - shown.length

  return (
    <Box flexDirection="column" flexShrink={0} width={cols}>
      {shown.map((row, i) => {
        const selected = focused && i === index
        const elapsed = row.elapsedSeconds == null ? '' : ` ${fmtDuration(row.elapsedSeconds)}`
        const head = `${selected ? '›' : ' '} ${GLYPH[row.state]} `
        const tail = `${elapsed} · ${LABEL[row.state]}`
        const titleW = Math.max(8, Math.floor((cols - head.length - tail.length) * 0.4))
        const actW = Math.max(0, cols - head.length - tail.length - titleW - 4)

        return (
          <Text key={row.key} wrap="truncate-end">
            <Text color={selected ? t.color.accent : t.color.muted}>{head.slice(0, 2)}</Text>
            <Text color={color(row.state, t)}>{GLYPH[row.state]} </Text>
            <Text bold={selected} color={t.color.text}>
              {compactPreview(row.title, titleW)}
            </Text>
            <Text color={t.color.muted}>{tail}</Text>
            {actW >= 8 ? <Text color={t.color.muted}>{` · ${compactPreview(row.activity, actW)}`}</Text> : null}
            {confirmKey === row.key ? <Text color={t.color.warn}> stop? y/n</Text> : null}
          </Text>
        )
      })}
      {more > 0 ? <Text color={t.color.muted}>{`  +${more} more`}</Text> : null}
      {focused ? <Text color={t.color.muted}>{'  ↑↓ move · ⏎ attach · x stop · esc back'}</Text> : null}
    </Box>
  )
}

/** Connected strip, mounted directly below the composer. */
export function AgentStrip({ cols }: { cols: number }) {
  const { sid, theme } = useStore($uiState)
  const sessions = useStore($stripSessions)
  const nav = useStore($stripNav)
  const subagents = useAgentRoster()
  const [now, setNow] = useState(Date.now)
  const rows = useMemo(() => buildStripRows(subagents, sessions, now, sid), [subagents, sessions, now, sid])
  const live = rows.some(r => r.state === 'working')

  useEffect(() => {
    if (!live) {
      return
    }

    const timer = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(timer)
  }, [live])

  useEffect(() => {
    $stripRows.set(rows)

    if (!rows.length && $stripNav.get().focused) {
      $stripNav.set({ confirmKey: null, focused: false, index: 0 })
    }
  }, [rows])

  return <AgentStripView cols={cols} confirmKey={nav.confirmKey} focused={nav.focused} index={nav.index} rows={rows} t={theme} />
}
