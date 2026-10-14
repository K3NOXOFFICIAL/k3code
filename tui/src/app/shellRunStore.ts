import { atom } from "nanostores";

import type { ShellOutputEvent } from "../gatewayTypes.js";

/** Lines of live output kept per running `!cmd`: the block shows a tail, the full output comes with the reply. */
export const SHELL_LIVE_LINES = 2000;

export interface ShellRunLive {
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
