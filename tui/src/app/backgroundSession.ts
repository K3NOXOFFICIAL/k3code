import { STARTUP_WORKSPACE_CWD } from "../config/env.js";

import type { GatewayRpc } from "./interfaces.js";
import { getUiState } from "./uiStore.js";

/**
 * The directory a /bg or Ctrl+B session starts in. The session's own cwd (`info.cwd`) is right both for the session
 * `k3code agents` creates (it gets the shell's cwd through session.create) and after the user switches to a session
 * of another project; K3CODE_TUI_CWD only stands in when no session info has arrived yet.
 */
export const backgroundCwd = (): string =>
  getUiState().info?.cwd || STARTUP_WORKSPACE_CWD || "";

/** Ctrl+B: hand the running turn to a background session (started in the same directory /bg would use). */
export const sendTurnToBackground = (rpc: GatewayRpc, sid: string) => {
  const cwd = backgroundCwd();

  return rpc<{ new_session_id?: string }>("prompt.background", {
    session_id: sid,
    ...(cwd ? { cwd } : {}),
  });
};
