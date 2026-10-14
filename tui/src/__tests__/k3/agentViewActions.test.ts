import { beforeEach, describe, expect, it, vi } from "vitest";

import { getOverlayState, patchOverlayState } from "../../app/overlayStore.js";
import { cancelAgentViewFromCtrlC } from "../../app/useInputHandlers.js";
import type { GatewayClient } from "../../gatewayClient.js";
import {
  closeSession,
  deleteSession,
  dispatchBackground,
  NO_SESSION,
  renameSession,
  sendToSession,
  settleViewAction,
  steerAgent,
  stopNotice,
  type ViewActionDeps,
} from "../../k3/agentViewActions.js";
import {
  getAgentViewDraft,
  setAgentViewDraft,
  type ViewRow,
} from "../../k3/agentViewStore.js";

const row = (over: Partial<ViewRow> = {}): ViewRow => ({
  activity: "",
  current: false,
  elapsedSeconds: null,
  group: "completed",
  id: "sess-other-1234",
  key: "session:sess-other-1234",
  kind: "session",
  state: "idle",
  title: "other work",
  ...over,
});

/** A gateway whose `request` answers from `reply` (a value, or a function of the method and params). */
const fakeGateway = (
  reply:
    ((method: string, params: Record<string, unknown>) => unknown) | unknown,
) => {
  const request = vi.fn(
    async (method: string, params: Record<string, unknown>) =>
      typeof reply === "function" ? reply(method, params) : reply,
  );

  return { gw: { request } as unknown as GatewayClient, request };
};

const depsFor = (
  gw: GatewayClient,
  sid: null | string = "sess-main",
  extra: Partial<ViewActionDeps> = {},
): ViewActionDeps => ({
  cwd: () => "/work/proj",
  gw,
  sid: () => sid,
  ...extra,
});

beforeEach(() => {
  setAgentViewDraft("");
  patchOverlayState({ agentView: false });
});

describe("dispatchBackground", () => {
  it("starts a background session from the attached one and names its id", async () => {
    const { gw, request } = fakeGateway({
      session_id: "abcdef123456",
      status: "started",
    });

    const n = await dispatchBackground(depsFor(gw), "fix the tests");

    expect(request).toHaveBeenCalledWith("prompt.background", {
      cwd: "/work/proj",
      session_id: "sess-main",
      text: "fix the tests",
    });
    expect(n).toEqual({
      sessionId: "abcdef123456",
      text: "started abcdef12 in the background",
      tone: "info",
    });
  });

  it("does not call the gateway before a session exists", async () => {
    const { gw, request } = fakeGateway({});

    expect(await dispatchBackground(depsFor(gw, null), "hi")).toEqual(
      NO_SESSION,
    );
    expect(NO_SESSION.text).toMatch(/no session yet/);
    expect(request).not.toHaveBeenCalled();
  });

  it("turns a gateway error into an error notice with its message", async () => {
    const { gw } = fakeGateway(() => {
      throw new Error("model not configured");
    });

    const n = await dispatchBackground(depsFor(gw), "hi");

    expect(n.tone).toBe("error");
    expect(n.text).toContain("model not configured");
  });
});

describe("sendToSession", () => {
  it("targets the row's session while staying on the attached one", async () => {
    const { gw, request } = fakeGateway({ status: "streaming" });

    const n = await sendToSession(depsFor(gw), row(), "carry on");

    expect(request).toHaveBeenCalledWith("prompt.submit", {
      session_id: "sess-main",
      target_session_id: "sess-other-1234",
      text: "carry on",
    });
    expect(n).toEqual({ text: "sent to other work", tone: "info" });
  });

  it("says when the text waits behind the running turn", async () => {
    const { gw } = fakeGateway({ status: "queued", turn_id: "" });

    expect((await sendToSession(depsFor(gw), row(), "next")).text).toBe(
      "queued behind the running turn of other work",
    );
  });

  it("reports a session that is no longer running", async () => {
    const { gw } = fakeGateway(() => {
      throw new Error(
        "session sess-other-1234 is not running; attach it first",
      );
    });

    const n = await sendToSession(depsFor(gw), row(), "next");

    expect(n.tone).toBe("error");
    expect(n.text).toContain("attach it first");
  });

  it("needs a session", async () => {
    const { gw, request } = fakeGateway({});

    expect(await sendToSession(depsFor(gw, null), row(), "x")).toEqual(
      NO_SESSION,
    );
    expect(request).not.toHaveBeenCalled();
  });
});

describe("steerAgent", () => {
  const agentRow = row({
    id: "sa-1",
    key: "agent:sa-1",
    kind: "agent",
    title: "explorer",
  });

  it("sends guidance to the sub-agent of the attached session", async () => {
    const { gw, request } = fakeGateway({ status: "queued" });

    const n = await steerAgent(depsFor(gw), agentRow, "look in src/");

    expect(request).toHaveBeenCalledWith("subagent.steer", {
      session_id: "sess-main",
      subagent_id: "sa-1",
      text: "look in src/",
    });
    expect(n).toEqual({ text: "steering sent to explorer", tone: "info" });
  });

  it("is an error when the agent no longer takes guidance", async () => {
    const { gw } = fakeGateway({ status: "not_queued" });

    expect((await steerAgent(depsFor(gw), agentRow, "x")).tone).toBe("error");
  });
});

describe("renameSession", () => {
  it("renames the row and updates the attached session's title", async () => {
    const { gw, request } = fakeGateway({ ok: true, title: "new name" });
    const onCurrentTitle = vi.fn();

    const n = await renameSession(
      depsFor(gw, "sess-main", { onCurrentTitle }),
      row({ current: true, id: "sess-main" }),
      "new name",
    );

    expect(request).toHaveBeenCalledWith("session.title", {
      session_id: "sess-main",
      title: "new name",
    });
    expect(onCurrentTitle).toHaveBeenCalledWith("new name");
    expect(n.tone).toBe("info");
  });

  it("leaves the attached title alone for another row", async () => {
    const { gw } = fakeGateway({ ok: true });
    const onCurrentTitle = vi.fn();

    await renameSession(
      depsFor(gw, "sess-main", { onCurrentTitle }),
      row(),
      "x",
    );

    expect(onCurrentTitle).not.toHaveBeenCalled();
  });
});

describe("deleteSession", () => {
  it("deletes another session", async () => {
    const { gw, request } = fakeGateway({ deleted: true });

    const n = await deleteSession(depsFor(gw), row({ kind: "past" }));

    expect(request).toHaveBeenCalledWith("session.delete", {
      session_id: "sess-other-1234",
    });
    expect(n).toEqual({ text: "deleted other work", tone: "info" });
  });

  it("refuses the attached session without calling the gateway", async () => {
    const { gw, request } = fakeGateway({});

    const n = await deleteSession(depsFor(gw), row({ current: true }));

    expect(n).toEqual({ text: "you are in this session", tone: "error" });
    expect(request).not.toHaveBeenCalled();
  });
});

describe("closeSession", () => {
  it("closes a live session", async () => {
    const { gw, request } = fakeGateway({ closed: true });

    expect(await closeSession(depsFor(gw), row())).toEqual({
      text: "closed other work",
      tone: "info",
    });
    expect(request).toHaveBeenCalledWith("session.close", {
      session_id: "sess-other-1234",
    });
  });

  it("shows the gateway's reason when it keeps the session", async () => {
    const { gw } = fakeGateway({ closed: false, reason: "still in use" });

    const n = await closeSession(depsFor(gw), row());

    expect(n.tone).toBe("info");
    expect(n.text).toContain("still in use");
  });
});

describe("stopNotice", () => {
  it("is null when the stop went through and a notice when it failed", async () => {
    expect(await stopNotice(Promise.resolve({}))).toBeNull();

    const n = await stopNotice(Promise.reject(new Error("no such agent")));

    expect(n?.tone).toBe("error");
    expect(n?.text).toMatch(/^stop failed: .*no such agent/);
    expect(stopNotice(undefined)).toBeUndefined();
  });
});

describe("settleViewAction", () => {
  it("clears the draft when the action succeeded", async () => {
    setAgentViewDraft("fix the tests");
    const { gw } = fakeGateway({ session_id: "abcdef123456" });

    const n = await settleViewAction(
      dispatchBackground(depsFor(gw), "fix the tests"),
    );

    expect(n.tone).toBe("info");
    expect(getAgentViewDraft()).toBe("");
  });

  it("keeps the draft and shows the message when it failed", async () => {
    setAgentViewDraft("carry on");
    const { gw } = fakeGateway(() => {
      throw new Error("gateway went away");
    });

    const n = await settleViewAction(
      sendToSession(depsFor(gw), row(), "carry on"),
    );

    expect(n.tone).toBe("error");
    expect(n.text).toContain("gateway went away");
    expect(getAgentViewDraft()).toBe("carry on");
  });

  it("keeps the draft when there is no session yet", async () => {
    setAgentViewDraft("hi");
    const { gw } = fakeGateway({});

    expect(
      await settleViewAction(dispatchBackground(depsFor(gw, null), "hi")),
    ).toEqual(NO_SESSION);
    expect(getAgentViewDraft()).toBe("hi");
  });

  it("turns a rejected action into an error notice and keeps the draft", async () => {
    setAgentViewDraft("x");

    const n = await settleViewAction(Promise.reject(new Error("boom")));

    expect(n).toMatchObject({ tone: "error" });
    expect(n.text).toContain("boom");
    expect(getAgentViewDraft()).toBe("x");
  });
});

describe("cancelAgentViewFromCtrlC", () => {
  it("clears a non-empty draft first and keeps the view open", () => {
    patchOverlayState({ agentView: true });
    setAgentViewDraft("half typed");

    cancelAgentViewFromCtrlC();

    expect(getAgentViewDraft()).toBe("");
    expect(getOverlayState().agentView).toBe(true);

    cancelAgentViewFromCtrlC();

    expect(getOverlayState().agentView).toBe(false);
  });
});
