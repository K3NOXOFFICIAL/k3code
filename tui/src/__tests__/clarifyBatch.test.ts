import { beforeEach, describe, expect, it, vi } from "vitest";

import { answerClarifyBatchRequest } from "../app/clarifyBatch.js";
import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import {
  rememberServerRequest,
  resetServerRequestsForTests,
} from "../app/serverRequestStore.js";
import { turnController } from "../app/turnController.js";
import { resetTurnState } from "../app/turnStore.js";
import { resetUiState } from "../app/uiStore.js";
import { answerClarifyRequest } from "../app/useMainApp.js";
import type { ClarifyReq, Msg } from "../types.js";

const REQ: ClarifyReq = {
  choices: null,
  question: "",
  questions: [
    {
      choices: ["lint", "test"],
      multiSelect: true,
      qid: "q1",
      question: "Which checks?",
    },
    { choices: ["a", "b"], qid: "q2", question: "Which branch?" },
    { choices: null, qid: "q3", question: "Anything else?" },
  ],
  requestId: "req-auq",
};

// Open the server request + overlay the way createServerRequestHandler does.
const open = () => {
  const respond = vi.fn();

  rememberServerRequest({
    fail: vi.fn(),
    id: REQ.requestId,
    method: "clarify",
    params: {},
    respond,
  });
  patchOverlayState({ clarify: REQ });

  return respond;
};

describe("answerClarifyBatchRequest", () => {
  beforeEach(() => {
    resetOverlayState();
    resetServerRequestsForTests();
    resetTurnState();
    resetUiState();
    turnController.turnTools = [];
  });

  it("sends the whole set as ONE {answers} frame and echoes decoded answers", () => {
    const respond = open();
    const appended: Msg[] = [];
    const answers = { q1: '["lint","test"]', q2: '["x"]' };

    answerClarifyBatchRequest(REQ, { answers }, (m) => appended.push(m));

    expect(respond).toHaveBeenCalledTimes(1);
    expect(respond).toHaveBeenCalledWith({ answers });
    expect(appended[0]?.kind).toBe("trail");
    expect(appended[0]?.tools?.[0]).toContain("Ask User Question");
    expect(appended[1]).toEqual({
      role: "user",
      text: [
        "Which checks? → lint, test",
        'Which branch? → ["x"]',
        "Anything else? → (skipped)",
      ].join("\n"),
    });
    expect(getOverlayState().clarify).toBeNull();
  });

  it("sends {answers, chat: true} for Chat about this", () => {
    const respond = open();
    const appended: Msg[] = [];

    answerClarifyBatchRequest(REQ, { answers: { q2: "a" }, chat: true }, (m) =>
      appended.push(m),
    );

    expect(respond).toHaveBeenCalledTimes(1);
    expect(respond).toHaveBeenCalledWith({ answers: { q2: "a" }, chat: true });
    expect(appended[1]?.text).toContain("Which branch? → a");
    expect(appended[1]?.text).toContain("Which checks? → (skipped)");
    expect(appended[1]?.text).toContain("(chat about this)");
    expect(getOverlayState().clarify).toBeNull();
  });

  it("cancels with {answer: ''} and records the abandoned questions", () => {
    const respond = open();
    const appended: Msg[] = [];

    answerClarifyBatchRequest(REQ, null, (m) => appended.push(m));

    expect(respond).toHaveBeenCalledTimes(1);
    expect(respond).toHaveBeenCalledWith({ answer: "" });
    expect(appended).toHaveLength(1);
    expect(appended[0]?.role).toBe("system");
    expect(appended[0]?.text).toMatch(/^ask \(3 questions\)/);
    expect(appended[0]?.text).toContain("(cancelled)");
    expect(getOverlayState().clarify).toBeNull();
  });

  it("drops the live clarify / ask_user_question trail lines", () => {
    open();
    turnController.turnTools = [
      'Ask User Question("Which checks?")',
      'Clarify("old")',
      'Read("x")',
    ];

    answerClarifyBatchRequest(REQ, { answers: {} }, () => {});

    expect(turnController.turnTools).toEqual(['Read("x")']);
  });

  it("only clears the overlay when the request already expired", () => {
    patchOverlayState({ clarify: REQ });
    const appended: Msg[] = [];

    answerClarifyBatchRequest(REQ, { answers: { q2: "a" } }, (m) =>
      appended.push(m),
    );

    expect(appended).toHaveLength(0);
    expect(getOverlayState().clarify).toBeNull();
  });
});

describe("answerClarifyRequest", () => {
  beforeEach(() => {
    resetOverlayState();
    resetServerRequestsForTests();
    resetTurnState();
    resetUiState();
    turnController.turnTools = [];
  });

  it("hands Ctrl+C's empty answer on a batch to the batch cancel", () => {
    const respond = open();
    const appended: Msg[] = [];

    answerClarifyRequest(REQ, "", (m) => appended.push(m));

    expect(respond).toHaveBeenCalledTimes(1);
    expect(respond).toHaveBeenCalledWith({ answer: "" });
    expect(appended).toHaveLength(1);
    expect(appended[0]?.role).toBe("system");
    expect(appended[0]?.text).toMatch(/^ask \(3 questions\)/);
    expect(appended[0]?.text).toContain("Which checks?");
    expect(appended[0]?.text).toContain("(cancelled)");
    expect(getOverlayState().clarify).toBeNull();
  });
});
