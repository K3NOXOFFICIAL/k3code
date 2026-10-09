import { stringWidth } from "@k3code/ink";
import { describe, expect, it } from "vitest";

import { sectionRuleText } from "../components/sectionRule.js";

describe("sectionRuleText", () => {
  it("fills exactly the given width so a rule never wraps", () => {
    for (const cols of [1, 12, 60, 80, 160]) {
      expect(stringWidth(sectionRuleText(cols))).toBe(cols);
      expect(
        stringWidth(sectionRuleText(cols, "agents (2) · ← agent view")),
      ).toBe(cols);
    }
  });

  it("puts the label after a short lead and drops it when it cannot fit", () => {
    expect(sectionRuleText(20, "agents")).toBe("── agents ──────────");
    expect(sectionRuleText(6, "a long label")).toBe("──────");
  });
});
