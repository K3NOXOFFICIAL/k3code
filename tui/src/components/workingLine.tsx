import { Text } from '@k3code/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { $sessionOutputTokens } from '../app/outputTokensStore.js'
import { isReducedMotion } from '../lib/animation.js'
import {
  advanceRotation,
  formatWorkingLine,
  startRotation,
  WORKING_TICK_MS,
  workingEffort,
  workingGlyph,
  workingMessage
} from '../lib/workingLine.js'
import type { Theme } from '../theme.js'

interface WorkingLineProps {
  busy: boolean
  /** `session.info.reasoning_effort`. Shown only when the gateway sends a real level. */
  effort?: null | string
  /** Turn start from the status clock; falls back to mount time. */
  startedAt?: null | number
  t: Theme
}

/** One row above the composer while a turn runs. Renders nothing when idle.
 *  Owns its own timer, so a tick re-renders this row and nothing else. */
export function WorkingLine({ busy, effort, startedAt, t }: WorkingLineProps) {
  if (!busy) {
    return null
  }

  return <ActiveWorkingLine effort={effort} startedAt={startedAt} t={t} />
}

const random = () => Math.random()

function ActiveWorkingLine({ effort, startedAt, t }: Omit<WorkingLineProps, 'busy'>) {
  // Read once per mount: reduced motion is a launch-time choice, not a live toggle.
  const [reduced] = useState(() => isReducedMotion())
  const [origin] = useState(() => startedAt ?? Date.now())
  const [tick, setTick] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  const [rotation, setRotation] = useState(() => startRotation(random))
  const sessionTokens = useStore($sessionOutputTokens)
  // Baseline is the session total when this turn started (this component mounts per turn).
  const [tokenBaseline] = useState(() => $sessionOutputTokens.get())

  useEffect(() => {
    // Animated: one timer drives the glyph, the clock and the message rotation.
    // Reduced motion: only the clock ticks, once a second. Glyph and message stay put.
    const id = setInterval(
      () => {
        const clock = Date.now()

        setNow(clock)

        if (!reduced) {
          setTick(n => n + 1)
          setRotation(prev => advanceRotation(prev, clock - origin, random))
        }
      },
      reduced ? 1000 : WORKING_TICK_MS
    )

    return () => clearInterval(id)
  }, [reduced, origin])

  const elapsedMs = Math.max(0, now - origin)

  const line = formatWorkingLine({
    effort: workingEffort(effort),
    elapsedMs,
    glyph: workingGlyph(tick, reduced),
    message: workingMessage(rotation.index),
    outputTokens: Math.max(0, sessionTokens - tokenBaseline)
  })

  return (
    <Text color={t.color.accent} wrap="truncate-end">
      {line}
    </Text>
  )
}
