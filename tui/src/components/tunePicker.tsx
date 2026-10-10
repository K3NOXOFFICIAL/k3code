import { Box, Text, useInput, useStdout } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import {
  initTuneState,
  normalizeTuneSnapshot,
  tuneKey,
  tuneLayout,
  type TuneLayout,
  tuneScreen,
  type TuneState,
  type TuneTone,
} from "../domain/tune.js";
import type { GatewayClient } from "../gatewayClient.js";
import type { TuneGetResponse, TuneSetParams } from "../gatewayTypes.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint } from "./overlayControls.js";
import { chipRowProps } from "./overlayPrimitives.js";

const toneColor = (t: Theme, tone: TuneTone) =>
  ({
    accent: t.color.accent,
    label: t.color.label,
    muted: t.color.muted,
    ok: t.color.ok,
    plain: t.color.text,
  })[tone];

/** The popup's lines, painted. The cursor row wears the shared selection chip; everything else is tone-tagged text. */
export function TuneView({ layout, locked = false, state, t }: TuneViewProps) {
  return (
    <Box flexDirection="column" width={layout.width}>
      {tuneScreen(state, layout, { locked }).map((line, i) => (
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

/**
 * /tune: model list, effort slider and ultracode in one popup (↑/↓ model, ←/→
 * effort, Tab ultracode, Enter apply as default, s this session only, Esc
 * cancel). Reads `tune.get`; Enter / `s` hand ONE `tune.set` payload (changed
 * fields only) to `onApply`. While an approval / question / password / confirm
 * prompt is open the popup ignores every key, so the prompt owns the keyboard.
 */
export function TunePicker({
  gw,
  maxWidth,
  onApply,
  onCancel,
  sessionId,
  t,
}: TunePickerProps) {
  const [state, setState] = useState<null | TuneState>(null);
  const [err, setErr] = useState("");
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();
  // Keys can land twice before React re-renders: the handler works off this ref, not the render's `state`.
  const live = useRef<null | TuneState>(null);

  useEffect(() => {
    let current = true;

    gw.request<TuneGetResponse>(
      "tune.get",
      sessionId ? { session_id: sessionId } : {},
    )
      .then((raw) => {
        if (!current) {
          return;
        }

        const snap = normalizeTuneSnapshot(asRpcResult(raw));

        if (!snap) {
          return setErr("invalid response: tune.get");
        }

        live.current = initTuneState(snap);
        setState(live.current);
      })
      .catch((e: unknown) => current && setErr(rpcErrorMessage(e)));

    return () => {
      current = false;
    };
  }, [gw, sessionId]);

  useInput((ch, key) => {
    if (locked) {
      return;
    }

    const cur = live.current;

    if (!cur) {
      if (key.escape) {
        onCancel();
      }

      return;
    }

    const step = tuneKey(cur, ch, key, { sessionId });

    if (step.state !== cur) {
      live.current = step.state;
      setState(step.state);
    }

    if (step.action.type === "cancel") {
      onCancel();
    } else if (step.action.type === "apply") {
      onApply(step.action.params);
    }
  });

  const layout = useMemo(
    () =>
      state
        ? tuneLayout({
            cols: stdout?.columns ?? 80,
            maxWidth,
            rows: stdout?.rows ?? 24,
            stops: state.stops,
            total: state.rows.length,
            wakeWords: state.wakeWords,
          })
        : null,
    [maxWidth, state, stdout?.columns, stdout?.rows],
  );

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc cancel</OverlayHint>
      </Box>
    );
  }

  if (!state || !layout) {
    return <Text color={t.color.muted}>loading models…</Text>;
  }

  return <TuneView layout={layout} locked={locked} state={state} t={t} />;
}

interface TuneViewProps {
  layout: TuneLayout;
  locked?: boolean;
  state: TuneState;
  t: Theme;
}

interface TunePickerProps {
  gw: GatewayClient;
  maxWidth?: number;
  onApply: (params: TuneSetParams) => void;
  onCancel: () => void;
  sessionId: null | string;
  t: Theme;
}
