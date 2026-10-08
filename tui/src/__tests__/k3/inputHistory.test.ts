import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { shouldEnterStrip } from '../../k3/agentStripStore.js'

describe('per-project input history', () => {
  let home: string

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'k3h-'))
    process.env.K3CODE_HOME = home
    vi.resetModules()
  })
  afterEach(() => {
    delete process.env.K3CODE_HOME
    rmSync(home, { force: true, recursive: true })
  })

  it('persists per project and reloads in order (↑ walks this list backwards)', async () => {
    const h = await import('../../lib/history.js')
    const a = h.projectHistoryKey('/p/a')
    const b = h.projectHistoryKey('/p/b')

    h.append('one', a)
    h.append('two\nlines', a)
    h.append('other', b)
    vi.resetModules()
    const h2 = await import('../../lib/history.js')

    expect(h2.load(a)).toEqual(['one', 'two\nlines'])
    expect(h2.load(b)).toEqual(['other'])
  })

  it('history cycle takes priority over strip entry', () => {
    // mid-cycle (historyIdx set) or non-empty input: ↓ goes forward in history, never into the strip
    expect(shouldEnterStrip({ historyIdx: 0, input: 'one', rows: 3 })).toBe(false)
    expect(shouldEnterStrip({ historyIdx: null, input: 'draft', rows: 3 })).toBe(false)
    expect(shouldEnterStrip({ historyIdx: null, input: '', rows: 3 })).toBe(true)
  })
})
