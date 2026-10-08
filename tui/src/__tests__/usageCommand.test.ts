import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import type { SessionUsageResponse } from '../gatewayTypes.js'

const usageCommand = sessionCommands.find(cmd => cmd.name === 'usage')!

/** Build a ctx whose rpc routes by method name to a supplied map of results. */
const buildCtx = (results: Record<string, unknown>) => {
  const sys = vi.fn()
  const panel = vi.fn()

  const rpc = vi.fn((method: string) => Promise.resolve(results[method]))

  const ctx = {
    gateway: { rpc },
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), panel, sys }
  }

  const run = async (arg: string) => {
    usageCommand.run(arg, ctx as any, 'usage')
    await rpc.mock.results[0]?.value
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, panel, run, sys }
}

const baseUsage = (overrides: Partial<SessionUsageResponse> = {}): SessionUsageResponse =>
  ({ calls: 0, input: 0, output: 0, total: 0, ...overrides }) as SessionUsageResponse

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

const usagePanel = (panel: ReturnType<typeof vi.fn>) => {
  const sections = panel.mock.calls.find(c => c[0] === 'Usage')?.[1] as { rows?: [string, string][]; text?: string }[] | undefined

  return (sections ?? []).map(s => s.text ?? (s.rows ?? []).map(([k, v]) => `${k}: ${v}`).join('\n')).join('\n')
}

describe('/usage slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows "no API calls yet" only when there are no calls', async () => {
    const empty = buildCtx({ 'session.usage': baseUsage({ calls: 0 }) })
    await empty.run('')
    expect(printed(empty.sys)).toContain('no API calls yet')

    const withCalls = buildCtx({ 'session.usage': baseUsage({ calls: 3 }) })
    await withCalls.run('')
    expect(printed(withCalls.sys)).not.toContain('no API calls yet')
  })

  it('renders the token/call usage panel (billing balance panel is cut)', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        calls: 12,
        compressions: 2,
        context_estimated: true,
        context_max: 200_000,
        context_percent: 40,
        context_used: 80_000,
        input: 1000,
        model: 'k3-model',
        output: 500,
        total: 1500
      })
    })

    await run('')

    const body = usagePanel(panel)
    expect(body).toContain('Model: k3-model')
    expect(body).toContain('Input tokens: 1,000')
    expect(body).toContain('Output tokens: 500')
    expect(body).toContain('Total tokens: 1,500')
    expect(body).toContain('API calls: 12')
    expect(body).toContain('Context: ~80,000 / 200,000 (~40%)')
    expect(body).toContain('Compressions: 2')
  })
})
