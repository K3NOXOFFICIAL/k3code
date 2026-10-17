import { Box, Text, useInput, useStdout } from "@k3code/ink";
import { useStore } from "@nanostores/react";
import { useEffect, useMemo, useState } from "react";
import { $overlayState, tuneLocked } from "../app/overlayStore.js";
import {
  initialPermissionsIdx,
  permissionRows,
  permissionsCommandForKey,
  type PermissionsSnapshot,
} from "../domain/permissions.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";
import type { Theme } from "../theme.js";

import { OverlayHint, windowItems } from "./overlayControls.js";
import { chipRowProps, clampOverlayWidth } from "./overlayPrimitives.js";

const VISIBLE = 12;
const MIN_WIDTH = 40;
const MAX_WIDTH = 90;

/**
 * /permissions popup: mode row plus effective rules from `permissions.get`.
 * Enter/m on the mode row cycles the mode; a/s/d/r on a rule row dispatches
 * the matching `/permissions ...` slash string via onApply. Esc/q cancels.
 */
export function PermissionsPicker({
  gw,
  maxWidth,
  onApply,
  onCancel,
  sessionId,
  t,
}: PermissionsPickerProps) {
  const [snap, setSnap] = useState<null | PermissionsSnapshot>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [idx, setIdx] = useState(0);
  const overlay = useStore($overlayState);
  const locked = tuneLocked(overlay);
  const { stdout } = useStdout();

  const width = clampOverlayWidth(
    Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6)),
    maxWidth,
  );

  const fetchPermissions = () => {
    setLoading(true);
    gw.request<PermissionsSnapshot>("permissions.get", {
      ...(sessionId ? { session_id: sessionId } : {}),
    })
      .then((raw) => {
        const r = asRpcResult<PermissionsSnapshot>(raw);

        if (!r || typeof r.mode !== "string" || !Array.isArray(r.rules)) {
          setErr("invalid response: permissions.get");
          setLoading(false);

          return;
        }

        const next: PermissionsSnapshot = {
          mode: r.mode,
          modes: Array.isArray(r.modes) ? r.modes : [r.mode],
          rules: r.rules,
        };
        setSnap(next);
        setIdx(initialPermissionsIdx(next));
        setErr("");
        setLoading(false);
      })
      .catch((e: unknown) => {
        setErr(rpcErrorMessage(e));
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchPermissions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gw, sessionId]);

  const rows = useMemo(
    () => (snap ? permissionRows(snap) : []),
    [snap],
  );

  useInput((ch, key) => {
    if (locked || !snap) {
      return;
    }

    if (key.escape) {
      onCancel();

      return;
    }

    if (ch === "q" && !key.ctrl && !key.meta) {
      onCancel();

      return;
    }

    if (key.upArrow && idx > 0) {
      setIdx((v) => v - 1);

      return;
    }

    if (key.downArrow && idx < rows.length - 1) {
      setIdx((v) => v + 1);

      return;
    }

    const row = rows[idx];

    if (key.return && row) {
      const command = permissionsCommandForKey(
        snap,
        row,
        row.rule ? "" : "enter",
      );

      if (command) {
        onApply(command);
      }

      return;
    }

    if (ch && !key.ctrl && !key.meta && row) {
      const command = permissionsCommandForKey(snap, row, ch);

      if (command) {
        onApply(command);
      }
    }
  });

  if (loading) {
    return <Text color={t.color.muted}>loading permissions…</Text>;
  }

  if (err) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.label}>error: {err}</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  if (!rows.length) {
    return (
      <Box flexDirection="column">
        <Text color={t.color.muted}>no permission rules</Text>
        <OverlayHint t={t}>Esc/q cancel</OverlayHint>
      </Box>
    );
  }

  const { items, offset } = windowItems(
    rows.map((r) => r.label),
    idx,
    VISIBLE,
  );

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent} wrap="truncate-end">
        Permissions
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        Mode and effective rules for this session
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {offset > 0 ? ` ↑ ${offset} more` : " "}
      </Text>

      {Array.from({ length: Math.min(VISIBLE, rows.length) }, (_, i) => {
        const label = items[i];
        const rowIdx = offset + i;

        return label ? (
          <Text
            color={t.color.muted}
            {...chipRowProps(t, rowIdx === idx)}
            key={`${rows[rowIdx]?.key ?? rowIdx}`}
            wrap="truncate-end"
          >
            {rowIdx === idx ? "▸ " : "  "}
            {rowIdx + 1}. {label}
          </Text>
        ) : null;
      })}

      <Text color={t.color.muted} wrap="truncate-end">
        {offset + VISIBLE < rows.length
          ? ` ↓ ${rows.length - offset - VISIBLE} more`
          : " "}
      </Text>

      <OverlayHint t={t}>
        ↑/↓ select · Enter/m cycle mode · a allow · s ask · d deny · r rm · Esc/q close
      </OverlayHint>
    </Box>
  );
}

interface PermissionsPickerProps {
  gw: GatewayClient;
  maxWidth?: number;
  /** Slash string to dispatch. */
  onApply: (command: string) => void;
  onCancel: () => void;
  sessionId: string | null;
  t: Theme;
}
