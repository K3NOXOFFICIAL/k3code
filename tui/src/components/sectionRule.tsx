import { Box, stringWidth, Text } from "@k3code/ink";

import type { Theme } from "../theme.js";

/** The rule text for `cols` cells: `── label ───…` or a plain `───…`, never wider than `cols`. */
export function sectionRuleText(cols: number, label = ""): string {
  const w = Math.max(1, Math.floor(cols));

  if (!label) {
    return "─".repeat(w);
  }

  const head = `── ${label} `;

  if (stringWidth(head) >= w) {
    return "─".repeat(w);
  }

  return head + "─".repeat(w - stringWidth(head));
}

/**
 * A full-width divider between session sections (turns, the agent list, the composer). Sized exactly to `cols`
 * so it never wraps into a second line; `dim` for the quieter in-transcript turn rules.
 */
export function SectionRule({
  cols,
  dim = false,
  label = "",
  t,
}: {
  cols: number;
  dim?: boolean;
  label?: string;
  t: Theme;
}) {
  const text = sectionRuleText(cols, label);

  if (!label || !text.startsWith("── ")) {
    return (
      <Box flexShrink={0} height={1}>
        <Text color={t.color.border} dimColor={dim}>
          {text}
        </Text>
      </Box>
    );
  }

  const head = `── ${label} `;

  return (
    <Box flexShrink={0} height={1}>
      <Text color={t.color.border} dimColor={dim}>
        {"── "}
        <Text color={t.color.accent}>{label}</Text>
        {" " + text.slice(head.length)}
      </Text>
    </Box>
  );
}
