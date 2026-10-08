import { PassThrough } from 'node:stream'

import { renderSync } from '@k3code/ink'
import React from 'react'
import stripAnsi from 'strip-ansi'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PET_NAMES, PETS } from '../content/pets.js'
import { TerminalPet } from '../components/terminalPet.js'
import { WorkingLine } from '../components/workingLine.js'
import { PET_DONE_HOLD_MS, PET_TICK_MS } from '../lib/terminalPet.js'
import { WORKING_TICK_MS } from '../lib/workingLine.js'
import { DEFAULT_THEME } from '../theme.js'

// React schedules its work on the real setImmediate. Capture it before fake timers replace it, so a
// test can let a fake-timer tick reach the screen: advance the clock, then `await settle()`.
const realSetImmediate = globalThis.setImmediate
const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await new Promise<void>(resolve => realSetImmediate(() => resolve()))
  }
}

const mount = (element: React.ReactElement) => {
  const stdout = Object.assign(new PassThrough(), { columns: 120, rows: 10 })
  const frames: string[] = []
  stdout.on('data', chunk => frames.push(chunk.toString()))

  const view = renderSync(element, {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: new PassThrough() as unknown as NodeJS.ReadStream
  })

  return {
    output: () => stripAnsi(frames.join('')),
    rerender: (next: React.ReactElement) => view.rerender(next),
    unmount: () => {
      view.unmount()
      view.cleanup()
    }
  }
}

/** Mount and report every setInterval delay armed while mounting. */
const mountWithIntervals = (element: React.ReactElement) => {
  const spy = vi.spyOn(globalThis, 'setInterval')
  const view = mount(element)
  const delays = spy.mock.calls.map(call => Number(call[1]))

  spy.mockRestore()

  return { delays, view }
}

describe('reduced motion (K3_NO_ANIMATION=1)', () => {
  const saved = process.env.K3_NO_ANIMATION

  beforeEach(() => {
    process.env.K3_NO_ANIMATION = '1'
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()

    if (saved === undefined) {
      delete process.env.K3_NO_ANIMATION
    } else {
      process.env.K3_NO_ANIMATION = saved
    }
  })

  it('shows the static glyph and arms no animation timer (only a 1 s clock)', () => {
    const { delays, view } = mountWithIntervals(
      <WorkingLine busy effort="xhigh" startedAt={Date.now()} t={DEFAULT_THEME} />
    )

    expect(view.output()).toContain('✻ ')
    expect(view.output()).toContain('thinking with xhigh effort')
    expect(delays).toEqual([1000])

    view.unmount()
  })

  it('shows a fixed pet frame and arms no pet timer', () => {
    const { delays, view } = mountWithIntervals(
      <TerminalPet busy={false} name="crab" needsInput={false} t={DEFAULT_THEME} />
    )

    expect(delays.filter(delay => delay < 1000)).toEqual([])
    expect(view.output()).toContain(PETS.crab.idle[0][0].trimEnd())

    view.unmount()
  })
})

describe('animated timers', () => {
  const saved = process.env.K3_NO_ANIMATION

  beforeEach(() => {
    delete process.env.K3_NO_ANIMATION
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()

    if (saved === undefined) {
      delete process.env.K3_NO_ANIMATION
    } else {
      process.env.K3_NO_ANIMATION = saved
    }
  })

  it('the working line animates on a timer no faster than 250 ms', () => {
    const { delays, view } = mountWithIntervals(<WorkingLine busy startedAt={Date.now()} t={DEFAULT_THEME} />)

    expect(delays).toContain(250)
    expect(Math.min(...delays)).toBeGreaterThanOrEqual(250)

    view.unmount()
  })

  it('the working line timer is cleared on unmount', () => {
    const before = vi.getTimerCount()
    const view = mount(<WorkingLine busy startedAt={Date.now()} t={DEFAULT_THEME} />)

    expect(vi.getTimerCount()).toBeGreaterThan(before)

    view.unmount()

    expect(vi.getTimerCount()).toBe(before)
  })

  it('the pet timer is cleared on unmount', () => {
    const before = vi.getTimerCount()
    const view = mount(<TerminalPet busy name="owl" needsInput={false} t={DEFAULT_THEME} />)

    expect(vi.getTimerCount()).toBeGreaterThan(before)

    view.unmount()

    expect(vi.getTimerCount()).toBe(before)
  })

  it('an idle pet arms no timer and shows its still frame', () => {
    const { delays, view } = mountWithIntervals(
      <TerminalPet busy={false} name="owl" needsInput={false} t={DEFAULT_THEME} />
    )

    expect(delays.filter(delay => delay < 1000)).toEqual([])
    expect(view.output()).toContain(PETS.owl.idle[0][1].trimEnd())

    view.unmount()
  })

  it('a finished turn keeps the pet timer through done, then stops it once the hold has passed', async () => {
    const before = vi.getTimerCount()
    const view = mount(<TerminalPet busy name="owl" needsInput={false} t={DEFAULT_THEME} />)

    expect(vi.getTimerCount()).toBeGreaterThan(before)

    view.rerender(<TerminalPet busy={false} name="owl" needsInput={false} t={DEFAULT_THEME} />)
    await settle()

    expect(vi.getTimerCount()).toBeGreaterThan(before)

    vi.advanceTimersByTime(PET_DONE_HOLD_MS + PET_TICK_MS)
    await settle()

    expect(vi.getTimerCount()).toBe(before)
    expect(view.output()).toContain(PETS.owl.idle[0][1].trimEnd())

    view.unmount()
  })

  it('a pet tick re-renders the pet, not the component that holds it', async () => {
    let parentRenders = 0
    const Parent = () => {
      parentRenders += 1

      return <TerminalPet busy name="owl" needsInput={false} t={DEFAULT_THEME} />
    }
    const view = mount(<Parent />)
    const first = view.output()

    vi.advanceTimersByTime(PET_TICK_MS * 3)
    await settle()

    expect(view.output()).not.toBe(first)
    expect(parentRenders).toBe(1)

    view.unmount()
  })

  it('a working-line tick re-renders the line, not the component that holds it', async () => {
    let parentRenders = 0
    const Parent = () => {
      parentRenders += 1

      return <WorkingLine busy startedAt={Date.now()} t={DEFAULT_THEME} />
    }
    const view = mount(<Parent />)
    const first = view.output()

    vi.advanceTimersByTime(WORKING_TICK_MS * 3)
    await settle()

    expect(view.output()).not.toBe(first)
    expect(parentRenders).toBe(1)

    view.unmount()
  })

  it('renders nothing for the working line while idle', () => {
    const view = mount(<WorkingLine busy={false} t={DEFAULT_THEME} />)

    expect(view.output().trim()).toBe('')

    view.unmount()
  })

  it('every pet name renders its idle frame', () => {
    for (const name of PET_NAMES) {
      const view = mount(<TerminalPet busy={false} name={name} needsInput={false} t={DEFAULT_THEME} />)

      expect(view.output()).toContain(PETS[name].idle[0][1].trimEnd())

      view.unmount()
    }
  })
})
