import { compactNumber } from '@k3code/shared/format'
import { isReasoningEffort } from '@k3code/shared/reasoning-effort'

import { WORKING_MESSAGES } from '../content/workingMessages.js'
import { fmtDuration } from '../domain/messages.js'

import { MIN_ANIMATION_TICK_MS } from './animation.js'

/** A `Math.random`-shaped source. Tests pass a fixed sequence. */
export type Rng = () => number

/** Spinner glyph cycle (single-column dingbats). */
export const SPINNER_FRAMES: readonly string[] = ['·', '✢', '✳', '✶', '✻', '✽']

/** Glyph shown instead of the cycle under reduced motion. */
export const STATIC_GLYPH = '✻'

/** The working line's own timer period. Never below the animation floor. */
export const WORKING_TICK_MS = MIN_ANIMATION_TICK_MS

/** A new message every this many ms of a turn (a few seconds, jittered by the pool). */
export const WORKING_ROTATE_MS = 3500

const mod = (n: number, m: number) => ((n % m) + m) % m

export const workingGlyph = (tick: number, reduced: boolean): string =>
  reduced ? STATIC_GLYPH : (SPINNER_FRAMES[mod(tick, SPINNER_FRAMES.length)] ?? STATIC_GLYPH)

/** Pick an index in [0, count). With `exclude`, never returns that index (when count > 1). */
export const pickMessageIndex = (rng: Rng, count: number, exclude = -1): number => {
  if (count <= 1) {
    return 0
  }

  if (exclude < 0 || exclude >= count) {
    return Math.min(count - 1, Math.floor(rng() * count))
  }

  // Draw from the other count-1 slots, then shift past `exclude`.
  const draw = Math.min(count - 2, Math.floor(rng() * (count - 1)))

  return draw >= exclude ? draw + 1 : draw
}

export interface MessageRotation {
  /** Index into WORKING_MESSAGES. */
  index: number
  /** Rotation segment the current message belongs to (elapsed / WORKING_ROTATE_MS). */
  segment: number
}

export const startRotation = (rng: Rng, count = WORKING_MESSAGES.length): MessageRotation => ({
  index: pickMessageIndex(rng, count),
  segment: 0
})

/** Move to a new message when the elapsed time enters a new rotation segment.
 *  The new message is never the one shown before. Returns the same object when nothing changes. */
export const advanceRotation = (
  state: MessageRotation,
  elapsedMs: number,
  rng: Rng,
  count = WORKING_MESSAGES.length
): MessageRotation => {
  const segment = Math.floor(Math.max(0, elapsedMs) / WORKING_ROTATE_MS)

  if (segment <= state.segment) {
    return state
  }

  return { index: pickMessageIndex(rng, count, state.index), segment }
}

export const workingMessage = (index: number): string =>
  WORKING_MESSAGES[mod(index, WORKING_MESSAGES.length)] ?? 'Working'

/** The thinking-effort level the gateway reports, or '' when it reports none.
 *  Only a real level counts: `none` (thinking off) and unset/default are hidden. */
export const workingEffort = (raw: null | string | undefined): string => {
  const value = String(raw ?? '')
    .trim()
    .toLowerCase()

  return isReasoningEffort(value) ? value : ''
}

export interface WorkingLineParts {
  /** Turn elapsed time in ms. */
  elapsedMs: number
  /** Output tokens the gateway has reported for this turn so far (completed calls). */
  outputTokens?: number
  /** Thinking effort the gateway reports, already normalised by `workingEffort`. */
  effort?: string
  glyph: string
  /** The message text, without the trailing ellipsis. */
  message: string
}

/** `✢ Deciphering… (41m 1s · ↓ 135.2k tokens · thinking with xhigh effort)`.
 *  Each optional part is omitted when the gateway did not provide it. */
export const formatWorkingLine = ({
  elapsedMs,
  outputTokens = 0,
  effort = '',
  glyph,
  message
}: WorkingLineParts): string => {
  const detail: string[] = [fmtDuration(elapsedMs)]

  if (outputTokens > 0) {
    detail.push(`↓ ${compactNumber(outputTokens)} tokens`)
  }

  if (effort) {
    detail.push(`thinking with ${effort} effort`)
  }

  return `${glyph} ${message}… (${detail.join(' · ')})`
}
