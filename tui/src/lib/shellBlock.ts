import { stripAnsi } from "@k3code/shared/ansi";

import type { ShellRun } from "../types.js";

/** Output over this many lines collapses to its head and tail. */
export const SHELL_COLLAPSE_OVER = 40;
export const SHELL_HEAD_LINES = 5;
export const SHELL_TAIL_LINES = 25;
/** Live lines a running block shows. */
export const SHELL_LIVE_TAIL = 25;

export interface ShellOutputRow {
  err: boolean;
  text: string;
}

export type ShellBodyRow = ShellOutputRow | { hidden: number };

export interface ShellBlockModel {
  /** Output is long enough to collapse: the header toggles. */
  collapsible: boolean;
  command: string;
  expanded: boolean;
  /** The last row: `running…`, or the exit code and tags; null when there is nothing to say. */
  footer: null | { running: true } | { code?: number; tags: string[] };
  rows: ShellBodyRow[];
}

/** One output stream as display lines: escapes stripped, a `\r` keeps what a terminal would (the text after it),
 *  one trailing newline dropped, empty output is no lines. */
export const shellOutputLines = (text: string | undefined): string[] => {
  if (!text) {
    return [];
  }

  // `\r` before stripping: the shared strip drops it with the escapes.
  const unix = text.replace(/\r\n/g, "\n");
  const body = unix.endsWith("\n") ? unix.slice(0, -1) : unix;

  return body.split("\n").map((line) => {
    const cr = line.lastIndexOf("\r", line.length - 2);

    return stripAnsi((cr >= 0 ? line.slice(cr + 1) : line).replace(/\r$/, ""));
  });
};

/** What a `!cmd` block shows: a pure function of the run, its live lines (while running) and its expand state. Every
 *  row renders as exactly one terminal row, so the row count is the block's height. */
export const shellBlockModel = (
  shell: ShellRun,
  liveLines: readonly string[] | undefined,
  expanded: boolean,
): ShellBlockModel => {
  if (shell.running) {
    return {
      collapsible: false,
      command: shell.command,
      expanded: false,
      footer: { running: true },
      rows: (liveLines ?? [])
        .slice(-SHELL_LIVE_TAIL)
        .map((line) => ({ err: false, text: stripAnsi(line) })),
    };
  }

  const all: ShellOutputRow[] = [
    ...shellOutputLines(shell.stdout).map((text) => ({ err: false, text })),
    ...shellOutputLines(shell.stderr).map((text) => ({ err: true, text })),
  ];
  const collapsible = all.length > SHELL_COLLAPSE_OVER;
  const rows: ShellBodyRow[] =
    collapsible && !expanded
      ? [
          ...all.slice(0, SHELL_HEAD_LINES),
          { hidden: all.length - SHELL_HEAD_LINES - SHELL_TAIL_LINES },
          ...all.slice(-SHELL_TAIL_LINES),
        ]
      : all;
  const tags = [
    ...(shell.cancelled ? ["cancelled"] : []),
    ...(shell.timedOut ? ["timed out"] : []),
  ];

  return {
    collapsible,
    command: shell.command,
    expanded: collapsible && expanded,
    footer:
      shell.code === undefined && !tags.length
        ? null
        : { ...(shell.code === undefined ? {} : { code: shell.code }), tags },
    rows,
  };
};

/** Terminal rows the block takes: header, body rows, footer. */
export const shellBlockHeight = (model: ShellBlockModel) =>
  1 + model.rows.length + (model.footer ? 1 : 0);
