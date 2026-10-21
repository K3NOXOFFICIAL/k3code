import { describe, expect, it } from "vitest";

import {
  buildSettingsItems,
  initSettingsState,
  settingsKey,
  settingsSnapshot,
  type SettingsSnapshot,
  visibleItems,
} from "../domain/settings.js";

const snap: SettingsSnapshot = {
  agentsView: false,
  archiveDays: 14,
  busy: "queue",
  compact: false,
  details: "collapsed",
  effort: "default",
  indicator: "kaomoji",
  indicators: ["kaomoji", "emoji"],
  model: "a",
  modelKeys: ["a", "b"],
  mouse: true,
  retryForever: false,
  outputStyle: "default",
  outputStyles: ["default", "concise"],
  permissionMode: "default",
  permissionModes: ["default", "plan"],
  showReasoning: false,
  statusBar: "top",
  statusLines: ["x", "y"],
  theme: "auto",
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

describe("settings panel theme row", () => {
  const local = {
    agentsView: false,
    busy: "queue",
    compact: false,
    details: "expanded",
    indicator: "kaomoji",
    indicators: ["kaomoji"],
    mouse: true,
    retryForever: false,
    showReasoning: false,
    statusBar: "top",
  };

  it("shows the saved display.tui_theme pin, not the gateway skin name", () => {
    const raw = { settings: { theme: "default" } };

    expect(settingsSnapshot(raw, { ...local, theme: "light" }).theme).toBe(
      "light",
    );
    expect(settingsSnapshot(raw, { ...local, theme: "dark" }).theme).toBe(
      "dark",
    );
  });

  it("falls back to auto when no pin is saved", () => {
    const raw = { settings: { theme: "default" } };

    expect(settingsSnapshot(raw, local).theme).toBe("auto");
    expect(settingsSnapshot(raw, { ...local, theme: "" }).theme).toBe("auto");
  });

  it("cycles only values /theme accepts", () => {
    const theme = buildSettingsItems({ ...snap, theme: "auto" }).find(
      (i) => i.id === "theme",
    );

    expect(theme?.options).toEqual(["auto", "light", "dark"]);
  });
});

describe("settings panel archive row", () => {
  const local = {
    agentsView: false,
    busy: "queue",
    compact: false,
    details: "expanded",
    indicator: "kaomoji",
    indicators: ["kaomoji"],
    mouse: true,
    retryForever: false,
    showReasoning: false,
    statusBar: "top",
  };
  const row = (s: SettingsSnapshot) =>
    buildSettingsItems(s).find((i) => i.id === "archive_days")!;

  it("shows core's 14 days until a threshold is saved", () => {
    expect(row(snap).value).toBe("14 days");
    expect(settingsSnapshot({}, local).archiveDays).toBe(14);
    expect(
      settingsSnapshot({}, { ...local, archiveDays: 30 }).archiveDays,
    ).toBe(30);
    expect(
      settingsSnapshot({}, { ...local, archiveDays: -1 }).archiveDays,
    ).toBe(14);
    expect(
      settingsSnapshot({}, { ...local, archiveDays: Number.NaN }).archiveDays,
    ).toBe(14);
  });

  it("reads 0 as off and keeps a hand-set value selectable", () => {
    expect(row({ ...snap, archiveDays: 0 }).value).toBe("off");
    expect(row({ ...snap, archiveDays: 1 }).value).toBe("1 day");

    const odd = row({ ...snap, archiveDays: 21 });

    expect(odd.value).toBe("21 days");
    expect(odd.options).toContain("21 days");
  });

  it("sets the retention key core reads, 0 for off", () => {
    const item = row(snap);

    expect(item.options).toEqual([
      "off",
      "7 days",
      "14 days",
      "30 days",
      "90 days",
    ]);
    expect(item.command("30 days")).toBe(
      "/config set retention.session_archive_days 30",
    );
    expect(item.command("off")).toBe(
      "/config set retention.session_archive_days 0",
    );
  });

  it("cycles from the panel to the next threshold", () => {
    let s = initSettingsState(snap);

    for (const ch of ["/", "a", "r", "c", "h", "i", "v", "e"]) {
      s = settingsKey(s, ch, {}).state;
    }

    s = settingsKey(s, "", { return: true }).state;

    expect(settingsKey(s, "", { rightArrow: true }).action).toEqual({
      command: "/config set retention.session_archive_days 30",
      id: "archive_days",
      type: "apply",
      value: "30 days",
    });
  });
});
