import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import { findSlashCommand } from "../app/slash/registry.js";
import type { SlashRunCtx } from "../app/slash/types.js";
import { Panel } from "../components/branding.js";
import { DEFAULT_THEME } from "../theme.js";
import type { PanelSection } from "../types.js";

// A live run under a pty reported that `/help` printed one line,
// "/help List available commands". These tests pin the path that produces the
// help screen: the TUI's own /help (never the gateway's) turns every category
// of `commands.catalog` into a panel section, and the panel renders every row.

const catalog = {
  canon: { "/clear": "/clear", "/goal": "/goal", "/help": "/help" },
  categories: [
    {
      name: "Session",
      pairs: [
        ["/clear", "Start fresh in this session"],
        ["/resume", "Resume a session"],
      ] as [string, string][],
    },
    {
      name: "Autonomy",
      pairs: [["/goal", "Set a goal and work until it is met"]] as [
        string,
        string,
      ][],
    },
    {
      name: "Other",
      pairs: [["/help", "List available commands"]] as [string, string][],
    },
  ],
  pairs: [] as [string, string][],
  skillCount: 3,
  sub: {},
};

const runHelp = (): PanelSection[] => {
  const panel = vi.fn();
  const help = findSlashCommand("help");

  expect(help?.name).toBe("help");
  help!.run(
    "",
    {
      local: { catalog },
      transcript: { panel },
      ui: { theme: DEFAULT_THEME },
    } as unknown as SlashRunCtx,
    "/help",
  );
  expect(panel).toHaveBeenCalledTimes(1);

  return panel.mock.calls[0]![1] as PanelSection[];
};

const renderPanel = async (
  sections: PanelSection[],
  columns = 100,
): Promise<string> => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();

  Object.assign(stdout, { columns, isTTY: false, rows: 24 });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });

  let captured = "";
  stdout.on("data", (chunk) => {
    captured += chunk.toString();
  });

  const instance = renderSync(
    React.createElement(Panel, {
      sections,
      t: DEFAULT_THEME,
      title: DEFAULT_THEME.brand.helpHeader,
    }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    },
  );

  try {
    await new Promise((resolve) => setTimeout(resolve, 20));

    // eslint-disable-next-line no-control-regex
    return captured.replace(/\u001b\[[0-9;]*m/g, "");
  } finally {
    instance.unmount();
    instance.cleanup();
  }
};

describe("/help", () => {
  it("turns every catalog category into a panel section, then the TUI and hotkey sections", () => {
    const sections = runHelp();
    const titles = sections.map((s) => s.title).filter(Boolean);

    expect(titles.slice(0, 3)).toEqual(["Session", "Autonomy", "Other"]);
    expect(titles).toContain("TUI");
    expect(titles).toContain("Hotkeys");
    expect(sections[0]!.rows).toEqual(catalog.categories[0]!.pairs);
    expect(sections.some((s) => s.text?.includes("3 skill commands"))).toBe(
      true,
    );
  });

  it("renders every command row of the help panel, not just one", async () => {
    const out = await renderPanel(runHelp());

    expect(out).toContain(DEFAULT_THEME.brand.helpHeader);

    for (const cat of catalog.categories) {
      expect(out).toContain(cat.name);

      for (const [cmd, help] of cat.pairs) {
        expect(out).toContain(cmd);
        expect(out).toContain(help);
      }
    }

    expect(out).toContain("Hotkeys");
  });

  it.each([120, 80])(
    "keeps a gap between every name and its description at width %i",
    async (columns) => {
      const sections = runHelp();
      const rows = sections.flatMap((s) => s.rows ?? []);
      const names = rows.map(([k]) => k);

      expect(names).toContain("/details [hidden|collapsed|expanded|cycle]");
      expect(names).toContain("Shift+Enter / Alt+Enter");

      const lines = (await renderPanel(sections, columns)).split("\n");

      for (const [name, desc] of rows) {
        const line = lines.find((l) => l.includes(name));

        expect(line, name).toBeDefined();

        const rest = line!.slice(line!.indexOf(name) + name.length);

        // The description may be cut off at narrow widths, but the two
        // spaces after the name must survive.
        expect(rest.startsWith("  "), `${name} | ${desc}`).toBe(true);
      }
    },
  );
});
