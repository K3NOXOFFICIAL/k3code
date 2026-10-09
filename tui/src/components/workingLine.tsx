import { Box, Text } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useState } from "react";

import { $escInterruptHint } from "../app/escInterruptHintStore.js";
import {
  $sessionOutputTokens,
  $streamedOutputChars,
  $turnTokenBaseline,
  liveTurnOutputTokens,
} from "../app/outputTokensStore.js";
import { isReducedMotion } from "../lib/animation.js";
import {
  advanceRotation,
  formatWorkingLine,
  startRotation,
  WORKING_TICK_MS,
  workingEffort,
  workingGlyph,
  workingMessage,
} from "../lib/workingLine.js";
import type { Theme } from "../theme.js";

interface WorkingLineProps {
  /** `/indicator ascii`: plain-ASCII glyphs and punctuation. */
  ascii?: boolean;
  busy: boolean;
  /** `session.info.reasoning_effort`. Shown only when the gateway sends a real level. */
  effort?: null | string;
  /** Turn start from the status clock; falls back to mount time. */
  startedAt?: null | number;
  t: Theme;
}

export const ESC_INTERRUPT_HINT = "Esc again to interrupt";

/** One row above the composer while a turn runs. Renders nothing when idle.
 *  Owns its own timer, so a tick re-renders this row and nothing else.
 *  After the first Esc of an interrupting pair it also shows `ESC_INTERRUPT_HINT` on the same row. */
export function WorkingLine({
  ascii,
  busy,
  effort,
  startedAt,
  t,
}: WorkingLineProps) {
  if (!busy) {
    return null;
  }

  return (
    <ActiveWorkingLine
      ascii={ascii}
      effort={effort}
      startedAt={startedAt}
      t={t}
    />
  );
}

const random = () => Math.random();

function ActiveWorkingLine({
  ascii = false,
  effort,
  startedAt,
  t,
}: Omit<WorkingLineProps, "busy">) {
  // Read once per mount: reduced motion is a launch-time choice, not a live toggle.
  const [reduced] = useState(() => isReducedMotion());
  const [origin] = useState(() => startedAt ?? Date.now());
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [rotation, setRotation] = useState(() => startRotation(random));
  const sessionTokens = useStore($sessionOutputTokens);
  // Session total when the turn started; set by the app, so an overlay that
  // unmounts this row mid-turn does not reset the count.
  const tokenBaseline = useStore($turnTokenBaseline);
  // Text streamed in the call still running: estimated until its real count lands.
  const pendingChars = useStore($streamedOutputChars);
  const escHint = useStore($escInterruptHint);

  useEffect(() => {
    // Animated: one timer drives the glyph, the clock and the message rotation.
    // Reduced motion: only the clock ticks, once a second. Glyph and message stay put.
    const id = setInterval(
      () => {
        const clock = Date.now();

        setNow(clock);

        if (!reduced) {
          setTick((n) => n + 1);
          setRotation((prev) => advanceRotation(prev, clock - origin, random));
        }
      },
      reduced ? 1000 : WORKING_TICK_MS,
    );

    return () => clearInterval(id);
  }, [reduced, origin]);

  const elapsedMs = Math.max(0, now - origin);

  const line = formatWorkingLine({
    ascii,
    effort: workingEffort(effort),
    elapsedMs,
    glyph: workingGlyph(tick, reduced, ascii),
    message: workingMessage(rotation.index),
    outputTokens: liveTurnOutputTokens(
      sessionTokens,
      tokenBaseline,
      pendingChars,
    ),
  });

  const working = (
    <Text color={t.color.accent} wrap="truncate-end">
      {line}
    </Text>
  );

  if (!escHint) {
    return working;
  }

  // The hint keeps its width and the working text truncates first, so a narrow terminal still shows the hint.
  return (
    <Box flexDirection="row">
      <Box flexShrink={1} minWidth={0}>
        {working}
      </Box>
      <Box flexShrink={0}>
        <Text color={t.color.muted} wrap="truncate-end">
          {` · ${ESC_INTERRUPT_HINT}`}
        </Text>
      </Box>
    </Box>
  );
}
