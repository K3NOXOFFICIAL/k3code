import type { ServerRequest } from "@k3code/shared/json-rpc-channel";

// Live server→client requests (clarify, approval, sudo, …) keyed by request
// id. Overlay state keeps only the id; answering resolves the stored request
// so a re-delivered (`open_requests`) request with the same id reuses the
// same card. Module-level, like the overlay store: the gateway client and
// the Ink handlers share one instance per process.
const open = new Map<string, ServerRequest>();

export function rememberServerRequest(request: ServerRequest): void {
  open.set(request.id, request);
}

export function forgetServerRequest(id: string): void {
  open.delete(id);
}

/** Answer request `id` and forget it. False when nothing is open under that id (expired / already answered). */
export function respondToServerRequest(
  id: string,
  result: Record<string, unknown>,
): boolean {
  const request = open.get(id);

  if (!request) {
    return false;
  }

  open.delete(id);
  request.respond(result);

  return true;
}

/** The open requests session `sid` asked (`params.session_id`), oldest first. */
export function serverRequestsForSession(sid: string): ServerRequest[] {
  return [...open.values()].filter((r) => r.params.session_id === sid);
}

/**
 * Forget the requests of a session this client just left. The gateway sends
 * `request.cancel` only to clients attached to the session, so an entry kept
 * here could be answered or expire elsewhere meanwhile; the gateway re-sends
 * whatever is still open when a client attaches to the session again.
 */
export function forgetServerRequestsForSession(sid: string): void {
  for (const r of serverRequestsForSession(sid)) {
    open.delete(r.id);
  }
}

export function hasOpenServerRequest(id: string): boolean {
  return open.has(id);
}

export function resetServerRequestsForTests(): void {
  open.clear();
}
