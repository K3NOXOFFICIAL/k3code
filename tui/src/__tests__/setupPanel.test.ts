import { beforeEach, describe, expect, it, vi } from "vitest";

import { setupCommands } from "../app/slash/commands/setup.js";
import {
  buildSetupRequiredSections,
  SETUP_REQUIRED_TITLE,
} from "../content/setup.js";

const runExternalSetup = vi.hoisted(() => vi.fn());

vi.mock("../app/setupHandoff.js", () => ({ runExternalSetup }));

const panelText = () =>
  buildSetupRequiredSections()
    .flatMap((s) => [s.text ?? "", ...(s.rows ?? []).flat()])
    .join("\n");

describe("Setup Required panel", () => {
  it("names `k3code onboard` as the command to run outside the TUI", () => {
    expect(SETUP_REQUIRED_TITLE).toBe("Setup Required");
    expect(panelText()).toContain("`k3code onboard`");
    expect(panelText()).not.toContain("`k3code setup`");
  });

  it("names the in-TUI route first: /provider and its + Add new provider... row", () => {
    const [first] = buildSetupRequiredSections()[1]!.rows!;

    expect(first![0]).toBe("/provider");
    expect(first![1]).toContain("+ Add new provider...");
    // the second column fits the 80-column panel without an ellipsis
    expect(first![1]!.length).toBeLessThanOrEqual(50);
  });

  it("does not point at a dashboard or Desktop settings page k3code does not have", () => {
    expect(panelText()).not.toMatch(/dashboard|desktop/i);
  });
});

describe("/setup", () => {
  const setup = setupCommands.find((c) => c.name === "setup")!;
  const ctx = {} as never;

  beforeEach(() => runExternalSetup.mockClear());

  it("starts the fast-or-full onboarding flow, not the 13-step wizard", () => {
    setup.run("", ctx);

    expect(runExternalSetup).toHaveBeenCalledOnce();
    expect(runExternalSetup.mock.calls[0]![0].args).toEqual(["onboard"]);
  });

  it("still passes arguments to the full wizard so one step can be re-run", () => {
    setup.run("--step providers", ctx);

    expect(runExternalSetup.mock.calls[0]![0].args).toEqual([
      "setup",
      "--step",
      "providers",
    ]);
  });
});
