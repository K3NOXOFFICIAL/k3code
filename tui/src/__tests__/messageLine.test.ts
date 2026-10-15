import { describe, expect, it } from "vitest";

import {
  roleBlockHeader,
  shouldShowThinkingTrail,
} from "../components/messageLine.js";
import { userPromptSource } from "../domain/roles.js";

describe("roleBlockHeader", () => {
  it("gives an answer with body text the role header, a blank one none", () => {
    expect(roleBlockHeader({ role: "assistant", text: "final" }, null)).toBe(
      "role",
    );
    expect(roleBlockHeader({ role: "assistant", text: "   " }, null)).toBe(
      null,
    );
  });

  it("gives a typed prompt the role header and a harness prompt a label", () => {
    expect(roleBlockHeader({ role: "user", text: "prompt" }, "user")).toBe(
      "role",
    );
    expect(
      roleBlockHeader({ role: "user", text: "[Standing goal]" }, "goal"),
    ).toBe("label");
  });

  it("leaves slash echoes, diffs and system notes without a header", () => {
    expect(
      roleBlockHeader({ kind: "slash", role: "user", text: "/help" }, "user"),
    ).toBe(null);
    expect(
      roleBlockHeader({ kind: "diff", role: "assistant", text: "x" }, null),
    ).toBe(null);
    expect(roleBlockHeader({ role: "system", text: "note" }, null)).toBe(null);
  });
});

describe("userPromptSource", () => {
  it("tells goal kicks and continuations from the person's own words", () => {
    expect(userPromptSource("[Standing goal]\nGoal: x")).toBe("goal");
    expect(
      userPromptSource("[Continuing toward your standing goal — 2/5]\nGoal: x"),
    ).toBe("goal");
    expect(
      userPromptSource(
        "Your previous answer was cut off at the output token limit. Continue.",
      ),
    ).toBe("system");
    expect(userPromptSource("fix the transcript")).toBe("user");
    expect(userPromptSource("[note] not a goal")).toBe("user");
  });
});

describe("shouldShowThinkingTrail", () => {
  it("hides an ordinary reasoning trail when every section is hidden", () => {
    const msg = { role: "system", text: "", thinking: "plan" } as const;
    expect(shouldShowThinkingTrail(msg, "hidden", "hidden", "hidden")).toBe(
      false,
    );
  });

  it("shows an ordinary reasoning trail when any section is visible", () => {
    const msg = { role: "system", text: "", thinking: "plan" } as const;
    expect(shouldShowThinkingTrail(msg, "collapsed", "hidden", "hidden")).toBe(
      true,
    );
    expect(shouldShowThinkingTrail(msg, "hidden", "collapsed", "hidden")).toBe(
      true,
    );
    expect(shouldShowThinkingTrail(msg, "hidden", "hidden", "expanded")).toBe(
      true,
    );
  });

  it("keeps a MoA reference block visible even when every section is hidden (#64657)", () => {
    const msg = {
      role: "system",
      text: "",
      thinking: "◇ Reference 1/2 — model-a\nadvice-a",
      isMoaReference: true,
    } as const;

    expect(shouldShowThinkingTrail(msg, "hidden", "hidden", "hidden")).toBe(
      true,
    );
  });
});
