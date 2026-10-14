import { atom } from "nanostores";

import type { GatewayClient } from "../gatewayClient.js";
import type { ShellOutputEvent } from "../gatewayTypes.js";
import { rpcErrorMessage } from "../lib/rpc.js";

/** Lines of live output kept per running `!cmd`: the block shows a tail, the full output comes with the reply. */
export const SHELL_LIVE_LINES = 2000;

export interface ShellRunLive {
  /** A `shell.cancel` went out for it: the next cancel press passes through to the turn. */
  cancelling?: boolean;
  lines: string[];
  notice?: string;
  /** The last line is still open: the next chunk continues it. */
  open: boolean;
}

/** The `!cmd` runs this client started and that have not replied yet, by exec id, with their streamed output. Ids
 *  another attached client started are never in it, so their `shell.output` events are ignored. */
export const $shellRuns = atom<Record<string, ShellRunLive>>({});

/** Running `!cmd`s: the input handlers cancel these before they interrupt a turn. */
export const shellRunCount = () => Object.keys($shellRuns.get()).length;

export const anyShellRunning = () => shellRunCount() > 0;

/** The running `!cmd`s no `shell.cancel` has gone out for yet. */
export const shellRunsToCancel = () =>
  Object.entries($shellRuns.get())
    .filter(([, run]) => !run.cancelling)
    .map(([execId]) => execId);

/** Flag (or unflag) a run as cancelling; a run that already replied is left gone. */
export const markShellCancelling = (execId: string, cancelling = true) => {
  const runs = $shellRuns.get();
  const run = runs[execId];

  if (run) {
    $shellRuns.set({ ...runs, [execId]: { ...run, cancelling } });
  }
};

/**
 * A cancel press (Ctrl+C, Esc Esc, Ctrl+K, a double Enter) goes to the running `!cmd`s first: one `shell.cancel` for
 * the session stops them all. True when it did; false when every run is already cancelling (or none runs), so the
 * press does what it did before, and a second press still reaches the turn. Not `interruptTurn`: its idle() would
 * clear `busy` under a process that is still running. A failed request unflags the runs, so a later press retries.
 */
export const cancelShellRuns = ({
  gw,
  sid,
  sys,
}: {
  gw: Pick<GatewayClient, "request">;
  sid: string;
  sys: (text: string) => void;
}): boolean => {
  const ids = shellRunsToCancel();

  if (!ids.length) {
    return false;
  }

  ids.forEach((execId) => markShellCancelling(execId));
  sys("cancelling the running ! command …");
  gw.request("shell.cancel", { session_id: sid }).catch((e: unknown) => {
    ids.forEach((execId) => markShellCancelling(execId, false));
    sys(`error: ${rpcErrorMessage(e)}`);
  });

  return true;
};

export const startShellRun = (execId: string) =>
  $shellRuns.set({ ...$shellRuns.get(), [execId]: { lines: [], open: false } });

export const finishShellRun = (execId: string) => {
  const { [execId]: _done, ...rest } = $shellRuns.get();

  $shellRuns.set(rest);
};

/** Append one `shell.output` event to its run; false (and nothing changes) for an exec id this client did not start. */
export const appendShellOutput = (ev: ShellOutputEvent): boolean => {
  const runs = $shellRuns.get();
  const run = runs[ev.exec_id];

  if (!run) {
    return false;
  }

  if (ev.notice) {
    $shellRuns.set({ ...runs, [ev.exec_id]: { ...run, notice: ev.notice } });

    return true;
  }

  if (!ev.text) {
    return true;
  }

  const parts = ev.text.split("\n");
  const lines = [...run.lines];

  if (run.open && lines.length) {
    lines[lines.length - 1] += parts.shift()!;
  }

  lines.push(...parts);

  // A chunk ending in a newline leaves an empty last part: the line is closed.
  const open = lines[lines.length - 1] !== "";

  if (!open) {
    lines.pop();
  }

  $shellRuns.set({
    ...runs,
    [ev.exec_id]: { ...run, lines: lines.slice(-SHELL_LIVE_LINES), open },
  });

  return true;
};

/** The `!cmd` blocks the user expanded, by exec id. A store, not component state: transcript rows remount on resize,
 *  on a patch and when virtualized away, and the height estimator has to read it for rows that are not mounted. */
export const $shellExpanded = atom<Record<string, true>>({});

export const toggleShellExpanded = (execId: string) => {
  const { [execId]: was, ...rest } = $shellExpanded.get();

  $shellExpanded.set(was ? rest : { ...rest, [execId]: true });
};

export const resetShellRunsForTests = () => {
  $shellRuns.set({});
  $shellExpanded.set({});
};
