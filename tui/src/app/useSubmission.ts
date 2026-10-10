import { looksLikeSlashCommand, parseSlashCommand } from "@k3code/shared/slash";
import type { Span } from "@k3code/shared/wake-words";
import { type MutableRefObject, useCallback, useEffect, useRef } from "react";

import { TYPING_IDLE_MS } from "../config/timing.js";
import {
  codePointSpans,
  expandTokens,
  expandTokensWithSpans,
  labelSpans,
} from "../domain/attachments.js";
import { completionToApplyOnSubmit } from "../domain/slash.js";
import type { GatewayClient } from "../gatewayClient.js";
import type {
  SessionSteerResponse,
  ShellExecResponse,
} from "../gatewayTypes.js";
import { queueItem, type QueueItem } from "../hooks/useQueue.js";
import { asRpcResult } from "../lib/rpc.js";
import { INTERPOLATION_RE } from "../protocol/interpolation.js";
import type { Msg } from "../types.js";

import type {
  ComposerActions,
  ComposerRefs,
  ComposerState,
  ComposerToken,
} from "./interfaces.js";
import { submitPrompt } from "./submissionCore.js";
import { turnController } from "./turnController.js";
import { getUiState, patchUiState } from "./uiStore.js";

const DOUBLE_ENTER_MS = 450;

const spliceMatches = (
  text: string,
  matches: RegExpMatchArray[],
  results: string[],
) =>
  matches.reduceRight(
    (acc, m, i) =>
      acc.slice(0, m.index!) + results[i] + acc.slice(m.index! + m[0].length),
    text,
  );

// `{!cmd}` the user typed: one inside (or reaching into) a paste is content, never run.
const typedInterpolations = (text: string, pasted: readonly Span[]) =>
  [...text.matchAll(new RegExp(INTERPOLATION_RE.source, "g"))].filter(
    (m) => !pasted.some(([a, b]) => m.index! < b && m.index! + m[0].length > a),
  );

// The paste spans once each match was replaced by its result.
const shiftSpans = (
  spans: readonly Span[],
  matches: RegExpMatchArray[],
  results: string[],
): Span[] =>
  spans.map(([a, b]) => {
    const by = matches.reduce(
      (acc, m, i) =>
        m.index! < a ? acc + results[i]!.length - m[0].length : acc,
      0,
    );

    return [a + by, b + by] as const;
  });

export const expandPasteTokens = (tokens: ComposerToken[]) =>
  expandTokens(tokens.filter((token) => token.kind === "paste"));

const slashArgument = (command: string) =>
  /^\/\S+\s+([\s\S]+)$/.exec(command)?.[1] ?? "";

/**
 * The item a `/queue <text>` queues. With the composer's `tokens`, a collapsed paste in the argument is expanded and
 * its span kept with the item, so it still counts as pasted when the item is sent.
 */
export const queueItemFromSlash = (
  displayCommand: string,
  expandedCommand: string,
  tokens: ComposerToken[] = [],
): QueueItem | undefined => {
  const display = slashArgument(displayCommand);

  if (!display.trim()) {
    return undefined;
  }

  if (!tokens.some((token) => token.kind === "paste")) {
    return queueItem(slashArgument(expandedCommand), display);
  }

  const { pasteSpans, text } = expandTokensWithSpans(
    tokens.filter((token) => token.kind === "paste"),
  )(display);

  return queueItem(text, display, pasteSpans);
};

// `pasteSpans`: where the pastes ended up in `text`, so the gateway does not
// read a wake word inside a pasted log as the user asking for a mode.
export const prepareSubmission = (display: string, tokens: ComposerToken[]) => {
  const { pasteSpans, text } = expandTokensWithSpans(tokens)(display);

  return { display, pasteSpans, text };
};

/**
 * Split a slash submission into the two things it has to be at once.
 *
 * A slash command's argument is ordinary user text, so a collapsed paste in it
 * must resolve BEFORE the command runs — otherwise `/pr-triage [[ … [412 lines]
 * … ]]` hands the skill the label and the agent faithfully reports that the
 * paste is truncated. The transcript still shows the compact form, because a
 * 412-line paste inlined into the scrollback is exactly what collapsing it was
 * for.
 *
 * Image tokens stay as labels: the gateway already holds those files in
 * `attached_images` and splices them in at submit.
 */
export const prepareSlashSubmission = (
  display: string,
  tokens: ComposerToken[],
) => ({
  command: expandPasteTokens(tokens)(display),
  display,
});

// `tokens`: the `[[ … ]]` labels of the composer's pastes. A label quotes the start of a paste, so a `{!cmd}` that
// shows in one is the paste's, not the user's.
export const shouldInterpolateSubmission = (
  display: string,
  tokens?: ComposerToken[],
) =>
  typedInterpolations(display, tokens ? labelSpans(display, tokens) : [])
    .length > 0;

export function useSubmission(opts: UseSubmissionOptions) {
  const {
    appendMessage,
    composerActions,
    composerRefs,
    composerState,
    gw,
    setLastUserMsg,
    slashRef,
    submitRef,
    sys,
  } = opts;

  const lastEmptyAt = useRef(0);
  // Whether the last prompt went out as `automated` (see submitPrompt): /retry sends it again the same way.
  const lastAutomated = useRef(false);
  // ...and where its pastes were, kept with the text they count in.
  const lastPaste = useRef<{ spans: readonly Span[]; text: string }>({
    spans: [],
    text: "",
  });
  const typingIdleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (typingIdleTimer.current) {
      clearTimeout(typingIdleTimer.current);
      typingIdleTimer.current = null;
    }

    if (!composerState.input && !composerState.inputBuf.length) {
      turnController.relaxStreaming();

      return;
    }

    if (getUiState().busy) {
      turnController.boostStreamingForTyping();
    }

    typingIdleTimer.current = setTimeout(() => {
      typingIdleTimer.current = null;
      turnController.relaxStreaming();
    }, TYPING_IDLE_MS);

    return () => {
      if (typingIdleTimer.current) {
        clearTimeout(typingIdleTimer.current);
        typingIdleTimer.current = null;
      }
    };
  }, [composerState.input, composerState.inputBuf]);

  const send = useCallback(
    (
      text: string,
      showUserMessage = true,
      displayText?: string,
      expandOverride?: (value: string) => string,
      submitOpts: {
        automated?: boolean;
        pasteSpans?: readonly (readonly [number, number])[];
        skipDetectDrop?: boolean;
      } = {},
    ) => {
      // Read tokens off the ref, not render state: a paste immediately followed
      // by Enter submits before React has re-rendered with the new token.
      const withSpans = expandTokensWithSpans(composerRefs.tokensRef.current);
      const expand =
        expandOverride ?? ((value: string) => withSpans(value).text);

      if (!expandOverride && submitOpts.pasteSpans === undefined) {
        submitOpts = { ...submitOpts, pasteSpans: withSpans(text).pasteSpans };
      }

      submitPrompt(
        text,
        {
          appendMessage,
          enqueue: composerActions.enqueue,
          expand,
          gw,
          // set when the prompt really goes out, so a send that stops early (no session yet) leaves /retry alone
          setLastUserMsg: (value) => {
            lastAutomated.current = submitOpts.automated === true;
            // With an `expandOverride` the text is final and the spans count in it, so /retry can send both again;
            // without one `text` still holds `[[ … ]]` labels and the spans count in its expansion.
            lastPaste.current = {
              spans: expandOverride ? (submitOpts.pasteSpans ?? []) : [],
              text: value,
            };
            setLastUserMsg(value);
          },
          sys,
        },
        showUserMessage,
        displayText,
        submitOpts,
      );
    },
    [appendMessage, composerActions, composerRefs, gw, setLastUserMsg, sys],
  );

  // The last submission again (/retry), automated or typed exactly as it first went out.
  const resend = useCallback(
    (text: string) => {
      if (lastAutomated.current) {
        return send(text, true, undefined, undefined, { automated: true });
      }

      // the same text again: its pastes are where they were (and it is not expanded a second time)
      if (lastPaste.current.spans.length && lastPaste.current.text === text) {
        return send(text, true, undefined, (value) => value, {
          pasteSpans: lastPaste.current.spans,
        });
      }

      send(text);
    },
    [send],
  );

  // Text the gateway generated (a /skill expansion, the /go send, a goal kick), not typed by the user: `automated`, so
  // neither the wake words nor the ultracode mode look at it.
  const sendAutomated = useCallback(
    (text: string, showUserMessage = true, displayText?: string) =>
      send(text, showUserMessage, displayText, undefined, { automated: true }),
    [send],
  );

  const shellExec = useCallback(
    (cmd: string) => {
      appendMessage({ role: "user", text: `!${cmd}` });
      patchUiState({ busy: true, status: "running…" });

      gw.request<ShellExecResponse>("shell.exec", { command: cmd })
        .then((raw) => {
          const r = asRpcResult<ShellExecResponse>(raw);

          if (!r) {
            return sys("error: invalid response: shell.exec");
          }

          const out = [r.stdout, r.stderr].filter(Boolean).join("\n").trim();

          if (out) {
            sys(out);
          }

          if (r.code !== 0 || !out) {
            sys(`exit ${r.code}`);
          }
        })
        .catch((e: Error) => sys(`error: ${e.message}`))
        .finally(() => patchUiState({ busy: false, status: "ready" }));
    },
    [appendMessage, gw, sys],
  );

  // `pasted`: spans of `text` the user did not type; a `{!cmd}` in or across one is left as it is. `then` gets the
  // result and where those spans are in it.
  const interpolate = useCallback(
    (
      text: string,
      then: (result: string, pasteSpans: Span[]) => void,
      pasted: readonly Span[] = [],
    ) => {
      patchUiState({ status: "interpolating…" });
      const matches = typedInterpolations(text, pasted);

      Promise.all(
        matches.map((m) =>
          gw
            .request<ShellExecResponse>("shell.exec", { command: m[1]! })
            .then((raw) => {
              const r = asRpcResult<ShellExecResponse>(raw);

              return [r?.stdout, r?.stderr].filter(Boolean).join("\n").trim();
            })
            .catch(() => "(error)"),
        ),
      ).then((results) =>
        then(
          spliceMatches(text, matches, results),
          shiftSpans(pasted, matches, results),
        ),
      );
    },
    [gw],
  );

  // A queued prompt holds its expanded pastes (`pasteSpans`): only what the user typed runs as `!cmd` or `{!cmd}`, and
  // the text goes out as it is (identity `expand`), so the spans still fit it.
  const sendQueued = useCallback(
    (text: string, pasteSpans: readonly Span[] = [], display?: string) => {
      if (text.startsWith("!") && !pasteSpans.length) {
        return shellExec(text.slice(1).trim());
      }

      if (typedInterpolations(text, pasteSpans).length) {
        patchUiState({ busy: true });

        return interpolate(
          text,
          (result, spans) =>
            send(result, true, undefined, (value) => value, {
              pasteSpans: spans,
            }),
          pasteSpans,
        );
      }

      send(text, true, display, (value) => value, { pasteSpans });
    },
    [interpolate, send, shellExec],
  );

  // Honors `display.busy_input_mode` from config.yaml (CLI parity):
  //   - 'queue'     (legacy): append to queueRef; drains on busy → false
  //   - 'steer'     : inject into the current turn via session.steer; falls
  //                   back to queue when steer is rejected (no agent / no
  //                   tool window).
  //   - 'interrupt' (default): submit immediately; the backend redirects the
  //                   active model request (or safely steers after a tool),
  //                   with legacy interrupt + queue as its compatibility path.
  //
  // `opts.fallbackToFront` re-inserts at the queue head (queue-edit picks keep
  // their position); the mainline submit path appends.
  const handleBusyInput = useCallback(
    (item: QueueItem, opts: { fallbackToFront?: boolean } = {}) => {
      const live = getUiState();
      const mode = live.busyInputMode;

      const enqueueText = () => {
        if (opts.fallbackToFront) {
          composerActions.prependQueue(item);
        } else {
          composerActions.enqueue(item.text, item.display, item.pasteSpans);
        }
      };

      const fallback = (note: string) => {
        enqueueText();
        sys(note);
      };

      if (mode === "queue") {
        return enqueueText();
      }

      if (mode === "steer" && live.sid) {
        gw.request<SessionSteerResponse>("session.steer", {
          session_id: live.sid,
          text: item.text,
          ...(item.pasteSpans?.length
            ? { paste_spans: codePointSpans(item.text, item.pasteSpans) }
            : {}),
        })
          .then((raw) => {
            const r = asRpcResult<SessionSteerResponse>(raw);

            if (r?.status !== "queued") {
              fallback("steer rejected — message queued for next turn");
            }
          })
          .catch(() => fallback("steer failed — message queued for next turn"));

        return;
      }

      // The gateway owns the atomic redirect decision because it knows whether
      // the agent is in model generation, tool execution, or an older runtime.
      // Reuse the normal submit pipeline so the correction gets its user bubble
      // and file-drop interpolation exactly once. Its text is already
      // expanded: the bubble shows the compact display, and the text goes out
      // as it is, so its paste spans still fit.
      send(item.text, true, item.display, (value) => value, {
        pasteSpans: item.pasteSpans ?? [],
      });
    },
    [composerActions, gw, send, sys],
  );

  const dispatchSubmission = useCallback(
    // `queuedSpans`: `full` is a queued item's text (already expanded), and
    // this is where its pastes are; `queuedDisplay` is how the item shows.
    (
      full: string,
      queuedSpans?: readonly (readonly [number, number])[],
      queuedDisplay?: string,
    ) => {
      if (!full.trim()) {
        return;
      }

      // History stores resolved content, not `[[…]]` labels: tokens are cleared
      // on submit, so recall must be self-contained. Image tokens resolve to
      // nothing — a detached image can't be re-attached by recalling the text.
      // Idempotent on token-free text, so re-submitting a recalled entry is
      // stable.
      // A queued item holding pastes (Ctrl+K, a double Enter) is already
      // expanded: sent as the drain sends it, so pasted text is never read as a
      // slash command, `!cmd` or `{!cmd}`, and keeps its spans.
      if (queuedSpans?.length) {
        if (!getUiState().sid) {
          return composerActions.enqueue(
            full,
            queuedDisplay ?? full,
            queuedSpans,
          );
        }

        return getUiState().busy
          ? handleBusyInput({
              ...queueItem(full, queuedDisplay),
              pasteSpans: queuedSpans,
            })
          : sendQueued(full, queuedSpans, queuedDisplay);
      }

      const submissionTokens = [...composerRefs.tokensRef.current];
      const submission = prepareSubmission(full, submissionTokens);
      const toHistory = submission.text;

      if (looksLikeSlashCommand(full)) {
        const slash = prepareSlashSubmission(full, submissionTokens);

        appendMessage({ kind: "slash", role: "system", text: slash.display });
        composerActions.pushHistory(toHistory);

        const parsed = parseSlashCommand(full);

        const queued =
          parsed.name === "queue" || parsed.name === "q"
            ? queueItemFromSlash(slash.display, slash.command, submissionTokens)
            : undefined;

        if (queued) {
          composerActions.enqueue(
            queued.text,
            queued.display,
            queued.pasteSpans,
          );
          sys(
            `queued: "${queued.display.slice(0, 50)}${queued.display.length > 50 ? "…" : ""}"`,
          );
        } else {
          slashRef.current(slash.command);
        }

        composerActions.clearIn();

        return;
      }

      // a shell escape is what the user typed: with a paste in it, it is a prompt
      if (full.startsWith("!") && !labelSpans(full, submissionTokens).length) {
        composerActions.clearIn();

        return shellExec(full.slice(1).trim());
      }

      const live = getUiState();

      if (!live.sid) {
        composerActions.pushHistory(toHistory);
        // The tokens are cleared below, so queue the expansion (and where its
        // pastes are), not the `[[…]]` labels that would then go out as-is.
        composerActions.enqueue(submission.text, full, submission.pasteSpans);
        composerActions.clearIn();

        return;
      }

      const editIdx = composerRefs.queueEditRef.current;
      composerActions.clearIn();

      if (editIdx !== null) {
        // the composer's tokens are cleared above, but a paste added while editing is still in `full`
        const picked = composerActions.takeQueue(
          editIdx,
          full,
          submissionTokens,
        );
        composerActions.setQueueEdit(null);

        if (!picked || !live.sid) {
          return;
        }

        if (getUiState().busy) {
          // 'interrupt' / 'steer' should reach the live turn instead of
          // silently going back to the queue.  handleBusyInput resolves
          // mode-specific behavior (interrupt-and-send, steer, or queue).
          if (getUiState().busyInputMode === "queue") {
            return composerActions.prependQueue(picked);
          }

          return handleBusyInput(picked, { fallbackToFront: true });
        }

        return sendQueued(picked.text, picked.pasteSpans, picked.display);
      }

      composerActions.pushHistory(toHistory);

      if (getUiState().busy) {
        return handleBusyInput(
          queueItem(submission.text, full, submission.pasteSpans),
        );
      }

      if (shouldInterpolateSubmission(full, submissionTokens)) {
        patchUiState({ busy: true });

        return interpolate(
          full,
          (text) => {
            const prepared = prepareSubmission(text, submissionTokens);

            send(prepared.text, true, text, (value) => value, {
              pasteSpans: prepared.pasteSpans,
            });
          },
          labelSpans(full, submissionTokens),
        );
      }

      send(submission.text, true, submission.display, (value) => value, {
        pasteSpans: submission.pasteSpans,
      });
    },
    [
      appendMessage,
      composerActions,
      composerRefs,
      handleBusyInput,
      interpolate,
      send,
      sendQueued,
      shellExec,
      slashRef,
      sys,
    ],
  );

  const submit = useCallback(
    (value: string) => {
      if (composerState.completions.length) {
        const row = composerState.completions[composerState.compIdx];
        const next = completionToApplyOnSubmit(
          value,
          row?.text,
          composerState.compReplace,
        );

        if (next !== null) {
          return composerActions.setInput(next);
        }
      }

      if (!value.trim() && !composerState.inputBuf.length) {
        const live = getUiState();
        const now = Date.now();
        const doubleTap = now - lastEmptyAt.current < DOUBLE_ENTER_MS;
        lastEmptyAt.current = now;

        if (doubleTap && live.busy && live.sid) {
          // Force-send: keep busy when a message is queued so the settle edge
          // drains it once (no race). Empty queue = plain Stop → 'ready'.
          const hasQueued = composerRefs.queueRef.current.length > 0;

          return turnController.interruptTurn(
            { appendMessage, gw, sid: live.sid, sys },
            { keepBusy: hasQueued },
          );
        }

        if (doubleTap && live.sid && composerRefs.queueRef.current.length) {
          const next = composerActions.dequeueItem();

          if (next) {
            composerActions.setQueueEdit(null);
            dispatchSubmission(next.text, next.pasteSpans ?? [], next.display);
          }
        }

        return;
      }

      lastEmptyAt.current = 0;

      if (value.endsWith("\\")) {
        composerActions.setInputBuf((prev) => [...prev, value.slice(0, -1)]);

        return composerActions.setInput("");
      }

      dispatchSubmission([...composerState.inputBuf, value].join("\n"));
    },
    [
      appendMessage,
      composerActions,
      composerRefs,
      composerState,
      dispatchSubmission,
      gw,
      sys,
    ],
  );

  submitRef.current = submit;

  // Literal submission: route text straight to the prompt pipeline, skipping
  // slash-command routing, `!` shell dispatch, [[token]] expansion, and
  // $(...) interpolation. Startup `-q` queries use this — they're arbitrary
  // launcher/script text, and one-shot mode already treats them literally.
  const submitLiteral = useCallback(
    (value: string, opts: { automated?: boolean } = {}) => {
      if (!value.trim()) {
        return;
      }

      send(value, true, value, (v) => v, {
        skipDetectDrop: true,
        ...(opts.automated ? { automated: true } : {}),
      });
    },
    [send],
  );

  return {
    dispatchSubmission,
    resend,
    send,
    sendAutomated,
    sendQueued,
    submit,
    submitLiteral,
  };
}

export interface UseSubmissionOptions {
  appendMessage: (msg: Msg) => void;
  composerActions: ComposerActions;
  composerRefs: ComposerRefs;
  composerState: ComposerState;
  gw: GatewayClient;
  setLastUserMsg: (value: string) => void;
  slashRef: MutableRefObject<(cmd: string) => boolean>;
  submitRef: MutableRefObject<(value: string) => void>;
  sys: (text: string) => void;
}
