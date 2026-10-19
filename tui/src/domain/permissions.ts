/** Pure row/key helpers for the /permissions popup (mirrors domain/providers.ts). */

export interface PermissionRule {
  action: string;
  id: string;
  pattern: string;
  source: string;
  tool: string;
}

export interface PermissionsSnapshot {
  mode: string;
  modes: string[];
  rules: PermissionRule[];
}

export interface PermissionRow {
  key: string;
  label: string;
  rule?: PermissionRule;
}

/** Mode row first, then one row per effective rule. */
export function permissionRows(snap: PermissionsSnapshot): PermissionRow[] {
  return [
    { key: `mode:${snap.mode}`, label: `Mode: ${snap.mode} (Enter cycles)` },
    ...snap.rules.map((rule) => ({
      key: `rule:${rule.id}`,
      label: `${rule.id} ${rule.action} ${rule.tool} ${rule.pattern} [${rule.source}]`,
      rule,
    })),
  ];
}

/** Cursor start: the mode row. */
export function initialPermissionsIdx(_snap: PermissionsSnapshot): number {
  return 0;
}

/** The slash command the picker emits for the highlighted row + key. */
export function permissionsCommandForKey(
  snap: PermissionsSnapshot,
  row: PermissionRow,
  ch: string,
): null | string {
  const k = ch.toLowerCase();
  if (!row.rule) {
    // Mode row: Enter/m cycles the mode through the advertised list.
    if (k === "m" || k === "enter") {
      const modes = snap.modes.length ? snap.modes : [snap.mode];
      const next =
        modes[(modes.indexOf(snap.mode) + 1) % modes.length] ?? snap.mode;
      return `/permissions mode ${next}`;
    }

    return null;
  }

  const { id, tool, pattern } = row.rule;
  if (k === "a") {
    return `/permissions allow ${tool} ${pattern}`;
  }

  if (k === "s") {
    return `/permissions ask ${tool} ${pattern}`;
  }

  if (k === "d") {
    return `/permissions deny ${tool} ${pattern}`;
  }

  if (k === "r") {
    return `/permissions rm ${id}`;
  }

  return null;
}
