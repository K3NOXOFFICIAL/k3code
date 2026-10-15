import { beforeEach, describe, expect, it } from "vitest";

import { turnController } from "../app/turnController.js";
import { getTurnState, resetTurnState } from "../app/turnStore.js";

// The tool trail is built from tool.start / tool.complete pairs. A gateway
// may replay either event (reconnect, a provider reporting the same result
// twice); the trail must list each call once, phrased from its arguments.
describe("turnController tool trail", () => {
  beforeEach(() => {
    resetTurnState();
    turnController.fullReset();
    turnController.startMessage();
  });

  const trailLines = () =>
    [
      ...getTurnState().streamSegments.flatMap((m) => m.tools ?? []),
      ...getTurnState().streamPendingTools,
    ].filter((line) => line.endsWith(" ✓") || line.endsWith(" ✗"));

  it("phrases a running call from its structured arguments", () => {
    turnController.recordToolStart("c1", "glob", "", undefined, undefined, {
      path: "src/k3code",
      pattern: "**/k3code*",
    });

    const [tool] = getTurnState().tools;

    expect(tool?.args).toEqual({ path: "src/k3code", pattern: "**/k3code*" });
  });

  it("lists a call once when tool.start is replayed", () => {
    const args = { pattern: "[!.]*/k3code*", path: "." };

    turnController.recordToolStart(
      "c1",
      "glob",
      "",
      undefined,
      undefined,
      args,
    );
    turnController.recordToolStart(
      "c1",
      "glob",
      "",
      undefined,
      undefined,
      args,
    );

    expect(getTurnState().tools).toHaveLength(1);
  });

  it("lists a finished call once when tool.complete is replayed", () => {
    const args = { pattern: "[!.]*/k3code*", path: "." };

    turnController.recordToolStart(
      "c1",
      "glob",
      "",
      undefined,
      undefined,
      args,
    );

    for (let i = 0; i < 30; i++) {
      turnController.recordToolComplete("c1", "glob", "", 0.1, undefined, "x");
    }

    const lines = trailLines();

    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/^Glob {2}\[!\.\]\*\/k3code\* \(0\.1s\)/);
    expect(lines[0]).not.toContain("{'");
    expect(getTurnState().tools).toHaveLength(0);
  });

  it("still lists two different calls of the same tool", () => {
    turnController.recordToolStart("c1", "glob", "", undefined, undefined, {
      pattern: "a",
    });
    turnController.recordToolComplete("c1", "glob", "", 0.1);
    turnController.recordToolStart("c2", "glob", "", undefined, undefined, {
      pattern: "b",
    });
    turnController.recordToolComplete("c2", "glob", "", 0.1);

    const lines = trailLines();

    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain("Glob  a");
    expect(lines[1]).toContain("Glob  b");
  });

  it("forgets completed ids at the next turn so an id can be reused", () => {
    turnController.recordToolStart("c1", "read", "", undefined, undefined, {
      path: "a.py",
    });
    turnController.recordToolComplete("c1", "read", "", 0.1);
    turnController.startMessage();
    turnController.recordToolStart("c1", "read", "", undefined, undefined, {
      path: "b.py",
    });
    turnController.recordToolComplete("c1", "read", "", 0.1);

    expect(trailLines().some((line) => line.includes("Read  b.py"))).toBe(true);
  });
});
