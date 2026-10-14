/**
 * Which emulator is hosting this TUI, when the host tells us.
 *
 * `hermes dashboard` spawns the TUI behind a PTY and mirrors it into xterm.js
 * in the browser; the bridge sets K3CODE_PTY_HOST=dashboard (HERMES_PTY_HOST still works) on the child
 * (hermes_cli/pty_bridge.py — keep the two constants in sync). Native
 * terminals never set it.
 */
export const PTY_HOST_ENV = "K3CODE_PTY_HOST";
const LEGACY_PTY_HOST_ENV = "HERMES_PTY_HOST";
export const PTY_HOST_DASHBOARD = "dashboard";

export const isDashboardHosted = (
  env: NodeJS.ProcessEnv = process.env,
): boolean =>
  (env[PTY_HOST_ENV] ?? env[LEGACY_PTY_HOST_ENV]) === PTY_HOST_DASHBOARD;
