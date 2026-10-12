import { describe, expect, it } from "vitest";

import {
  buildSettingsItems,
  initSettingsState,
  settingsKey,
  type SettingsSnapshot,
  visibleItems,
} from "../domain/settings.js";

const snap: SettingsSnapshot = {
  busy: "queue",
  compact: false,
  details: "collapsed",
  effort: "default",
  indicator: "kaomoji",
  indicators: ["kaomoji", "emoji"],
  model: "a",
  modelKeys: ["a", "b"],
  mouse: true,
  outputStyle: "default",
  outputStyles: ["default", "concise"],
  permissionMode: "default",
  permissionModes: ["default", "plan"],
  showReasoning: false,
  statusBar: "top",
  statusLines: ["x", "y"],
  theme: "default",
};

describe("settings panel state", () => {
  it("moves with arrows and wraps", () => {
    const up = settingsKey(initSettingsState(snap), "", { upArrow: true });

    expect(up.state.cursor).toBe(buildSettingsItems(snap).length - 1);
    expect(settingsKey(up.state, "", { downArrow: true }).state.cursor).toBe(0);
  });

  it("cycles an enum and emits the slash command", () => {
    const r = settingsKey(initSettingsState(snap), "", { rightArrow: true });

    expect(r.action).toEqual({
      command: "/model b",
      id: "model",
      type: "apply",
      value: "b",
    });
  });

  it("filters by search and toggles a bool with Enter", () => {
    let s = initSettingsState(snap);

    for (const ch of ["/", "m", "o", "u", "s"]) {
      s = settingsKey(s, ch, {}).state;
    }

    s = settingsKey(s, "", { return: true }).state;

    expect(visibleItems(s).map((i) => i.id)).toEqual(["mouse"]);
    expect(settingsKey(s, "", { return: true }).action).toMatchObject({
      command: "/mouse off",
      type: "apply",
    });
  });

  it("Esc closes, Tab switches tab, locked ignores keys", () => {
    const s = initSettingsState(snap);

    expect(settingsKey(s, "", { escape: true }).action.type).toBe("close");
    expect(settingsKey(s, "", { tab: true }).state.tab).toBe("status");
    expect(settingsKey(s, "q", {}, { locked: true }).action.type).toBe("none");
  });

  // Ink sets key.meta on every Esc (a bare ESC byte parses as meta+escape), so
  // the "ignore Alt-chords" guard used to swallow the key the footer advertises.
  it("Esc closes although Ink flags it as meta", () => {
    const s = initSettingsState(snap);

    expect(settingsKey(s, "", { escape: true, meta: true }).action.type).toBe(
      "close",
    );
  });

  it("Esc with meta clears the search before it closes", () => {
    let s = initSettingsState(snap);

    for (const ch of ["/", "m", "o"]) {
      s = settingsKey(s, ch, {}).state;
    }

    s = settingsKey(s, "", { escape: true, meta: true }).state;
    expect(s.searching).toBe(false);
    expect(s.query).toBe("");
    expect(settingsKey(s, "", { escape: true, meta: true }).action.type).toBe(
      "close",
    );
  });

  it("an open approval still blocks Esc, and Alt-chords stay ignored", () => {
    const s = initSettingsState(snap);

    expect(
      settingsKey(s, "", { escape: true, meta: true }, { locked: true }).action
        .type,
    ).toBe("none");
    expect(settingsKey(s, "q", { meta: true }).action.type).toBe("none");
    expect(settingsKey(s, "", { ctrl: true, escape: true }).action.type).toBe(
      "none",
    );
  });
});
