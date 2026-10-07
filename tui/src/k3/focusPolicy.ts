import { getUiState, patchUiState } from '../app/uiStore.js'
import type { GatewayEvent } from '@k3code/shared/gateway-events'

/** Event importance levels — events may carry this field to override type-based defaults. */
export type Importance = 'essential' | 'progress' | 'debug'

/** Check if an event should be shown in focus mode. */
export function shouldShowInFocusMode(event: GatewayEvent): boolean {
  // If the event explicitly carries importance, honour it
  const importance = (event as GatewayEvent & { importance?: Importance }).importance
  if (importance) {
    return importance === 'essential'
  }

  // Fall back to event-type rules
  switch (event.type) {
    // User messages are always essential
    case 'message.start':
    case 'message.delta':
    case 'message.complete':
    case 'message.interim':
      // Only the final assistant answer counts as essential in focus mode
      // (we'll filter delta/interim in the transcript renderer)
      return event.type === 'message.complete' || event.type === 'message.start'

    // Errors with a payload — the generic `error` notification
    case 'error':
      return true

    // Notifications: only warn/error break through; info toasts are progress
    case 'notification.show': {
      const payload = (event as { payload?: { level?: string } }).payload
      return payload?.level === 'error' || payload?.level === 'warn'
    }

    // Session control updates (goal/loop/heartbeat) - keep for awareness
    case 'session.control.update':
      return true

    // Tool calls and output are progress (hidden in focus mode)
    case 'tool.start':
    case 'tool.complete':
    case 'tool.generating':
    case 'tool.output_risk':
      return false

    // Todo churn is progress
    case 'todo.updated':
      return false

    // Status chatter is progress
    case 'status.update':
      return false

    // Reasoning/thinking is progress
    case 'reasoning.delta':
    case 'reasoning.available':
    case 'thinking.delta':
      return false

    // Gateway/session metadata events
    case 'gateway.ready':
    case 'skin.changed':
    case 'session.info':
    case 'session.title':
    case 'session.resume_progress':
    case 'session.reclaimed':
    case 'session.usage':
    case 'notice':
    case 'gateway.reconnecting':
    case 'gateway.stderr':
      return false

    // Sub-agent/background events are progress
    case 'subagent.spawn_requested':
    case 'subagent.start':
    case 'subagent.progress':
    case 'subagent.thinking':
    case 'subagent.tool':
    case 'subagent.complete':
    case 'background.complete':
    case 'btw.complete':
    case 'preview.restart.complete':
    case 'preview.restart.progress':
    case 'moa.reference':
    case 'moa.aggregating':
    case 'moa.progress':
    case 'moa.phase':
      return false

    // Cut events - never show
    case 'billing.step_up.verification':
    case 'voice.status':
    case 'voice.transcript':
    case 'voice.interrupted':
    case 'wake.detected':
    case 'pet.changed':
    case 'pet.generate.progress':
    case 'pet.hatch.progress':
    case 'preview.open':
    case 'preview.close':
    case 'layout.apply':
    case 'pane.reveal':
    case 'message.reaction':
    case 'agent.terminal.output':
    case 'terminal.close':
    case 'browser.progress':
    case 'browser.controller.command':
    case 'browser.controller.cancel':
    case 'cron.changed':
    case 'sessions.changed':
    case 'projects.changed':
    case 'pairing.changed':
    case 'bot_relay.outbox.pending':
    case 'tip.show':
    case 'review.summary':
    case 'reaction':
    case 'setup.ready':
    case 'connection.request':
    case 'connection.update':
    case 'request.cancel':
      return false

    // Default: hide unknown events in focus mode
    default:
      return false
  }
}

/** Get current focus mode state. */
export function isFocusMode(): boolean {
  return getUiState().focusView === true
}

/** Toggle focus mode and persist to config if available. */
export async function toggleFocusMode(gateway?: { rpc: (method: string, params: any) => Promise<any> }): Promise<boolean> {
  const newValue = !isFocusMode()
  patchUiState({ focusView: newValue })

  // Try to persist to config
  if (gateway) {
    try {
      await gateway.rpc('config.set', { key: 'focus', value: newValue ? 'on' : 'off' })
    } catch {
      // Config not available or failed - keep in memory only
    }
  }

  return newValue
}

/** Initialize focus mode from config on startup. */
export async function initFocusMode(gateway?: { rpc: (method: string, params: any) => Promise<any> }): Promise<void> {
  if (!gateway) {
    return
  }

  try {
    const result = await gateway.rpc('config.get', { key: 'focus_view' })
    if (result?.value !== undefined) {
      patchUiState({ focusView: result.value === '1' || result.value === true })
    }
  } catch {
    // Config not available - keep default (false)
  }
}
