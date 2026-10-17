import { describe, expect, it } from "vitest";

import {
  initialPermissionsIdx,
  permissionRows,
  permissionsCommandForKey,
  type PermissionsSnapshot,
} from "../domain/permissions.js";

const SNAP: PermissionsSnapshot = {
  mode: "default",
  modes: ["default", "accept-edits", "plan", "auto"],
  rules: [
    { action: "ask", id: "u1", pattern: "rm *", source: "user", tool: "bash" },
    { action: "deny", id: "s1", pattern: "*", source: "session", tool: "edit" },
  ],
};

describe("permissionRows", () => {
  it("puts the mode row first, then one row per rule", () => {
    const rows = permissionRows(SNAP);
    expect(rows).toHaveLength(3);
    expect(rows[0]!.label).toContain("Mode: default");
    expect(rows[1]!.label).toContain("u1");
    expect(rows[2]!.label).toContain("s1");
  });

  it("starts the cursor on the mode row", () => {
    expect(initialPermissionsIdx(SNAP)).toBe(0);
  });
});

describe("permissionsCommandForKey", () => {
  it("cycles the mode from the mode row", () => {
    const rows = permissionRows(SNAP);
    expect(permissionsCommandForKey(SNAP, rows[0]!, "m")).toBe(
      "/permissions mode accept-edits",
    );
    expect(permissionsCommandForKey(SNAP, rows[0]!, "enter")).toBe(
      "/permissions mode accept-edits",
    );
  });

  it("wraps the mode cycle", () => {
    const rows = permissionRows({ ...SNAP, mode: "auto" });
    expect(permissionsCommandForKey({ ...SNAP, mode: "auto" }, rows[0]!, "m")).toBe(
      "/permissions mode default",
    );
  });

  it("maps a/s/d/r on a rule row", () => {
    const rows = permissionRows(SNAP);
    const rule = rows[1]!;
    expect(permissionsCommandForKey(SNAP, rule, "a")).toBe(
      "/permissions allow bash rm *",
    );
    expect(permissionsCommandForKey(SNAP, rule, "s")).toBe(
      "/permissions ask bash rm *",
    );
    expect(permissionsCommandForKey(SNAP, rule, "d")).toBe(
      "/permissions deny bash rm *",
    );
    expect(permissionsCommandForKey(SNAP, rule, "r")).toBe("/permissions rm u1");
  });

  it("returns null for unbound keys", () => {
    const rows = permissionRows(SNAP);
    expect(permissionsCommandForKey(SNAP, rows[1]!, "z")).toBe(null);
    expect(permissionsCommandForKey(SNAP, rows[0]!, "a")).toBe(null);
  });
});
