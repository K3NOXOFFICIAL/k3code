import { beforeEach, describe, expect, it } from 'vitest'

import {
  $sessionOutputTokens,
  $streamedOutputChars,
  $turnTokenBaseline,
  addStreamedText,
  estimateTokens,
  liveTurnOutputTokens,
  markTurnStart,
  settleOutput
} from '../app/outputTokensStore.js'

const reset = () => {
  $sessionOutputTokens.set(0)
  $turnTokenBaseline.set(0)
  $streamedOutputChars.set(0)
}

/** What the working line shows for the running turn. */
const shown = () => liveTurnOutputTokens($sessionOutputTokens.get(), $turnTokenBaseline.get(), $streamedOutputChars.get())

describe('output token estimate', () => {
  beforeEach(reset)

  it('is about four characters per token, rounded up', () => {
    expect(estimateTokens(0)).toBe(0)
    expect(estimateTokens(1)).toBe(1)
    expect(estimateTokens(4)).toBe(1)
    expect(estimateTokens(5)).toBe(2)
    expect(estimateTokens(400)).toBe(100)
  })

  it('deltas add up to the estimate of the whole text', () => {
    markTurnStart()
    addStreamedText('Hel')
    addStreamedText('lo wor')
    addStreamedText('')
    addStreamedText(undefined)
    addStreamedText('ld')

    expect($streamedOutputChars.get()).toBe('Hello world'.length)
    expect(shown()).toBe(estimateTokens('Hello world'.length))
    expect(shown()).toBe(3)
  })

  it('shows the estimate while the call streams, on top of finished calls', () => {
    $sessionOutputTokens.set(1000)
    markTurnStart()
    addStreamedText('x'.repeat(400))

    expect(shown()).toBe(100)

    addStreamedText('y'.repeat(40))

    expect(shown()).toBe(110)
  })

  it('is replaced by the real count when the call reports its usage', () => {
    markTurnStart()
    addStreamedText('x'.repeat(400))
    expect(shown()).toBe(100)

    settleOutput(87)

    expect($streamedOutputChars.get()).toBe(0)
    expect(shown()).toBe(87)
  })

  it('reconciles on done: a call that reports no usage keeps its estimate, a reported one wins', () => {
    markTurnStart()
    addStreamedText('x'.repeat(40))
    settleOutput(0)

    expect(shown()).toBe(10)

    markTurnStart()
    addStreamedText('x'.repeat(400))
    settleOutput(90)
    // message.complete: nothing is still streaming, so nothing is added again.
    settleOutput(0)

    expect(shown()).toBe(90)
  })

  it('a new turn starts from the session total with no leftover estimate', () => {
    markTurnStart()
    addStreamedText('x'.repeat(80))
    settleOutput(0)
    markTurnStart()

    expect(shown()).toBe(0)
  })
})
