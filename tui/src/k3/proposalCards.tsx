import { Box, Text } from "@k3code/ink";
import { useStore } from "@nanostores/react";

import { $uiState } from "../app/uiStore.js";
import { compactPreview } from "../lib/text.js";

import { $proposals, glyphFor } from "./proposalsStore.js";

/** Small card list above the agent strip: the gateway's `proposal.show` suggestions. */
export function ProposalCards({ cols }: { cols: number }) {
  const items = useStore($proposals);
  const t = useStore($uiState).theme;

  if (!items.length) {
    return null;
  }

  return (
    <Box flexDirection="column">
      <Text color={t.color.muted}>
        proposals — <Text color={t.color.accent}>alt+y</Text> accept ·{" "}
        <Text color={t.color.accent}>alt+n</Text> dismiss
      </Text>
      {items.map((p, i) => (
        <Text
          color={i === 0 ? t.color.accent : t.color.muted}
          key={p.id}
          wrap="truncate-end"
        >
          {glyphFor(p.kind)} {compactPreview(p.text, Math.max(10, cols - 4))}
        </Text>
      ))}
    </Box>
  );
}
