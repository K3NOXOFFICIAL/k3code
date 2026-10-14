import { sendAgentSteer } from "../components/agentControls.js";
import type { GatewayClient } from "../gatewayClient.js";
import { asRpcResult, rpcErrorMessage } from "../lib/rpc.js";

import {
  setAgentViewDraft,
  type ViewNotice,
  type ViewRow,
} from "./agentViewStore.js";

/** What the agent view's actions need from the app: the gateway and the attached session, read at call time. */
export interface ViewActionDeps {
  /** The directory a background agent starts in (`backgroundCwd`). */
  cwd: () => string;
  gw: GatewayClient;
  /** Called after a rename of the attached session, so its title updates at once. */
  onCurrentTitle?: (title: string) => void;
  sid: () => null | string;
}

const info = (text: string, sessionId?: string): ViewNotice =>
  sessionId ? { sessionId, text, tone: "info" } : { text, tone: "info" };

const fail = (text: string): ViewNotice => ({ text, tone: "error" });

export const NO_SESSION = fail("no session yet — wait for it to start");

const short = (id: string) => id.slice(0, 8);

/** Every failure becomes an error notice: the transcript is hidden behind the view. */
const guarded = async (run: () => Promise<ViewNotice>): Promise<ViewNotice> => {
  try {
    return await run();
  } catch (e) {
    return fail(rpcErrorMessage(e));
  }
};

/** Text on "+ New agent" (or `/bg`, Ctrl+N with text): a new background session started from the attached one. */
export const dispatchBackground = (deps: ViewActionDeps, text: string) =>
  guarded(async () => {
    const sid = deps.sid();

    if (!sid) {
      return NO_SESSION;
    }

    const cwd = deps.cwd();
    const r = asRpcResult<{ session_id?: string }>(
      await deps.gw.request("prompt.background", {
        session_id: sid,
        text,
        ...(cwd ? { cwd } : {}),
      }),
    );
    const id = r?.session_id;

    return id
      ? info(`started ${short(id)} in the background`, id)
      : fail("invalid response: prompt.background");
  });

/** Text on another live session: a turn on it (queued behind its running turn), this client stays attached here. */
export const sendToSession = (
  deps: ViewActionDeps,
  row: ViewRow,
  text: string,
) =>
  guarded(async () => {
    const sid = deps.sid();

    if (!sid) {
      return NO_SESSION;
    }

    const r = asRpcResult<{ status?: string }>(
      await deps.gw.request("prompt.submit", {
        session_id: sid,
        target_session_id: row.id,
        text,
      }),
    );

    return info(
      r?.status === "queued"
        ? `queued behind the running turn of ${row.title}`
        : `sent to ${row.title}`,
    );
  });

/** Text on a sub-agent: guidance it applies at its next tool boundary. */
export const steerAgent = (deps: ViewActionDeps, row: ViewRow, text: string) =>
  guarded(async () => {
    const sid = deps.sid();

    if (!sid) {
      return NO_SESSION;
    }

    const r = await sendAgentSteer(deps.gw, sid, row.id, text);

    return r.accepted ? info(`steering sent to ${row.title}`) : fail(r.message);
  });

/** `/rename <title>` on a live or earlier session. */
export const renameSession = (
  deps: ViewActionDeps,
  row: ViewRow,
  title: string,
) =>
  guarded(async () => {
    await deps.gw.request("session.title", { session_id: row.id, title });

    if (row.current) {
      deps.onCurrentTitle?.(title);
    }

    return info(`renamed to ${title}`);
  });

/** `/delete` (after its y/n): removes the session's stored messages and files. Never the attached session. */
export const deleteSession = (deps: ViewActionDeps, row: ViewRow) =>
  guarded(async () => {
    if (row.current) {
      return fail("you are in this session");
    }

    await deps.gw.request("session.delete", { session_id: row.id });

    return info(`deleted ${row.title}`);
  });

/** `/close` on another live session: the gateway drops it unless it is busy, and says why when it keeps it. */
export const closeSession = (deps: ViewActionDeps, row: ViewRow) =>
  guarded(async () => {
    const r = asRpcResult<{ closed?: boolean; reason?: string }>(
      await deps.gw.request("session.close", { session_id: row.id }),
    );

    return r?.closed === false
      ? info(`${row.title} kept open: ${r.reason ?? "still in use"}`)
      : info(`closed ${row.title}`);
  });

/** A stop request (Ctrl+X, `/stop`): nothing to show on success, the row's state changes; a failure is a notice. */
export const stopNotice = (
  pending: Promise<unknown> | void,
): Promise<null | ViewNotice> | undefined =>
  pending?.then(
    () => null,
    (e: unknown) => fail(`stop failed: ${rpcErrorMessage(e)}`),
  );

/**
 * Settle an action for the view: an info notice clears the draft (it was sent), an error keeps it so it can be fixed
 * and sent again. A rejection becomes an error notice.
 */
export const settleViewAction = async (
  pending: Promise<ViewNotice>,
): Promise<ViewNotice> => {
  const n = await guarded(() => pending);

  if (n.tone === "info") {
    setAgentViewDraft("");
  }

  return n;
};
