/**
 * Wire types for the `tui_gateway` JSON-RPC surface shared by the Ink TUI, Desktop and the
 * web dashboard.
 *
 * Every shape here is GENERATED from `tui_gateway/contracts` (Python is the single source):
 * `./gateway-contract.generated.ts` carries `RpcMethods` (client→server method → params/result),
 * `ServerRequestMap` (server→client request → params/result), `GatewayEventMap` (notification
 * type → payload) and every value shape. `scripts/gen_gateway_contracts.py` regenerates it and
 * `tests/tui_gateway/contracts/test_generated.py` fails when the committed file is stale, so a field the
 * backend stops sending fails `tsc` here instead of drifting.
 *
 * This module adds only what the wire does not carry: the client-local synthetic events the TUI
 * transport publishes into the same handler stream, and the `GatewayEvent` envelope.
 */
import type { BackendGatewayEventMap } from './gateway-contract.generated.js'

export * from './gateway-contract.generated.js'

/**
 * Client-local synthetic events. Never emitted by `tui_gateway`; the Ink TUI's `gatewayClient`
 * publishes them into the same handler stream to report transport state.
 */
export interface ClientLocalGatewayEventMap {
  'dashboard.new_session_requested': { reason?: string }
  'gateway.protocol_error': { preview?: string }
  'gateway.reconnecting': { attempt?: number; delay_ms?: number }
  'gateway.start_timeout': { cwd?: string; python?: string; stderr_tail?: string }
  'gateway.stderr': { line: string }
}

/** k3code autonomy events (M4a). The Python gateway is not the generated Hermes contract, so these are declared here. */
export interface K3GatewayEventMap {
  'plan.show': {
    advisor?: string
    auto_approved?: boolean
    fanout_candidate?: boolean
    plan: string
    plan_id: string
    risk: string
    scope: string
    status: 'approved' | 'proposed' | 'rejected'
  }
  'proposal.show': { action: string; id: string; kind: 'also_setup' | 'consequence' | 'improvement'; text: string }
  'routing.escalated': { from: string; reason: string; task_kind: string; to: string }
  'scope.verdict': { fanout_candidate?: boolean; needs_plan: boolean; reason: string; risk: string; scope: string; source: string }
}

export interface GatewayEventMap extends BackendGatewayEventMap, ClientLocalGatewayEventMap, K3GatewayEventMap {}

export type GatewayEventName = keyof GatewayEventMap

/** One `event` notification's `params`. */
export interface GatewayEvent<K extends GatewayEventName = GatewayEventName> {
  /** Client-local: recovered/held during reconnect, not fresh user-facing work. */
  replayed?: boolean
  /** Client-local: the backend process's `replay_epoch` the delivering socket had adopted
   * (from `gateway.ready`) when it dispatched this event. Two sockets to one process share
   * it, so a renderer can recognise the same frame arriving on both. */
  replayEpoch?: string
  /** Registry connection whose socket delivered the event (renderer-side tag;
   * absent for the local/legacy primary path). */
  connectionId?: string
  payload?: GatewayEventMap[K]
  /** Renderer-side source tag added by the Desktop gateway registry. */
  profile?: string
  /** Per-session monotonic counter stamped by `tui_gateway/event_replay.py::_stamp_event`;
   *  absent on session-less broadcasts. */
  seq?: number
  session_id?: string
  type: K
}

/** Backend-emitted notification names (generated `GATEWAY_EVENT_TYPES`), re-exported under the
 *  name the consumers already use. */
export { GATEWAY_EVENT_TYPES as BACKEND_EVENT_NAMES } from './gateway-contract.generated.js'
