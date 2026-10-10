import React from "react";
import { beforeEach, describe, expect, it } from "vitest";

import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import { ctrlCOverlayTarget } from "../app/useInputHandlers.js";
import { StatusRule } from "../components/appChrome.js";
import {
  highlightsStable,
  splitComposerHighlights,
} from "../domain/composerHighlights.js";
import { DEFAULT_THEME } from "../theme.js";

type Node = React.ReactNode;

const textContent = (node: Node): string => {
  if (node === null || node === undefined || typeof node === "boolean") {
    return "";
  }

  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join("");
  }

  if (React.isValidElement<{ children?: Node }>(node)) {
    return textContent(node.props.children);
  }

  return "";
};

// The innermost element whose text holds the needle: the leaf <Text> that carries the colour.
const leafWith = (
  node: Node,
  needle: string,
): null | React.ReactElement<{ color?: string; children?: Node }> => {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = leafWith(child, needle);

      if (found) {
        return found;
      }
    }

    return null;
  }

  if (!React.isValidElement<{ color?: string; children?: Node }>(node)) {
    return null;
  }

  return (
    leafWith(node.props.children, needle) ??
    (textContent(node).includes(needle) ? node : null)
  );
};

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: "~/repo",
  liveSessionCount: 0,
  model: "opus-4.8",
  sessionStartedAt: null,
  status: "ready",
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: {
    context_max: 200_000,
    context_percent: 25,
    context_used: 50_000,
    total: 50_000,
  },
};

describe("StatusRule ultracode chip", () => {
  it.each([60, 80, 100, 140])(
    "shows an `ultracode` chip beside the model at %i columns when the mode is on",
    (cols) => {
      const text = textContent(
        StatusRule({ ...baseProps, cols, ultraMode: "ultracode" }),
      );

      expect(text).toContain("opus 4.8");
      expect(text).toContain("ultracode");
      // pinned with the model, not in the droppable tail
      expect(text.indexOf("ultracode")).toBeGreaterThan(text.indexOf("opus"));
    },
  );

  it.each([undefined, "off", "", "ultraplan"])(
    "shows no chip when ultraMode is %j",
    (ultraMode) => {
      expect(
        textContent(StatusRule({ ...baseProps, ultraMode })),
      ).not.toContain("ultracode");
    },
  );

  it("wears the accent colour, like the title it sits beside", () => {
    const chip = leafWith(
      StatusRule({ ...baseProps, ultraMode: "ultracode" }),
      "ultracode",
    );

    expect(chip?.props.color).toBe(DEFAULT_THEME.color.accent);
  });

  it("does not push the context label out of a narrow bar", () => {
    const text = textContent(
      StatusRule({ ...baseProps, cols: 60, ultraMode: "ultracode" }),
    );

    expect(text).toContain("ready");
    expect(text).toContain("50k tok");
  });
});

describe("composer wake-word highlight", () => {
  const painted = (text: string) =>
    splitComposerHighlights(text)
      .filter((segment) => segment.ref)
      .map((segment) => segment.text);

  it("paints an unambiguous wake word, wherever it stands", () => {
    expect(painted("ultracode fix the failing tests")).toEqual(["ultracode"]);
    expect(painted("fix the failing tests, ultraplan")).toEqual(["ultraplan"]);
    expect(painted("please ULTRAResearch the options")).toEqual([
      "ULTRAResearch",
    ]);
  });

  it("paints nothing the gateway would skip", () => {
    for (const text of [
      'the word "ultracode" is a mode name',
      "the word `ultracode` is a mode name",
      "run ```\nultracode\n``` as a block",
      "look in src/ultracode.py",
      "pass --ultracode to it",
      "ultracodes are fun",
      "ultracode then ultraplan the rollout",
    ]) {
      expect(painted(text), text).toEqual([]);
    }
  });

  it("a paste label is a paste: its preview never counts as a wake word", () => {
    // The gateway is told to skip the paste, so its preview must not make the
    // typed word look like a second mode, or be painted itself.
    expect(painted("ultracode [[ ultraplan the rollout [2 lines] ]]")).toEqual([
      "ultracode",
      "[[ ultraplan the rollout [2 lines] ]]",
    ]);
    expect(painted("see [[ ultracode crashed [9 lines] ]]")).toEqual([
      "[[ ultracode crashed [9 lines] ]]",
    ]);
  });

  it("a slash command is a command, not a wake word", () => {
    expect(painted("/ultracode fix the bug")).toEqual(["/ultracode"]);
  });

  it("keeps the other reference kinds and never rewrites the text", () => {
    const text = "ultracode review @file:src/ultracode.py with /work";
    const parts = splitComposerHighlights(text);

    expect(painted(text)).toEqual([
      "ultracode",
      "@file:src/ultracode.py",
      "/work",
    ]);
    expect(parts.map((p) => p.text).join("")).toBe(text);
  });

  it("flags the keystroke that completes (or breaks) the word as a repaint, not a fast echo", () => {
    expect(highlightsStable("ultracod", "ultracode")).toBe(false);
    expect(highlightsStable("ultracode", "ultracodes")).toBe(false);
    expect(highlightsStable("ultracode fix", "ultracode fix x")).toBe(true);
    expect(highlightsStable("fix it", "fix it now")).toBe(true);
  });
});

describe("Ctrl+C and the tune popup", () => {
  beforeEach(resetOverlayState);

  it("dismisses the popup", () => {
    patchOverlayState({ tunePicker: true });
    expect(ctrlCOverlayTarget(getOverlayState())).toBe("tunePicker");
  });

  it("answers a prompt first, never the popup behind it", () => {
    patchOverlayState({
      approval: { command: "ls", description: "", requestId: "a" },
      tunePicker: true,
    });
    expect(ctrlCOverlayTarget(getOverlayState())).toBe("approval");
  });
});
