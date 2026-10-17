import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import { ApprovalPrompt, approvalLabel } from "../components/prompts.js";
import { DEFAULT_THEME } from "../theme.js";
import type { ApprovalReq } from "../types.js";

// What the gateway sends for `touch /home/u/pa1.txt` (core: test_permissions_gateway asserts these are the
// rules it then stores).
const touch: ApprovalReq = {
  command: "touch /home/u/pa1.txt",
  description: "bash",
  requestId: "approval-1",
  rules: {
    always: ["allow bash touch * [project]"],
    session: ["allow bash touch * [session]"],
  },
};

const paint = (req: ApprovalReq): string => {
  const stdout = Object.assign(new PassThrough(), {
    columns: 100,
    rows: 30,
    isTTY: false,
  });
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true,
    setRawMode: () => {},
    ref: () => {},
    unref: () => {},
  });
  let output = "";
  stdout.on("data", (chunk) => {
    output += stripAnsi(chunk.toString());
  });

  const view = renderSync(
    <ApprovalPrompt
      cols={100}
      onChoice={() => {}}
      req={req}
      t={DEFAULT_THEME}
    />,
    {
      stdout: stdout as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      patchConsole: false,
    },
  );

  view.unmount();
  view.cleanup();

  return output;
};

describe("approval choices name the rule they store", () => {
  it("shows the exact rule Always allow and Allow this session add", () => {
    expect(approvalLabel(touch, "always")).toBe(
      "Always allow · allow bash touch * [project]",
    );
    expect(approvalLabel(touch, "session")).toBe(
      "Allow this session · allow bash touch * [session]",
    );
    expect(approvalLabel(touch, "once")).toBe("Allow once");
    expect(approvalLabel(touch, "deny")).toBe("Deny");
  });

  it("lists every rule a compound command stores", () => {
    const req: ApprovalReq = {
      ...touch,
      command: "mkdir -p sub && touch sub/x.txt",
      rules: {
        always: [
          "allow bash mkdir * [project]",
          "allow bash touch * [project]",
        ],
      },
    };

    expect(approvalLabel(req, "always")).toBe(
      "Always allow · allow bash mkdir * [project], allow bash touch * [project]",
    );
  });

  it("says when a choice stores no rule (a launcher such as sh -c)", () => {
    const req: ApprovalReq = {
      ...touch,
      command: "sh -c 'true'",
      rules: { always: [], session: [] },
    };

    expect(approvalLabel(req, "always")).toBe(
      "Always allow · adds no rule, allows this call only",
    );
    expect(approvalLabel(req, "session")).toBe(
      "Allow this session · adds no rule, allows this call only",
    );
  });

  it("leaves requests without rules (plan approval) as they were", () => {
    const req: ApprovalReq = {
      choices: ["once", "session", "deny"],
      command: "Approve this plan?",
      description: "plan",
      labels: { session: "Approve plan + accept edits" },
      requestId: "approval-2",
    };

    expect(approvalLabel(req, "session")).toBe("Approve plan + accept edits");
    expect(approvalLabel(req, "once")).toBe("Allow once");
  });

  it("keeps the rule after a gateway label override", () => {
    const req: ApprovalReq = { ...touch, labels: { always: "Remember" } };

    expect(approvalLabel(req, "always")).toBe(
      "Remember · allow bash touch * [project]",
    );
  });

  it("paints the rule on the prompt before it is stored", () => {
    const screen = paint(touch);

    expect(screen).toContain("touch /home/u/pa1.txt");
    expect(screen).toContain("3. Always allow · allow bash touch * [project]");
    expect(screen).toContain(
      "2. Allow this session · allow bash touch * [session]",
    );
  });
});
