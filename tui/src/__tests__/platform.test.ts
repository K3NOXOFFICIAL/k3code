import { describe, expect, it } from 'vitest'

import { isActionMod, isCopyShortcut, isMac, isMacActionFallback } from '../lib/platform.js'

// These cover the non-macOS (host-native on the Linux CI lane) arms. The
// macOS arms key off a module-level `isMac` and would need the interpreter to
// believe it is on darwin — see AGENTS.md "Don't fake the host OS".
const describeHost = describe.skipIf(isMac)

describeHost('platform action modifier', () => {
  it('still uses Ctrl as the action modifier on non-macOS', () => {
    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(false)
  })
})

describeHost('isCopyShortcut', () => {
  it('keeps Ctrl+C as the local non-macOS copy chord', () => {
    expect(isCopyShortcut({ ctrl: true, meta: false, super: false }, 'c', {})).toBe(true)
  })

  it('accepts client Cmd+C over SSH even when running on Linux', () => {
    const env = { SSH_CONNECTION: '1 2 3 4' } as NodeJS.ProcessEnv

    expect(isCopyShortcut({ ctrl: false, meta: false, super: true }, 'c', env)).toBe(true)
    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', env)).toBe(true)
  })

  it('does not treat local Linux Alt+C as copy', () => {
    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', {})).toBe(false)
  })
})

describeHost('isMacActionFallback', () => {
  it('is a no-op on non-macOS (Linux routes Ctrl+K/W through isActionMod directly)', () => {
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(false)
  })
})
