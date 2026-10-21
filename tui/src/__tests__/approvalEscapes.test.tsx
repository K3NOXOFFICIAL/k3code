import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import {
  ApprovalPrompt,
  ClarifyBatchFallback,
  ClarifyPrompt,
} from "../components/prompts.js";
import { visibleControls } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";
import type { ApprovalReq, ClarifyReq } from "../types.js";

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
// Title set, erase line, cursor up: what lets a prompt repaint over itself.
const TITLE = `${ESC}]0;TITLE${BEL}`;
const ERASE = `${ESC}[2K`;
const UP = `${ESC}[1A`;
const ATTACK = `MARK${TITLE}${ERASE}${UP}hidden\rOVER`;

// Paint `node` once and return the bytes written to stdout, escapes intact.
const paintRaw = (node: React.ReactElement): string => {
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
    output += chunk.toString();
  });

  const view = renderSync(node, {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stderr: new PassThrough() as unknown as NodeJS.WriteStream,
    patchConsole: false,
  });

  view.unmount();
  view.cleanup();

  return output;
};

// None of the request's escape sequences may reach the terminal.
const expectNoRequestEscapes = (raw: string) => {
  expect(raw).not.toContain(TITLE);
  expect(raw).not.toContain(`${ESC}]0;`);
  expect(raw).not.toContain(ERASE);
  expect(raw).not.toContain(UP);
  expect(raw).not.toContain(BEL);
  expect(raw).not.toContain("\r");
};

const approval = (over: Partial<ApprovalReq>): ApprovalReq => ({
  command: "ls",
  description: "bash",
  requestId: "approval-1",
  ...over,
});

const paintApproval = (req: ApprovalReq) =>
  paintRaw(
    <ApprovalPrompt
      cols={100}
      onChoice={() => {}}
      req={req}
      t={DEFAULT_THEME}
    />,
  );

const paintClarify = (req: ClarifyReq) =>
  paintRaw(
    <ClarifyPrompt
      cols={100}
      onAnswer={() => {}}
      onCancel={() => {}}
      req={req}
      t={DEFAULT_THEME}
    />,
  );

// Batch requests (`questions[]`) render through the batch fallback, not ClarifyPrompt.
const paintClarifyBatch = (req: ClarifyReq) =>
  paintRaw(
    <ClarifyBatchFallback onCancel={() => {}} req={req} t={DEFAULT_THEME} />,
  );

describe("visibleControls", () => {
  it("shows escape and control bytes as visible symbols", () => {
    expect(visibleControls(`a${ESC}[2Kb`)).toBe("a␛[2Kb");
    expect(visibleControls(`a${BEL}b\rc`)).toBe("a␇b␍c");
    expect(visibleControls("a\u007fb")).toBe("a␡b");
  });

  it("escapes C1 controls and bidi overrides by code point", () => {
    expect(visibleControls("a\u009bb")).toBe("a\\x9bb");
    expect(visibleControls("a‮b")).toBe("a\\u202eb");
  });

  it("keeps newline, tab and ordinary text", () => {
    expect(visibleControls("a\n\tb ünï 日本")).toBe("a\n\tb ünï 日本");
  });
});

describe("approval prompt does not paint request escape sequences", () => {
  it("benign request paints plain text", () => {
    expect(stripAnsi(paintApproval(approval({ command: "ls -la" })))).toContain(
      "ls -la",
    );
  });

  it("neutralizes the command", () => {
    const raw = paintApproval(approval({ command: `echo ${ATTACK}` }));

    expectNoRequestEscapes(raw);
    expect(stripAnsi(raw)).toContain("echo MARK␛]0;TITLE␇␛[2K␛[1Ahidden␍OVER");
  });

  it("neutralizes the description", () => {
    expectNoRequestEscapes(paintApproval(approval({ description: ATTACK })));
  });

  it("neutralizes the edit/write preview", () => {
    expectNoRequestEscapes(
      paintApproval(approval({ preview: `--- a\n+++ b\n+${ATTACK}` })),
    );
  });

  it("neutralizes the rules and label overrides", () => {
    expectNoRequestEscapes(
      paintApproval(
        approval({
          labels: { once: ATTACK },
          rules: { always: [ATTACK], session: [ATTACK] },
        }),
      ),
    );
  });
});

describe("clarify prompt does not paint request escape sequences", () => {
  it("neutralizes the question and choices", () => {
    expectNoRequestEscapes(
      paintClarify({
        choices: [ATTACK, "b"],
        question: ATTACK,
        requestId: "c-1",
      }),
    );
  });

  it("neutralizes batch questions, headers, choices and descriptions", () => {
    const raw = paintClarifyBatch({
      answers: { q2: ATTACK },
      choices: null,
      question: "",
      questions: [
        {
          choices: [ATTACK],
          descriptions: [ATTACK],
          header: ATTACK,
          qid: "q1",
          question: ATTACK,
        },
        { choices: null, qid: "q2", question: ATTACK },
      ],
      requestId: "c-2",
    });

    expectNoRequestEscapes(raw);
    expect(stripAnsi(raw)).toContain("MARK␛]0;TITLE␇");
  });
});

describe("visibleControls on a missing field", () => {
  it("is empty, as an unset description painted before", () => {
    expect(visibleControls(undefined)).toBe("");
    expect(visibleControls(null)).toBe("");
  });
});
