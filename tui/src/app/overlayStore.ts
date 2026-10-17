import { atom, computed } from "nanostores";

import type { OverlayState } from "./interfaces.js";
import { $uiState } from "./uiStore.js";

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsInitialHistoryIndex: 0,
  agentsInitialAgentId: null,
  agentView: false,
  approval: null,
  clarify: null,
  confirm: null,
  ambient: [],
  widget: null,
  modelPicker: false,
  pager: null,
  providerPicker: false,
  permissionsPicker: false,
  secret: null,
  sessions: false,
  sudo: null,
  settingsPanel: false,
  tunePicker: false,
});

export const $overlayState = atom<OverlayState>(buildOverlayState());

export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    agentView,
    approval,
    clarify,
    confirm,
    modelPicker,
    pager,
    permissionsPicker,
    providerPicker,
    secret,
    sessions,
    settingsPanel,
    sudo,
    tunePicker,
    widget,
  }) =>
    Boolean(
      agents ||
      agentView ||
      approval ||
      clarify ||
      confirm ||
      modelPicker ||
      pager ||
      permissionsPicker ||
      providerPicker ||
      secret ||
      sessions ||
      settingsPanel ||
      sudo ||
      tunePicker ||
      widget,
    ),
);

/**
 * Does an open overlay actually PAINT OVER the status rule?
 *
 * Deliberately NOT `$isBlocked`.  That aggregate answers a different
 * question — "is text input suspended" — and `appLayout` uses it only to hide
 * the input rows (`appLayout.tsx:384`).  `StatusRulePane` is rendered OUTSIDE
 * that guard (`appLayout.tsx:365` for `at="top"`, `:449` for `at="bottom"`),
 * so most `$isBlocked` fields leave the rule fully visible.
 *
 * Occluding (included here):
 *
 * - `widget` — the modal widget slot renders at viewport level
 *   (`ActiveWidgetSlot`, `sdk/host.tsx:209`, outside the ComposerPane
 *   subtree) so it can anchor the full-screen absolute `Overlay`
 *   (`components/overlay.tsx`) against the whole terminal.
 * - The FloatingOverlays set — `modelPicker`, `tunePicker`, `providerPicker`,
 *   `pager`, `sessions` — but ONLY when the rule sits at
 *   the top.  That panel is `position="absolute" bottom="100%"` inside
 *   ComposerPane's relative Box (`appOverlays.tsx:387`), so it grows UPWARD
 *   over the `at="top"` rule and never reaches the `at="bottom"` one.
 *
 * NOT occluding (deliberately excluded):
 *
 * - The PromptZone flow states — `approval`,
 *   `confirm`, `clarify`, `sudo`, `secret` (`appOverlays.tsx:58-162`).  They
 *   render in NORMAL FLOW above ComposerPane (`appLayout.tsx:553-568`): they
 *   push content down, they do not cover it.  The rule stays on screen and
 *   its clock must keep running.
 * - `agents` and `agentView`.  They unmount the entire ComposerPane subtree
 *   (`appLayout.tsx:553`), so `StatusRule` unmounts with it and React's own
 *   effect cleanup clears the intervals.  Gating on them would be dead code.
 * - `ambient` — a glanceable in-flow dock that reserves its own rows.
 * - Composer completions.  They share the FloatingOverlays grid and do
 *   occlude, but they are a render prop rather than store state and they
 *   change on every keystroke; re-arming a 1s interval per character would
 *   restart the countdown each time and starve the tick outright — strictly
 *   worse than the churn this gate removes.
 *
 * `statusBar: 'off'` needs no branch: `StatusRulePane` returns null for both
 * slots, so the timers are never mounted in the first place.
 */
/**
 * True when any floating overlay PANEL is open (widget overlays and inline
 * completions are separate concerns — see $isStatusRuleOccluded for why
 * completions never occlude the status rule).
 *
 * SINGLE SOURCE for the floating-panel kind set: consumed by both
 * FloatingOverlays' render gate (plus completions, which it adds locally)
 * and $isStatusRuleOccluded's top-statusbar occlusion arm. Add new floating
 * panels HERE so the timer gate can't silently miss them.
 */
export const hasFloatingPanel = (overlay: OverlayState): boolean =>
  Boolean(
    overlay.modelPicker ||
    overlay.pager ||
    overlay.permissionsPicker ||
    overlay.providerPicker ||
    overlay.sessions ||
    overlay.settingsPanel ||
    overlay.tunePicker,
  );

/**
 * A question to the user is open (approval, clarify, sudo/secret password,
 * confirm): it owns the keyboard. The /tune and /provider popups neither open
 * over one nor read a key while one is up.
 */
export const tuneLocked = (
  overlay: Pick<
    OverlayState,
    "approval" | "clarify" | "confirm" | "secret" | "sudo"
  >,
): boolean =>
  Boolean(
    overlay.approval ||
    overlay.clarify ||
    overlay.confirm ||
    overlay.secret ||
    overlay.sudo,
  );

export const $isStatusRuleOccluded = computed(
  [$overlayState, $uiState],
  (overlay, ui) =>
    Boolean(
      overlay.widget || (ui.statusBar === "top" && hasFloatingPanel(overlay)),
    ),
);

export const getOverlayState = () => $overlayState.get();

export const patchOverlayState = (
  next: Partial<OverlayState> | ((state: OverlayState) => OverlayState),
) =>
  $overlayState.set(
    typeof next === "function"
      ? next($overlayState.get())
      : { ...$overlayState.get(), ...next },
  );

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => $overlayState.set(buildOverlayState());

/** Open the /settings panel; false while a prompt is waiting for an answer. */
export const openSettingsPanel = (): boolean => {
  if (tuneLocked($overlayState.get())) {
    return false;
  }

  patchOverlayState({ settingsPanel: true });

  return true;
};

/** Open the /tune popup; false (nothing opened) while a prompt is waiting for an answer. */
export const openTunePicker = (): boolean => {
  if (tuneLocked($overlayState.get())) {
    return false;
  }

  patchOverlayState({ tunePicker: true });

  return true;
};

/** Open the /provider popup; false (nothing opened) while a prompt is waiting for an answer. */
export const openProviderPicker = (): boolean => {
  if (tuneLocked($overlayState.get())) {
    return false;
  }

  patchOverlayState({ providerPicker: true });

  return true;
};

/** Open the /permissions popup; false (nothing opened) while a prompt is waiting for an answer. */
export const openPermissionsPicker = (): boolean => {
  if (tuneLocked($overlayState.get())) {
    return false;
  }

  patchOverlayState({ permissionsPicker: true });

  return true;
};

/**
 * Soft reset: drop FLOW-scoped overlays (approval / clarify / confirm / sudo
 * / secret / pager) but PRESERVE user-toggled ones — agents dashboard, agent view, model
 * picker, skills hub, sessions overlay.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 */
export const resetFlowOverlays = () =>
  $overlayState.set({
    ...buildOverlayState(),
    agents: $overlayState.get().agents,
    agentsInitialHistoryIndex: $overlayState.get().agentsInitialHistoryIndex,
    agentsInitialAgentId: $overlayState.get().agentsInitialAgentId,
    agentView: $overlayState.get().agentView,
    ambient: $overlayState.get().ambient,
    widget: $overlayState.get().widget,
    modelPicker: $overlayState.get().modelPicker,
    permissionsPicker: $overlayState.get().permissionsPicker,
    providerPicker: $overlayState.get().providerPicker,
    sessions: $overlayState.get().sessions,
    settingsPanel: $overlayState.get().settingsPanel,
    tunePicker: $overlayState.get().tunePicker,
  });
