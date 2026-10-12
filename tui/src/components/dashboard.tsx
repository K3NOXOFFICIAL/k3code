import { Box, Text, useInput, useStdout } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import { $uiState, patchUiState } from "../app/uiStore.js";
import {
  type DashboardChange,
  dashboardClickAt,
  dashboardKey,
  dashboardLayout,
  dashboardScreen,
  type DashboardState,
  type DashboardStep,
  type DashboardTabId,
  type DashboardTone,
  type DashboardUi,
  dashboardWheel,
  initDashboardState,
} from "../domain/dashboard.js";
import type { Theme } from "../theme.js";

import { chipRowProps } from "./overlayPrimitives.js";

const toneColor = (t: Theme, tone: DashboardTone) =>
  ({
    accent: t.color.accent,
    label: t.color.label,
    muted: t.color.muted,
    ok: t.color.ok,
    plain: t.color.text,
  })[tone];

/**
 * /dashboard (and /display): Look, Details and Input settings on one tabbed
 * popup. ←/→ change a row, ↑/↓ move, Tab changes tab, Enter saves, Esc puts it
 * all back. Rows paint live as they change; closing without saving (Esc,
 * Ctrl+C, a session switch) restores the ui the popup found. A click on a tab
 * or a row only moves the cursor; a click on a chip picks that option. While a
 * question is open the popup reads no key and no click, so the prompt owns the
 * input.
 */
export function Dashboard({
  initialTab,
  maxWidth,
  onCancel,
  onSave,
  t,
}: DashboardProps) {
  const [state, setState] = useState<DashboardState>(() =>
    initDashboardState($uiState.get(), initialTab),
  );
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();
  // Keys and clicks can land twice before React re-renders: handlers work off this ref, not the render's `state`.
  const live = useRef(state);
  // Saved (or already put back): the unmount must not restore the ui under the user's saved choice.
  const settled = useRef(false);

  useEffect(
    () => () => {
      if (!settled.current) {
        patchUiState(live.current.origin);
      }
    },
    [],
  );

  const layout = useMemo(
    () =>
      dashboardLayout({
        cols: stdout?.columns ?? 80,
        maxWidth,
        rows: stdout?.rows ?? 24,
      }),
    [maxWidth, stdout?.columns, stdout?.rows],
  );

  const lines = useMemo(
    () => dashboardScreen(state, layout, { locked }),
    [layout, locked, state],
  );

  const take = (step: DashboardStep) => {
    if (step.state !== live.current) {
      live.current = step.state;
      setState(step.state);
    }

    const { action } = step;

    if (action.type === "preview") {
      patchUiState(action.patch);
    } else if (action.type === "cancel") {
      settled.current = true;
      patchUiState(action.revert);
      onCancel();
    } else if (action.type === "save") {
      settled.current = true;
      onSave(action.changes, action.patch);
    }
  };

  useInput((ch, key) => {
    if (key.wheelUp || key.wheelDown) {
      return take(
        dashboardWheel(live.current, key.wheelUp ? -1 : 1, { locked }),
      );
    }

    take(dashboardKey(live.current, ch, key, { locked }));
  });

  return (
    <Box
      flexDirection="column"
      onClick={(e: { localCol: number; localRow: number }) =>
        take(dashboardClickAt(live.current, layout, e, { locked }))
      }
      width={layout.width}
    >
      {lines.map((line, i) => (
        <Text
          key={`${line.kind}:${i}`}
          wrap="truncate-end"
          {...(line.active ? chipRowProps(t, true) : {})}
        >
          {line.segs.map((s, j) => (
            <Text
              bold={s.bold}
              color={line.active ? undefined : toneColor(t, s.tone)}
              key={j}
            >
              {s.text}
            </Text>
          ))}
        </Text>
      ))}
    </Box>
  );
}

interface DashboardProps {
  initialTab: DashboardTabId;
  maxWidth?: number;
  onCancel: () => void;
  onSave: (changes: DashboardChange[], patch: Partial<DashboardUi>) => void;
  t: Theme;
}
