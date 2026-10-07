import { atom } from 'nanostores'

/** Output tokens the gateway has reported across completed LLM calls this
 *  session. The gateway only reports a call's count when that call finishes
 *  (`session.usage`), so this grows in steps, once per call. The working line
 *  takes a per-turn delta from it. */
export const $sessionOutputTokens = atom(0)

export const addOutputTokens = (count: number) => {
  if (Number.isFinite(count) && count > 0) {
    $sessionOutputTokens.set($sessionOutputTokens.get() + count)
  }
}

/** Completion tokens from a `session.usage` payload. The core gateway sends
 *  `completion_tokens`; the generated `Usage` type calls the same count `output`
 *  or `completion`, so accept either. */
export const completionTokensOf = (usage: Record<string, unknown> | null | undefined): number => {
  const value = usage?.completion_tokens ?? usage?.completion ?? usage?.output ?? 0

  return Number(value) || 0
}
