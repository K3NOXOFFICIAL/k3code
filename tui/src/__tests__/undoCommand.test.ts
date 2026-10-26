import { describe, expect, it, vi } from "vitest";

import { findSlashCommand } from "../app/slash/registry.js";

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r);
    }
  };

const runUndo = async (name: string, undoResult: unknown) => {
  const sys = vi.fn();
  const rpc = vi.fn(() => Promise.resolve(undoResult));

  const ctx = {
    gateway: { rpc },
    guarded,
    sid: "sid-1",
    transcript: { setHistoryItems: vi.fn(), sys, trimLastExchange: vi.fn() },
  };

  findSlashCommand(name)!.run("", ctx as never, `/${name}`);
  await rpc.mock.results[0]?.value;
  await Promise.resolve();

  return { printed: sys.mock.calls.map((c) => String(c[0])).join("\n"), rpc };
};

describe("/undo", () => {
  it("is also /rewind", () => {
    expect(findSlashCommand("rewind")?.name).toBe("undo");
  });

  it("shows the files the gateway rewound", async () => {
    const { printed, rpc } = await runUndo("rewind", {
      note: "restored 1 file(s):\n  /p/a.txt",
      removed: 2,
    });

    expect(rpc).toHaveBeenCalledWith("session.undo", { session_id: "sid-1" });
    expect(printed).toContain("undid 2 messages");
    expect(printed).toContain("restored 1 file(s):\n  /p/a.txt");
  });

  it("says only what it undid when no file was rewound", async () => {
    const { printed } = await runUndo("undo", { removed: 2 });

    expect(printed).toBe("undid 2 messages");
  });
});
