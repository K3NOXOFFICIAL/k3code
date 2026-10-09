import { writeFileSync } from "node:fs";

import type { ScrollBoxHandle } from "@k3code/ink";
import { evictInkCaches } from "@k3code/ink";
import type {
  InflightTurn,
  SessionResumeResult,
  Usage,
} from "@k3code/shared/gateway-events";
import type { ServerRequest } from "@k3code/shared/json-rpc-channel";
import { type RefObject, useCallback, useEffect, useMemo, useRef } from "react";

import { STARTUP_WORKSPACE_CWD } from "../config/env.js";
import {
  buildSetupRequiredSections,
  SETUP_REQUIRED_TITLE,
} from "../content/setup.js";
import { introMsg, toTranscriptMessages } from "../domain/messages.js";
import { ZERO } from "../domain/usage.js";
import { type GatewayClient } from "../gatewayClient.js";
import type {
  SessionActivateResponse,
  SessionCloseResponse,
  SessionCreateResponse,
  SessionTitleResponse,
  SetupStatusResponse,
} from "../gatewayTypes.js";
import { asRpcResult } from "../lib/rpc.js";
import type { Msg, PanelSection, SessionInfo } from "../types.js";

import type {
  ComposerActions,
  GatewayRpc,
  ResumeOutcome,
  StateSetter,
} from "./interfaces.js";
import { patchOverlayState } from "./overlayStore.js";
import {
  forgetServerRequestsForSession,
  serverRequestsForSession,
} from "./serverRequestStore.js";
import { scheduleResumeScrollToBottom } from "./sessionResumeView.js";
import { turnController } from "./turnController.js";
import { patchTurnState } from "./turnStore.js";
import { getUiState, patchUiState } from "./uiStore.js";
import { describeCredentialWarning } from "./userMessages.js";

export {
  refreshSessionView,
  scheduleResumeScrollToBottom,
} from "./sessionResumeView.js";

const usageFrom = (info: null | SessionInfo): Usage =>
  info?.usage ? { ...ZERO, ...info.usage } : ZERO;

const statusFromLiveSession = (status?: string, running = false) => {
  if (status === "waiting") {
    return "waiting for input…";
  }

  if (status === "starting") {
    return "starting agent…";
  }

  return running || status === "working" ? "running…" : "ready";
};

export const writeActiveSessionFile = (
  sessionId: null | string,
  file = process.env.K3CODE_TUI_ACTIVE_SESSION_FILE,
) => {
  if (!file || !sessionId) {
    return;
  }

  try {
    writeFileSync(file, JSON.stringify({ session_id: sessionId }), {
      mode: 0o600,
    });
  } catch {
    // Best-effort shell epilogue hint only; never break live session changes.
  }
};

export const liveSessionInflightMessages = (
  inflight?: null | InflightTurn,
): Msg[] => {
  const user = String(inflight?.user ?? "").trim();

  return user
    ? toTranscriptMessages([
        {
          role: "user",
          text: user,
          ...(inflight?.display_kind
            ? { display_kind: inflight.display_kind }
            : {}),
          ...(inflight?.display_metadata
            ? { display_metadata: inflight.display_metadata }
            : {}),
        },
      ])
    : [];
};

export const hydrateLiveSessionInflight = (inflight?: null | InflightTurn) => {
  const assistant = String(inflight?.assistant ?? "");

  if (!assistant && !inflight?.streaming) {
    return;
  }

  turnController.hydrateStreamingText(assistant);
};

export const signalFreshSessionBoundary = (
  previousSid: null | string,
  nextSid: null | string,
  onFreshSessionStarted?: (sessionId: string) => void,
) => {
  if (
    !previousSid ||
    !nextSid ||
    previousSid === nextSid ||
    !onFreshSessionStarted
  ) {
    return false;
  }

  onFreshSessionStarted(nextSid);

  return true;
};

// The gateway's answer to a session id it does not store (`unknown session: <id>`, JSON-RPC invalid params).
const isUnknownSessionError = (e: unknown) =>
  e instanceof Error && e.message.includes("unknown session");

const trimTail = (items: Msg[]) => {
  const q = [...items];

  while (q.at(-1)?.role === "assistant" || q.at(-1)?.role === "tool") {
    q.pop();
  }

  if (q.at(-1)?.role === "user") {
    q.pop();
  }

  return q;
};

export interface UseSessionLifecycleOptions {
  colsRef: { current: number };
  composerActions: ComposerActions;
  gw: GatewayClient;
  onFreshSessionStarted?: (sessionId: string) => void;
  panel: (title: string, sections: PanelSection[]) => void;
  /** Opens a server→client request's card again (the server-request handler). */
  reopenServerRequest?: (request: ServerRequest) => void;
  rpc: GatewayRpc;
  scrollRef: RefObject<null | ScrollBoxHandle>;
  setHistoryItems: StateSetter<Msg[]>;
  setLastUserMsg: StateSetter<string>;
  setSessionStartedAt: StateSetter<number>;
  setStickyPrompt: StateSetter<string>;
  sys: (text: string) => void;
}

export function useSessionLifecycle(opts: UseSessionLifecycleOptions) {
  const {
    colsRef,
    composerActions,
    gw,
    onFreshSessionStarted,
    panel,
    reopenServerRequest,
    rpc,
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setStickyPrompt,
    sys,
  } = opts;

  // After a switch has settled: the gateway re-sends the new session's open approval/clarify when this client
  // attaches, and that can arrive before the activate/resume `.then` whose resetSession() drops every prompt card.
  // Re-open what is still stored for the new session, so a needs-input session shows its prompt whatever the order.
  const settleServerRequests = useCallback(
    (previousSid: null | string, nextSid: string) => {
      if (previousSid && previousSid !== nextSid) {
        forgetServerRequestsForSession(previousSid);
      }

      for (const request of serverRequestsForSession(nextSid)) {
        reopenServerRequest?.({ ...request, replayed: true });
      }
    },
    [reopenServerRequest],
  );

  const closeSession = useCallback(
    (targetSid?: null | string) =>
      targetSid
        ? rpc<SessionCloseResponse>("session.close", { session_id: targetSid })
        : Promise.resolve(null),
    [rpc],
  );

  const cancelResumeScrollRef = useRef<null | (() => void)>(null);

  const resetSession = useCallback(() => {
    cancelResumeScrollRef.current?.();
    cancelResumeScrollRef.current = null;
    turnController.fullReset();
    patchUiState({
      bgTasks: new Set(),
      info: null,
      sid: null,
      storedSid: null,
      usage: ZERO,
    });
    setHistoryItems([]);
    setLastUserMsg("");
    setStickyPrompt("");
    composerActions.setComposerTokens([]);
    // Half-prune: new session has new keys, but keep a warm pool in case
    // the user resumes back to the prior session.
    evictInkCaches("half");
  }, [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt]);

  useEffect(
    () => () => {
      cancelResumeScrollRef.current?.();
      cancelResumeScrollRef.current = null;
    },
    [],
  );

  const resetVisibleHistory = useCallback(
    (info: null | SessionInfo = null) => {
      turnController.idle();
      turnController.clearReasoning();
      turnController.turnTools = [];
      turnController.persistedToolLabels.clear();

      setHistoryItems(info ? [introMsg(info)] : []);
      setStickyPrompt("");
      setLastUserMsg("");
      composerActions.setComposerTokens([]);
      patchTurnState({ activity: [] });
      patchUiState({ info, usage: usageFrom(info) });
    },
    [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt],
  );

  const startNewSession = useCallback(
    async (msg?: string, title?: string, keepCurrent = false) => {
      const setup = await rpc<SetupStatusResponse>("setup.status", {});

      if (setup?.provider_configured === false) {
        panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections());
        patchUiState({ status: "setup required" });

        return null;
      }

      const previousSid = getUiState().sid;

      const r = await rpc<SessionCreateResponse>("session.create", {
        cols: colsRef.current,
        ...(STARTUP_WORKSPACE_CWD ? { cwd: STARTUP_WORKSPACE_CWD } : {}),
      });

      if (!r) {
        patchUiState({ status: "ready" });

        return null;
      }

      // Close the old session only now that this client is attached to the new one: the daemon refuses to close a
      // session a client is still looking at ("still in use"), and the result used to be ignored, so every /new
      // and /clear left one more live session behind for the daemon's lifetime.
      if (!keepCurrent && previousSid && previousSid !== r.session_id) {
        await closeSession(previousSid);
      }

      // Left for good from this client's view: the gateway re-sends the old session's open requests if it is
      // attached again, so a kept entry could only reopen a card answered or expired elsewhere meanwhile.
      if (previousSid && previousSid !== r.session_id) {
        forgetServerRequestsForSession(previousSid);
      }

      // The durable id lives on the create result; the lazy-create `info` does
      // not carry it, and session.resume / the exit epilogue need the stored id.
      const storedSid = r.stored_session_id || r.session_id;
      const info = r.info ? { ...r.info, stored_session_id: storedSid } : null;
      const requestedTitle = title?.trim() ?? "";

      resetSession();
      setSessionStartedAt(Date.now());

      writeActiveSessionFile(storedSid);
      patchUiState({
        info,
        sid: r.session_id,
        status: info?.version ? "ready" : "starting agent…",
        storedSid,
        usage: usageFrom(info),
      });

      if (info) {
        setHistoryItems([introMsg(info)]);
      }

      if (info?.credential_warning) {
        sys(`warning: ${describeCredentialWarning(info.credential_warning)}`);
      }

      if (info?.config_warning) {
        sys(`warning: ${info.config_warning}`);
      }

      if (msg) {
        sys(msg);
      }

      if (requestedTitle) {
        rpc<SessionTitleResponse>("session.title", {
          session_id: r.session_id,
          title: requestedTitle,
        })
          .then((result) => {
            if (!result || getUiState().sid !== r.session_id) {
              return;
            }

            const nextTitle = (result.title ?? requestedTitle).trim();
            const suffix = result.pending
              ? " (queued while session initializes)"
              : "";
            patchUiState({ sessionTitle: nextTitle });
            sys(`session title set: ${nextTitle}${suffix}`);
          })
          .catch((err: unknown) => {
            if (getUiState().sid !== r.session_id) {
              return;
            }

            const message = err instanceof Error ? err.message : String(err);
            sys(`warning: failed to set session title: ${message}`);
          });
      }

      signalFreshSessionBoundary(
        previousSid,
        r.session_id,
        onFreshSessionStarted,
      );

      return r.session_id;
    },
    [
      closeSession,
      colsRef,
      onFreshSessionStarted,
      panel,
      resetSession,
      rpc,
      setHistoryItems,
      setSessionStartedAt,
      sys,
    ],
  );

  const newSession = useCallback(
    (msg?: string, title?: string) => startNewSession(msg, title, false),
    [startNewSession],
  );

  // `dropSid`: the session this client just left. Once attached elsewhere, ask the gateway to close it if it is
  // disposable (empty, idle, not background); the gateway judges, and `closed: false` or an error is not a failure.
  const dropAfterSwitch = useCallback(
    (dropSid: string | undefined, nowSid: null | string | undefined) => {
      if (dropSid && nowSid && dropSid !== nowSid) {
        void gw
          .request<SessionCloseResponse>("session.close", {
            disposable_only: true,
            session_id: dropSid,
          })
          .catch(() => null);
      }
    },
    [gw],
  );

  const newLiveSession = useCallback(
    (msg = "new live session started", title?: string, dropSid?: string) => {
      patchOverlayState({ agentView: false, sessions: false });

      return startNewSession(msg, title, true).then((sid) => {
        dropAfterSwitch(dropSid, sid);

        return sid;
      });
    },
    [dropAfterSwitch, startNewSession],
  );

  const activateLiveSession = useCallback(
    (id: string, dropSid?: string) => {
      patchOverlayState({ agentView: false, sessions: false });
      patchUiState({ status: "switching session…" });
      const previousSid = getUiState().sid;

      gw.request<SessionActivateResponse>("session.activate", {
        session_id: id,
      })
        .then((raw) => {
          const r = asRpcResult<SessionActivateResponse>(raw);

          if (!r) {
            sys("error: invalid response: session.activate");

            return patchUiState({ status: "ready" });
          }

          const info = r.info ?? null;
          // Agent-less (lazy) activations answer with `_fallback_session_info`, which
          // has no stored_session_id; the durable id is the response's session_key.
          const storedSid = r.session_key || r.session_id;
          const running = Boolean(
            r.running || r.status === "working" || r.status === "waiting",
          );

          resetSession();
          setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now());
          const transcript = [
            ...toTranscriptMessages(r.messages),
            ...liveSessionInflightMessages(r.inflight),
          ];
          setHistoryItems(info ? [introMsg(info), ...transcript] : transcript);
          writeActiveSessionFile(storedSid);
          patchUiState({
            busy: running,
            info,
            sid: r.session_id,
            status: statusFromLiveSession(r.status, running),
            storedSid,
            usage: usageFrom(info),
          });
          hydrateLiveSessionInflight(r.inflight);
          settleServerRequests(previousSid, r.session_id);

          cancelResumeScrollRef.current?.();
          cancelResumeScrollRef.current =
            scheduleResumeScrollToBottom(scrollRef);
          dropAfterSwitch(dropSid, r.session_id);
        })
        .catch((e: Error) => {
          sys(`error: ${e.message}`);
          patchUiState({ status: "ready" });
        });
    },
    [
      dropAfterSwitch,
      gw,
      resetSession,
      scrollRef,
      setHistoryItems,
      setSessionStartedAt,
      settleServerRequests,
      sys,
    ],
  );

  const resumeById = useCallback(
    (id: string): Promise<ResumeOutcome> => {
      patchOverlayState({ agentView: false, sessions: false });
      patchUiState({ status: "resuming…" });

      return rpc<SetupStatusResponse>("setup.status", {}).then((setup) => {
        if (setup?.provider_configured === false) {
          panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections());
          patchUiState({ status: "setup required" });

          return;
        }

        const previousSid = getUiState().sid;

        return gw
          .request<SessionResumeResult>("session.resume", {
            cols: colsRef.current,
            session_id: id,
          })
          .then((raw) => {
            const r = asRpcResult<SessionResumeResult>(raw);

            if (!r) {
              sys("error: invalid response: session.resume");
              patchUiState({ status: "ready" });

              return;
            }

            const storedSid =
              r.info?.stored_session_id ||
              r.stored_session_id ||
              r.resumed ||
              id;
            const info = r.info
              ? { ...r.info, stored_session_id: storedSid }
              : null;

            const running = Boolean(
              r.running || r.status === "working" || r.status === "waiting",
            );

            resetSession();
            setSessionStartedAt(
              r.started_at ? r.started_at * 1000 : Date.now(),
            );

            const resumed = [
              ...toTranscriptMessages(r.messages),
              ...liveSessionInflightMessages(r.inflight),
            ];

            setHistoryItems(info ? [introMsg(info), ...resumed] : resumed);
            writeActiveSessionFile(storedSid);
            patchUiState({
              busy: running,
              info,
              sid: r.session_id,
              status: statusFromLiveSession(r.status ?? undefined, running),
              storedSid,
              usage: usageFrom(info),
            });
            hydrateLiveSessionInflight(r.inflight);
            settleServerRequests(previousSid, r.session_id);

            cancelResumeScrollRef.current?.();
            cancelResumeScrollRef.current =
              scheduleResumeScrollToBottom(scrollRef);

            if (previousSid && previousSid !== r.session_id) {
              void closeSession(previousSid);
            }
          })
          .catch((e: Error): ResumeOutcome => {
            sys(`error: ${e.message}`);
            patchUiState({ status: "ready" });

            return isUnknownSessionError(e) ? "unknown-session" : undefined;
          });
      });
    },
    [
      closeSession,
      colsRef,
      gw,
      panel,
      resetSession,
      rpc,
      scrollRef,
      setHistoryItems,
      setSessionStartedAt,
      settleServerRequests,
      sys,
    ],
  );

  const guardBusySessionSwitch = useCallback(
    (what = "switch sessions") => {
      if (!getUiState().busy) {
        return false;
      }

      sys(`interrupt the current turn before trying to ${what}`);

      return true;
    },
    [sys],
  );

  return useMemo(
    () => ({
      activateLiveSession,
      closeSession,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      trimLastExchange: trimTail,
    }),
    [
      activateLiveSession,
      closeSession,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
    ],
  );
}
