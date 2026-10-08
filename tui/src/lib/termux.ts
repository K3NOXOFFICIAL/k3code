const TERMUX_PREFIX = '/data/data/com.termux/files/usr'

const truthy = (value?: string): boolean => /^(?:1|true|yes|on)$/i.test(String(value ?? '').trim())

export const isTermuxEnv = (env: NodeJS.ProcessEnv = process.env): boolean => {
  const prefix = String(env.PREFIX ?? '')

  return Boolean(env.TERMUX_VERSION) || prefix.includes(TERMUX_PREFIX)
}

/**
 * Return true when k3code should enable Termux-focused TUI defaults.
 *
 * Defaults to on in Termux, with an explicit opt-out for debugging:
 *   K3CODE_TUI_TERMUX_MODE=0
 */
export const isTermuxTuiMode = (env: NodeJS.ProcessEnv = process.env): boolean => {
  if (!isTermuxEnv(env)) {
    return false
  }

  const override = String(env.K3CODE_TUI_TERMUX_MODE ?? '')
    .trim()
    .toLowerCase()

  if (override) {
    return truthy(override)
  }

  return true
}
