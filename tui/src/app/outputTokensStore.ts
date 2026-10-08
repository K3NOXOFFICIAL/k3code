import { atom } from 'nanostores'

/** Streamed reply text is about four characters per output token. */
export const CHARS_PER_TOKEN = 4

/** Output-token estimate for `chars` of streamed text, rounded up. */
export const estimateTokens = (chars: number): number => (chars > 0 ? Math.ceil(chars / CHARS_PER_TOKEN) : 0)

/** Output tokens the gateway has reported across completed LLM calls this
 *  session. The gateway reports a call's count only when that call finishes
 *  (`session.usage`), so this grows in steps, once per call. */
export const $sessionOutputTokens = atom(0)

/** Characters of reply text streamed (`message.delta`) for the LLM call in
 *  flight. The working line shows them as an estimate until that call's real
 *  count arrives, which replaces the estimate. */
export const $streamedOutputChars = atom(0)

/** The session total when the current turn started. Kept outside the working
 *  line, which unmounts while an approval or other overlay is open, so the
 *  per-turn count survives a remount. */
export const $turnTokenBaseline = atom(0)

export const markTurnStart = () => {
  $turnTokenBaseline.set($sessionOutputTokens.get())
  $streamedOutputChars.set(0)
}

/** Count one `message.delta` chunk toward the estimate. */
export const addStreamedText = (text: unknown) => {
  if (typeof text === 'string' && text.length > 0) {
    $streamedOutputChars.set($streamedOutputChars.get() + text.length)
  }
}

export const addOutputTokens = (count: number) => {
  if (Number.isFinite(count) && count > 0) {
    $sessionOutputTokens.set($sessionOutputTokens.get() + count)
  }
}

/** An LLM call has ended. `reported` is its real completion count from
 *  `session.usage`; the text streamed in that call is no longer an estimate.
 *  With no usage reported, the estimate stands as the call's count. */
export const settleOutput = (reported: number) => {
  const pending = $streamedOutputChars.get()

  $streamedOutputChars.set(0)
  addOutputTokens(reported > 0 ? reported : estimateTokens(pending))
}

/** Tokens to show for the running turn: real counts for the calls that have
 *  finished, plus the estimate for the text still streaming. */
export const liveTurnOutputTokens = (total: number, baseline: number, pendingChars: number): number =>
  Math.max(0, total - baseline) + estimateTokens(pendingChars)

/** Completion tokens from a `session.usage` payload. The core gateway sends
 *  `completion_tokens`; the generated `Usage` type calls the same count `output`
 *  or `completion`, so accept either. */
export const completionTokensOf = (usage: Record<string, unknown> | null | undefined): number => {
  const value = usage?.completion_tokens ?? usage?.completion ?? usage?.output ?? 0

  return Number(value) || 0
}
