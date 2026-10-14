import { Box, Text } from "@k3code/ink";
import { useStore } from "@nanostores/react";

import {
  $shellExpanded,
  $shellRuns,
  toggleShellExpanded,
} from "../app/shellRunStore.js";
import { type ShellBlockModel, shellBlockModel } from "../lib/shellBlock.js";
import type { Theme } from "../theme.js";
import type { ShellRun } from "../types.js";

/** A `!cmd` run in the transcript: the live tail while it runs, then its output (long output collapsed to head and
 *  tail, click the header to expand), the exit code and its tags. */
export function ShellBlock({ shell, t }: { shell: ShellRun; t: Theme }) {
  const live = useStore($shellRuns)[shell.execId];
  const expanded = Boolean(useStore($shellExpanded)[shell.execId]);

  return (
    <ShellBlockView
      model={shellBlockModel(shell, live?.lines, expanded)}
      onToggle={() => toggleShellExpanded(shell.execId)}
      t={t}
    />
  );
}

/** One terminal row per model row (every row truncates), so `shellBlockHeight` is the rendered height. */
export function ShellBlockView({
  model,
  onToggle,
  t,
}: {
  model: ShellBlockModel;
  onToggle?: () => void;
  t: Theme;
}) {
  const { footer } = model;

  return (
    <Box flexDirection="column">
      <Box onClick={model.collapsible ? onToggle : undefined}>
        <Text wrap="truncate-end">
          {model.collapsible ? (
            <Text color={t.color.accent}>{model.expanded ? "▾ " : "▸ "}</Text>
          ) : null}
          <Text color={t.color.shellDollar}>$ </Text>
          <Text color={t.color.text}>{model.command}</Text>
        </Text>
      </Box>

      {model.rows.map((row, i) =>
        "hidden" in row ? (
          <Box key={i} onClick={onToggle}>
            <Text color={t.color.muted} dimColor wrap="truncate-end">
              {"  "}… {row.hidden} lines hidden
            </Text>
          </Box>
        ) : (
          <Text
            color={row.err ? t.color.error : t.color.muted}
            key={i}
            wrap="truncate-end"
          >
            {"  "}
            {row.text || " "}
          </Text>
        ),
      )}

      {footer && "running" in footer ? (
        <Text color={t.color.muted} dimColor wrap="truncate-end">
          {"  "}running…
        </Text>
      ) : footer ? (
        <Text wrap="truncate-end">
          {"  "}
          {footer.code === undefined ? null : (
            <Text
              color={footer.code === 0 ? t.color.muted : t.color.error}
              dimColor={footer.code === 0}
            >
              exit {footer.code}
            </Text>
          )}
          {footer.tags.map((tag, i) => (
            <Text color={t.color.warn} key={tag}>
              {i || footer.code !== undefined ? " · " : ""}
              {tag}
            </Text>
          ))}
        </Text>
      ) : null}
    </Box>
  );
}
