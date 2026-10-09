import { PassThrough } from "stream";

import { renderSync, useInput } from "@k3code/ink";
import { stripAnsi } from "@k3code/shared/ansi";
import React, { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GatewayProvider } from "../app/gatewayContext.js";
import type {
  AppLayoutProps,
  ComposerActions,
  GatewayServices,
  InputHandlerContext,
  OverlayState,
  UiState,
} from "../app/interfaces.js";
import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import { patchTurnState, resetTurnState } from "../app/turnStore.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import { useInputHandlers } from "../app/useInputHandlers.js";
import { useSessionLifecycle } from "../app/useSessionLifecycle.js";
import { StatusRule } from "../components/appChrome.js";
import { AppLayout } from "../components/appLayout.js";
import type { GatewayClient } from "../gatewayClient.js";
import { AGENT_VIEW_HINT } from "../k3/agentView.js";
import {
  $stripNav,
  $stripSessions,
  IDLE_NAV,
  setStripHandlers,
} from "../k3/agentStripStore.js";
import { DEFAULT_THEME } from "../theme.js";
import type { SubagentProgress } from "../types.js";
import { waitFor } from "./waitFor.js";

type StatusRuleProps = React.ComponentProps<typeof StatusRule>;
type IntervalSpy = ReturnType<
  typeof vi.spyOn<typeof globalThis, "setInterval">
>;

// Fixed wall clock so the rendered elapsed read-outs are exact strings rather
// than whatever the machine's clock happens to produce mid-test.
const T0 = 1_800_000_000_000;

const mounted: Array<() => void> = [];

/**
 * Mount a real StatusRule through Ink so the leaf components' effects — and
 * therefore their `setInterval` calls — actually run.  The existing
 * appChromeStatusRule tests invoke `StatusRule(...)` as a plain function,
 * which only builds the element tree and never mounts FaceTicker /
 * SessionDuration / IdleSince, so it cannot observe timer behaviour.
 *
 * Teardown is registered up front so a failing assertion still unmounts the
 * tree — a leaked instance would keep re-arming timers into the next test.
 */
const mountTree = (tree: React.ReactElement, { interactive = false } = {}) => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();

  let output = "";

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 20 });
  // PromptZone's prompts call `useInput`, which needs raw mode; without it Ink
  // swaps the whole tree for an error panel and stops updating.
  Object.assign(
    stdin,
    interactive
      ? { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} }
      : { isTTY: false },
  );
  Object.assign(stderr, { isTTY: false });
  stdout.on("data", (chunk) => {
    output += chunk.toString();
  });

  const instance = renderSync(tree, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream,
  });

  mounted.push(() => {
    instance.unmount();
    instance.cleanup();
  });

  return {
    /** Drop frames rendered so far so `output()` reads only what comes next. */
    clear: () => {
      output = "";
    },
    output: () => stripAnsi(output),
    /** Type into the tree's stdin (interactive mounts only). */
    press: (keys: string) => stdin.write(keys),
  };
};

const mount = (props: StatusRuleProps) => mountTree(<StatusRule {...props} />);

const idleProps: StatusRuleProps = {
  bgCount: 0,
  busy: false,
  cols: 120,
  cwdLabel: "~/repo",
  lastTurnEndedAt: T0 - 5_000,
  liveSessionCount: 0,
  model: "opus-4.8",
  sessionStartedAt: T0 - 60_000,
  status: "ready",
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: {
    context_max: 200_000,
    context_percent: 25,
    context_used: 50_000,
    total: 50_000,
  },
};

// Busy swaps the idle read-out for the FaceTicker, which owns the glyph +
// verb + elapsed-clock trio.
const busyProps: StatusRuleProps = {
  ...idleProps,
  busy: true,
  indicatorStyle: "kaomoji",
  lastTurnEndedAt: null,
  turnStartedAt: T0 - 30_000,
};

/** Delays of every interval armed while the spy was installed. */
const armedDelays = (spy: IntervalSpy) => spy.mock.calls.map((call) => call[1]);

const oneSecondTimers = (spy: IntervalSpy) =>
  armedDelays(spy).filter((delay) => delay === 1000).length;

/** The handlers of every 1s clock armed so far — `() => setNow(Date.now())`. */
const oneSecondTicks = (spy: IntervalSpy) =>
  spy.mock.calls
    .filter((call) => call[1] === 1000)
    .map((call) => call[0] as () => void);

// ── AppLayout harness ────────────────────────────────────────────────
//
// teknium1's review of this file was right that mounting StatusRule alone
// proves the store pauses timers but NOT that the overlay in question covers
// the status rule.  These props render the real AppLayout so the rule sits in
// its true position relative to PromptZone / FloatingOverlays / the widget
// slot, and the assertions can read what is actually on screen.

const gatewayStub = {
  gw: {
    request: () => new Promise<never>(() => {}),
    send: () => {},
  } as unknown as GatewayClient,
  rpc: (() => new Promise<never>(() => {})) as never,
};

const layoutProps: AppLayoutProps = {
  actions: {
    activateLiveSession: () => {},
    answerApproval: () => {},
    answerClarify: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: () => Promise.resolve(null),
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {},
  },
  composer: {
    cols: 120,
    compIdx: 0,
    completions: [],
    empty: true,
    handleTextPaste: () => null,
    input: "",
    inputBuf: [],
    pagerPageSize: 10,
    queueEditIdx: null,
    queuedDisplay: [],
    submit: () => {},
    updateInput: () => {},
  },
  mouseTracking: "off",
  progress: { showProgressArea: false },
  status: {
    cwdLabel: "~/repo",
    goodVibesTick: 0,
    lastTurnEndedAt: T0 - 5_000,
    sessionStartedAt: T0 - 60_000,
    sessionTitle: "",
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: "",
    turnStartedAt: null,
  },
  transcript: {
    historyItems: [],
    scrollRef: { current: null },
    virtualHistory: {
      bottomSpacer: 0,
      end: 0,
      measureRef: () => () => {},
      offsets: [],
      start: 0,
      topSpacer: 0,
    },
    virtualRows: [],
  },
};

/** Mount the real AppLayout with the given overlay + ui state applied first. */
const mountLayout = (
  overlay: Partial<OverlayState> = {},
  ui: Partial<UiState> = {},
  actions: Partial<AppLayoutProps["actions"]> = {},
  {
    beside = null,
    gateway = gatewayStub,
  }: { beside?: React.ReactNode; gateway?: GatewayServices } = {},
) => {
  patchUiState({ sessionTitle: "test", sid: "sid-1", status: "ready", ...ui });
  patchOverlayState(overlay);

  return mountTree(
    <GatewayProvider value={gateway}>
      <AppLayout
        {...layoutProps}
        actions={{ ...layoutProps.actions, ...actions }}
      />
      {beside}
    </GatewayProvider>,
    { interactive: true },
  );
};

// Give React's scheduler a turn so a store-driven re-render (and the effect
// re-arm that follows it) lands before we assert.
const flush = () => new Promise((resolve) => setTimeout(resolve, 20));

let intervalSpy: IntervalSpy;
let nowSpy: ReturnType<typeof vi.spyOn<typeof Date, "now">>;

beforeEach(() => {
  resetOverlayState();
  resetUiState();
  nowSpy = vi.spyOn(Date, "now").mockReturnValue(T0);
  intervalSpy = vi.spyOn(globalThis, "setInterval");
});

afterEach(() => {
  while (mounted.length > 0) {
    mounted.pop()!();
  }

  intervalSpy.mockRestore();
  nowSpy.mockRestore();
  resetOverlayState();
  resetUiState();
});

describe("status-chrome timers under an occluding overlay", () => {
  it("arms the one-second SessionDuration + IdleSince clocks when nothing covers the rule", () => {
    mount(idleProps);

    expect(oneSecondTimers(intervalSpy)).toBe(2);
  });

  it("arms no timer at all when an occluding overlay is already open", () => {
    patchOverlayState({ modelPicker: true });

    mount(idleProps);

    expect(oneSecondTimers(intervalSpy)).toBe(0);
  });

  it("arms the FaceTicker glyph/verb/clock trio mid-turn when nothing covers the rule", () => {
    mount(busyProps);

    // kaomoji cadence for the glyph + verb rotation, plus the elapsed clock.
    expect(armedDelays(intervalSpy)).toContain(2500);
    expect(oneSecondTimers(intervalSpy)).toBeGreaterThan(0);
  });

  it("freezes the FaceTicker verb on compacting and skips verb rotation (#97239)", () => {
    const { output } = mount({ ...busyProps, compacting: true });

    expect(output()).toContain("compacting");
    // Glyph still ticks at the kaomoji cadence; the rotating-verb timer does not.
    expect(
      armedDelays(intervalSpy).filter((delay) => delay === 2500),
    ).toHaveLength(1);
    expect(oneSecondTimers(intervalSpy)).toBeGreaterThan(0);
  });

  it("arms no FaceTicker timer mid-turn while the modal widget slot is open", () => {
    patchOverlayState({ widget: { appId: "demo", state: null } });

    mount(busyProps);

    expect(armedDelays(intervalSpy)).not.toContain(2500);
    expect(oneSecondTimers(intervalSpy)).toBe(0);
  });

  it("keeps the FaceTicker running mid-turn under a flow-layout sudo prompt", () => {
    // `sudo` is in `$isBlocked` but renders in PromptZone's normal flow, so it
    // pushes the rule down rather than covering it — the trio must keep going.
    patchOverlayState({ sudo: { requestId: "sudo-1" } });

    mount(busyProps);

    expect(armedDelays(intervalSpy)).toContain(2500);
    expect(oneSecondTimers(intervalSpy)).toBeGreaterThan(0);
  });

  it("keeps the clocks running when a floating overlay cannot reach a bottom status rule", () => {
    // FloatingOverlays is `position="absolute" bottom="100%"` inside
    // ComposerPane's relative Box, so it grows UPWARD: it covers the `at="top"`
    // rule and never the `at="bottom"` one.
    patchUiState({ statusBar: "bottom" });
    patchOverlayState({ modelPicker: true });

    mount(idleProps);

    expect(oneSecondTimers(intervalSpy)).toBe(2);
  });

  it("re-syncs the elapsed read-outs from the wall clock on reveal instead of resuming stale", async () => {
    // Regression guard for the naive fix: an early `return` that pauses the
    // interval but never re-seeds `now` leaves SessionDuration and IdleSince
    // frozen at the instant the overlay opened.
    patchOverlayState({ sessions: true });

    const rule = mount(idleProps);

    expect(rule.output()).toContain("1m 0s");
    expect(rule.output()).toContain("✓ 5s");

    // Five minutes of wall clock elapse while the overlay covers the rule.
    nowSpy.mockReturnValue(T0 + 300_000);
    rule.clear();
    resetOverlayState();
    // Poll for the reveal frame instead of a fixed tick: under CI load the
    // store-driven re-render can land well after one 20ms scheduler turn.
    await waitFor(() => expect(rule.output()).toContain("6m 0s"), {
      interval: 10,
      timeout: 5_000,
    });

    const resumed = rule.output();

    // Caught up to real elapsed time, not stuck on the pre-overlay values.
    expect(resumed).toContain("6m 0s");
    expect(resumed).toContain("✓ 5m 5s");
    expect(resumed).not.toContain("1m 0s");

    // …and the clocks are running again.
    expect(oneSecondTimers(intervalSpy)).toBe(2);
  });

  it("tears the clocks down when an overlay opens over an already-running status rule", async () => {
    mount(idleProps);

    // Handles of the two live 1-second clocks (SessionDuration + IdleSince).
    const clocks = intervalSpy.mock.results
      .filter((_result, i) => intervalSpy.mock.calls[i]?.[1] === 1000)
      .map((result) => result.value as ReturnType<typeof setInterval>);

    expect(clocks).toHaveLength(2);

    const clearSpy = vi.spyOn(globalThis, "clearInterval");

    patchOverlayState({ pluginsHub: true });
    await flush();

    // Each running clock is cleared as the overlay goes up …
    for (const handle of clocks) {
      expect(clearSpy).toHaveBeenCalledWith(handle);
    }

    // … and the occluded re-run arms no replacement (still just the original two).
    expect(oneSecondTimers(intervalSpy)).toBe(2);

    clearSpy.mockRestore();
  });
});

// teknium1's review of #12463 called out that its test asserted on a `picker`
// overlay state that no longer exists.  Pin the gate to fields the current
// OverlayState actually carries so a rename breaks this file loudly.
describe("status-chrome timers track the current overlay model", () => {
  // Everything that genuinely paints over the rule: the modal widget slot,
  // plus the FloatingOverlays set (with the rule at its default `top`).
  const occluding: Array<[string, Partial<OverlayState>]> = [
    ["modelPicker", { modelPicker: true }],
    ["pager", { pager: { lines: ["a"], offset: 0 } }],
    ["pluginsHub", { pluginsHub: true }],
    ["sessions", { sessions: true }],
    ["skillsHub", { skillsHub: true }],
    ["widget", { widget: { appId: "demo", state: null } }],
  ];

  // In `$isBlocked` but NOT occluding.  `agents` / `agentView` / `journey`
  // unmount the whole ComposerPane subtree, so React's effect cleanup already
  // stops the clocks and gating on them would be dead code; the rest are
  // PromptZone states that render in normal flow and push the rule down
  // without covering it.
  const nonOccluding: Array<[string, Partial<OverlayState>]> = [
    ["agents", { agents: true }],
    ["agentView", { agentView: true }],
    [
      "approval",
      {
        approval: {
          command: "ls",
          requestId: "a-1",
        } as OverlayState["approval"],
      },
    ],
    [
      "clarify",
      {
        clarify: {
          question: "which?",
          requestId: "c-1",
        } as OverlayState["clarify"],
      },
    ],
    [
      "confirm",
      {
        confirm: {
          onConfirm: () => {},
          prompt: "sure?",
        } as OverlayState["confirm"],
      },
    ],
    ["journey", { journey: true }],
    [
      "secret",
      {
        secret: { envVar: "TOKEN", prompt: "token?" } as OverlayState["secret"],
      },
    ],
    ["sudo", { sudo: { requestId: "sudo-1" } as OverlayState["sudo"] }],
  ];

  it.each(occluding)(
    "pauses the status clocks while %s covers the rule",
    (_name, patch) => {
      patchOverlayState(patch);

      mount(idleProps);

      expect(oneSecondTimers(intervalSpy)).toBe(0);
    },
  );

  it.each(nonOccluding)(
    "keeps the status clocks running while %s is open",
    (_name, patch) => {
      patchOverlayState(patch);

      mount(idleProps);

      expect(oneSecondTimers(intervalSpy)).toBe(2);
    },
  );

  it("keeps the clocks running for the non-occluding ambient dock", () => {
    // `ambient` is a glanceable in-flow dock that reserves its own rows and
    // doesn't cover the status rule, so pausing there would be a regression.
    patchOverlayState({ ambient: [{ appId: "clock", state: null }] });

    mount(idleProps);

    expect(oneSecondTimers(intervalSpy)).toBe(2);
  });
});

// The visibility gate teknium1 asked for: mount the REAL AppLayout so the
// status rule sits in its true position relative to PromptZone (normal flow,
// above ComposerPane) and FloatingOverlays (absolute, growing upward), then
// assert on what is actually on screen rather than on the store alone.
describe("AppLayout status-rule visibility", () => {
  it("keeps the status rule on screen AND its clock advancing under a flow-layout approval prompt", async () => {
    const layout = mountLayout({
      approval: {
        command: "rm -rf /",
        requestId: "a-1",
      } as OverlayState["approval"],
    });

    await flush();

    // The rule is genuinely rendered — the approval prompt pushed it, it did
    // not cover it — so freezing its clock would freeze something visible.
    expect(layout.output()).toContain("~/repo");
    expect(layout.output()).toContain("1m 0s");
    expect(oneSecondTimers(intervalSpy)).toBe(2);

    // …and it really advances: drive the armed 1s handlers forward.
    nowSpy.mockReturnValue(T0 + 30_000);

    for (const tick of oneSecondTicks(intervalSpy)) {
      tick();
    }

    await flush();
    await flush();

    expect(layout.output()).toContain("1m 30s");
  });

  it("arms no clock under a floating model picker while the rule is at the top", async () => {
    mountLayout({ modelPicker: true }, { statusBar: "top" });

    await flush();

    expect(oneSecondTimers(intervalSpy)).toBe(0);
  });

  it("keeps the clocks armed under a floating model picker while the rule is at the bottom", async () => {
    mountLayout({ modelPicker: true }, { statusBar: "bottom" });

    await flush();

    expect(oneSecondTimers(intervalSpy)).toBe(2);
  });

  it("swaps the transcript and composer for the full-screen agent view", async () => {
    const layout = mountLayout({ agentView: true });

    await flush();

    expect(layout.output()).toContain(AGENT_VIEW_HINT);
    // the composer (and the status rule inside it) unmounts with the view up
    expect(layout.output()).not.toContain("~/repo");
    expect(oneSecondTimers(intervalSpy)).toBe(0);
  });
});

describe("AppLayout agent view: leaving the startup session", () => {
  afterEach(() => {
    $stripSessions.set([]);
  });

  // `k3code agents` opens the view on gateway.ready, before the startup session exists; it arrives afterwards.
  it.each([
    ["n", "newLiveSession", ["s1"]],
    ["\r", "activateLiveSession", ["s2", "s1"]],
  ] as const)(
    "%j closes the empty session forged after the view opened",
    async (key, method, args) => {
      const spy = vi.fn();
      const layout = mountLayout(
        { agentView: true },
        { sid: null },
        {
          [method]: spy,
        },
      );

      await flush();
      patchUiState({ sid: "s1" });
      $stripSessions.set([
        {
          current: true,
          id: "s1",
          message_count: 0,
          status: "idle",
          title: "startup",
        },
        { id: "s2", status: "working", title: "busy one" },
      ]);
      await waitFor(() => expect(layout.output()).toContain("busy one"));

      // s2 (working) is listed first, so ⏎ attaches to it.
      layout.press(key);

      await waitFor(() => expect(spy).toHaveBeenCalled());
      expect(spy).toHaveBeenCalledWith(...args);
    },
  );
});

// The real session lifecycle behind the layout, as useMainApp wires it: what reaches the gateway, not which action
// the pane called.
describe("AppLayout agent view: the gateway closes the session left behind", () => {
  afterEach(() => {
    $stripSessions.set([]);
  });

  const CLOSE_S1 = [
    "session.close",
    { disposable_only: true, session_id: "s1" },
  ];

  const mountWithLifecycle = (sid: null | string) => {
    const request = vi.fn((method: string, params?: Record<string, unknown>) =>
      Promise.resolve<unknown>(
        method === "session.list"
          ? { sessions: [] }
          : method === "session.activate"
            ? {
                messages: [],
                running: false,
                session_id: params?.session_id,
                status: "idle",
              }
            : method === "session.close"
              ? { closed: true }
              : null,
      ),
    );
    const rpc = vi.fn((method: string) =>
      Promise.resolve<unknown>(
        method === "setup.status"
          ? { provider_configured: true }
          : method === "session.create"
            ? { session_id: "s-new" }
            : null,
      ),
    );
    const gateway = {
      gw: { request, send: () => {} } as unknown as GatewayClient,
      rpc: rpc as unknown as GatewayServices["rpc"],
    };

    const WithLifecycle = () => {
      const session = useSessionLifecycle({
        colsRef: { current: 120 },
        composerActions: {
          setComposerTokens: () => {},
        } as unknown as ComposerActions,
        gw: gateway.gw,
        panel: () => {},
        rpc: gateway.rpc,
        scrollRef: { current: null },
        setHistoryItems: () => {},
        setLastUserMsg: () => {},
        setSessionStartedAt: () => {},
        setStickyPrompt: () => {},
        sys: () => {},
      });

      return (
        <AppLayout
          {...layoutProps}
          actions={{
            ...layoutProps.actions,
            activateLiveSession: session.activateLiveSession,
            newLiveSession: (dropSid?: string) =>
              void session.newLiveSession(undefined, undefined, dropSid),
          }}
        />
      );
    };

    patchUiState({ sessionTitle: "test", sid, status: "ready" });
    patchOverlayState({ agentView: true });

    const layout = mountTree(
      <GatewayProvider value={gateway}>
        <WithLifecycle />
      </GatewayProvider>,
      { interactive: true },
    );

    return { layout, request, rpc };
  };

  const closeCalls = (request: ReturnType<typeof vi.fn>) =>
    request.mock.calls.filter(([method]) => method === "session.close");

  it("n with no strip rows closes the origin session once the new one is attached", async () => {
    const { layout, request } = mountWithLifecycle("s1");

    await waitFor(() =>
      expect(layout.output()).toContain("No sessions yet - press n"),
    );
    layout.press("n");

    await waitFor(() => expect(request).toHaveBeenCalledWith(...CLOSE_S1));
    expect(getUiState().sid).toBe("s-new");
    expect(closeCalls(request)).toHaveLength(1);
  });

  it("⏎ on a live row closes the origin session even though the strip does not list it", async () => {
    $stripSessions.set([{ id: "s2", status: "working", title: "busy one" }]);

    const { layout, request } = mountWithLifecycle("s1");

    await waitFor(() => expect(layout.output()).toContain("› ◐ busy one"));
    layout.press("\r");

    await waitFor(() => expect(request).toHaveBeenCalledWith(...CLOSE_S1));
    expect(request).toHaveBeenCalledWith("session.activate", {
      session_id: "s2",
    });
    expect(getUiState().sid).toBe("s2");
    expect(closeCalls(request)).toHaveLength(1);
  });

  it("closes nothing when there was no origin session", async () => {
    const { layout, request, rpc } = mountWithLifecycle(null);

    await waitFor(() =>
      expect(layout.output()).toContain("No sessions yet - press n"),
    );
    layout.press("n");

    // The switch has landed (the close would be sent right after it), so an absent close is a real absence.
    await waitFor(() => expect(getUiState().sid).toBe("s-new"));
    expect(rpc).toHaveBeenCalledWith("session.create", expect.anything());
    await flush();

    expect(closeCalls(request)).toHaveLength(0);
  });

  it("closes nothing when ⏎ re-attaches the session the view was opened from", async () => {
    $stripSessions.set([
      { current: true, id: "s1", status: "working", title: "this one" },
    ]);

    const { layout, request } = mountWithLifecycle("s1");

    await waitFor(() => expect(layout.output()).toContain("› ◐ this one"));
    layout.press("\r");

    // ⏎ on the current row just leaves the view: no switch, so nothing to close.
    await waitFor(() => expect(getOverlayState().agentView).toBe(false));
    await flush();

    expect(
      request.mock.calls.filter(([method]) => method === "session.activate"),
    ).toHaveLength(0);
    expect(closeCalls(request)).toHaveLength(0);
    expect(getUiState().sid).toBe("s1");
  });
});

// ── useInputHandlers harness ─────────────────────────────────────────
//
// The real global key handler, mounted through Ink beside AppLayout as useMainApp does, with a composer whose state
// lives in React so a cleared draft shows up in `composer.input`.

const LEFT = "\x1b[D";
const ESC = "\x1b";

type ComposerProbe = { clearIn: ReturnType<typeof vi.fn>; input: string };

const InputHarness = ({
  historyIdx = null,
  initialInput = "",
  probe,
}: {
  historyIdx?: null | number;
  initialInput?: string;
  probe: ComposerProbe;
}) => {
  const [input, setInput] = useState(initialInput);

  probe.input = input;

  const composerActions: Partial<ComposerActions> = {
    clearIn: () => {
      probe.clearIn();
      setInput("");
    },
    pushHistory: () => {},
    setHistoryIdx: () => {},
    setInput,
    setQueueEdit: () => {},
  };
  const ctx: InputHandlerContext = {
    actions: {
      answerClarify: () => {},
      appendMessage: () => {},
      die: () => {},
      dispatchSubmission: () => {},
      guardBusySessionSwitch: () => false,
      newSession: () => {},
      sys: () => {},
    },
    composer: {
      actions: composerActions as ComposerActions,
      refs: {
        historyDraftRef: { current: "" },
        historyRef: { current: [""] },
        queueEditRef: { current: null },
        queueRef: { current: [] },
        submitRef: { current: () => {} },
        tokensRef: { current: [] },
      },
      state: {
        compIdx: 0,
        compReplace: 0,
        completions: [],
        historyIdx,
        input,
        inputBuf: [],
        queueEditIdx: null,
        queuedDisplay: [],
        tokens: [],
      },
    },
    gateway: gatewayStub,
    terminal: {
      hasSelection: false,
      scrollRef: { current: null },
      scrollWithSelection: () => {},
      selection: {} as InputHandlerContext["terminal"]["selection"],
    },
    wheelStep: 1,
  };

  useInputHandlers(ctx);

  return null;
};

const newProbe = (): ComposerProbe => ({ clearIn: vi.fn(), input: "" });

// A key nothing in the layout handles: pressed after the key under test, its arrival proves that key was dispatched
// (stdin is read in order), so an absence asserted afterwards is real. Counted only unmodified, so an Esc that merged
// with it into Alt+F12 never registers and the wait fails instead of passing falsely.
const F12 = "\x1b[24~";

const SentinelProbe = ({ hits }: { hits: { f12: number } }) => {
  useInput((_ch, key, event) => {
    if (event.keypress.name === "f12" && !key.meta) {
      hits.f12++;
    }
  });

  return null;
};

/** Press `key`, then the F12 sentinel, and wait until the sentinel has been dispatched. */
const pressThenSentinel = async (
  layout: { press: (keys: string) => void },
  hits: { f12: number },
  key: string,
) => {
  const before = hits.f12;

  layout.press(key);
  layout.press(F12);
  await waitFor(() => expect(hits.f12).toBe(before + 1));
};

describe("useInputHandlers: ← opens the agent view from an idle, empty prompt", () => {
  afterEach(() => {
    $stripNav.set(IDLE_NAV);
    $stripSessions.set([]);
  });

  it("opens the view on a plain ← with an empty composer", async () => {
    const probe = newProbe();
    const layout = mountLayout(
      {},
      {},
      {},
      {
        beside: <InputHarness probe={probe} />,
      },
    );

    await flush();
    layout.press(LEFT);

    await waitFor(() => expect(getOverlayState().agentView).toBe(true));
    await waitFor(() => expect(layout.output()).toContain(AGENT_VIEW_HINT));
  });

  it.each<
    [
      string,
      {
        harness?: Partial<React.ComponentProps<typeof InputHarness>>;
        key?: string;
        overlay?: Partial<OverlayState>;
        strip?: boolean;
      },
    ]
  >([
    ["with text typed", { harness: { initialInput: "draft" } }],
    ["during a history walk", { harness: { historyIdx: 0 } }],
    ["with the agent strip focused", { strip: true }],
    ["with Shift held", { key: "\x1b[1;2D" }],
    ["with Alt held", { key: "\x1b[1;3D" }],
    [
      "while an approval prompt is open",
      {
        overlay: {
          approval: {
            command: "ls",
            requestId: "a-1",
          } as OverlayState["approval"],
        },
      },
    ],
  ])("does not open %s", async (_name, { harness, key, overlay, strip }) => {
    if (strip) {
      // The strip drops focus as soon as it has no rows, so give it one.
      $stripSessions.set([{ id: "bg-1", status: "working", title: "bg" }]);
      $stripNav.set({ ...IDLE_NAV, focused: true });
    }

    const probe = newProbe();
    const hits = { f12: 0 };
    const layout = mountLayout(
      overlay,
      {},
      {},
      {
        beside: (
          <>
            <InputHarness probe={probe} {...harness} />
            <SentinelProbe hits={hits} />
          </>
        ),
      },
    );

    await flush();
    await pressThenSentinel(layout, hits, key ?? LEFT);

    expect(getOverlayState().agentView).toBe(false);

    if (strip) {
      expect($stripNav.get().focused).toBe(true);
    }
  });
});

describe("useInputHandlers: Esc that closes the agent view does not count toward double-Esc", () => {
  it("keeps the draft when a second Esc follows the one that closed the view", async () => {
    const probe = newProbe();
    const hits = { f12: 0 };
    const layout = mountLayout(
      { agentView: true },
      {},
      {},
      {
        beside: (
          <>
            <InputHarness initialInput="keep me" probe={probe} />
            <SentinelProbe hits={hits} />
          </>
        ),
      },
    );

    await waitFor(() => expect(layout.output()).toContain(AGENT_VIEW_HINT));

    // Date.now is pinned to T0, so both presses sit well inside DOUBLE_ESC_MS.
    layout.press(ESC);
    await waitFor(() => expect(getOverlayState().agentView).toBe(false));
    await flush();

    layout.press(ESC);
    // A lone Esc is only emitted after Ink's 50 ms escape-sequence flush; the sentinel must arrive after that, or the
    // two merge into Alt+F12 (which the probe ignores, so the wait below would fail rather than pass falsely).
    await flush();
    await flush();
    await flush();
    await pressThenSentinel(layout, hits, "");

    expect(probe.clearIn).not.toHaveBeenCalled();
    expect(probe.input).toBe("keep me");
  });
});

describe("AppLayout agent view: ⏎ routes by row kind", () => {
  const helper: SubagentProgress = {
    depth: 0,
    goal: "helper agent",
    id: "ag-1",
    index: 0,
    notes: [],
    parentId: null,
    startedAt: T0 - 5_000,
    status: "running",
    taskCount: 1,
    thinking: [],
    toolCount: 0,
    tools: [],
  } as SubagentProgress;

  afterEach(() => {
    $stripSessions.set([]);
    setStripHandlers(null);
    resetTurnState();
  });

  /** A gateway whose `session.list` returns one earlier session of this project. */
  const pastGateway: GatewayServices = {
    gw: {
      request: () =>
        Promise.resolve({
          sessions: [
            {
              cwd: "/work/proj",
              id: "past-1",
              message_count: 4,
              started_at: T0 / 1000 - 86_400,
              title: "older work",
            },
          ],
        }),
      send: () => {},
    } as unknown as GatewayClient,
    rpc: gatewayStub.rpc,
  };

  const info = {
    cwd: "/work/proj",
    model: "test",
    skills: {},
    tools: {},
  } as UiState["info"];

  // Each case leaves only the target row in the list, so it is the selected one.
  it("attaches a live session row with activateLiveSession", async () => {
    const activateLiveSession = vi.fn();
    const resumeById = vi.fn();

    $stripSessions.set([
      { id: "live-1", message_count: 3, status: "working", title: "busy one" },
    ]);

    const layout = mountLayout(
      { agentView: true },
      { info, sid: "s1" },
      { activateLiveSession, resumeById },
    );

    await waitFor(() => expect(layout.output()).toContain("› ◐ busy one"));
    layout.press("\r");

    await waitFor(() => expect(activateLiveSession).toHaveBeenCalled());
    // The origin goes along even though it is not in the live list: the gateway decides whether it is disposable.
    expect(activateLiveSession).toHaveBeenCalledWith("live-1", "s1");
    expect(resumeById).not.toHaveBeenCalled();
    expect(getOverlayState().agentView).toBe(false);
  });

  it("resumes an earlier session row with resumeById", async () => {
    const activateLiveSession = vi.fn();
    const resumeById = vi.fn();
    const layout = mountLayout(
      { agentView: true },
      { info, sid: "s1" },
      { activateLiveSession, resumeById },
      { gateway: pastGateway },
    );

    await waitFor(() => expect(layout.output()).toContain("› · older work"));
    // The row is painted before the pane's input handler is re-armed with it; a ⏎ in that gap is dropped, and
    // nothing observable marks the end of the gap.
    await flush();
    layout.press("\r");

    await waitFor(() => expect(resumeById).toHaveBeenCalled());
    expect(resumeById).toHaveBeenCalledWith("past-1");
    expect(activateLiveSession).not.toHaveBeenCalled();
    expect(getOverlayState().agentView).toBe(false);
  });

  it("opens an in-turn agent row through the strip's activate handler", async () => {
    const activateLiveSession = vi.fn();
    const resumeById = vi.fn();
    const stripActivate = vi.fn();

    setStripHandlers({ activate: stripActivate, stop: () => {} });
    patchTurnState({ subagents: [helper] });

    const layout = mountLayout(
      { agentView: true },
      { info, sid: "s1" },
      { activateLiveSession, resumeById },
    );

    await waitFor(() => expect(layout.output()).toContain("› ◐ helper agent"));
    layout.press("\r");

    await waitFor(() => expect(stripActivate).toHaveBeenCalled());
    expect(stripActivate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ag-1", kind: "agent" }),
    );
    expect(activateLiveSession).not.toHaveBeenCalled();
    expect(resumeById).not.toHaveBeenCalled();
    expect(getOverlayState().agentView).toBe(false);
  });
});
