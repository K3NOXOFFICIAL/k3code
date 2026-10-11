import { atom } from "nanostores";

import type { DelegationStatusResponse } from "../gatewayTypes.js";

export interface DelegationState {
  // True when agent counts are sized from the task (`autonomy.agents.dynamic`).  null until fetched.
  dynamic: boolean | null;
  // Most sub-agents one /ultracode run may start: a number or "auto" (`ultracode.max_agents`, /maxagents).
  maxAgents: null | number | string;
  // Last known caps from `delegation.status` RPC.  null until fetched.
  maxConcurrentChildren: null | number;
  // Sub-agents of a session running at once (`autonomy.agents.max_parallel`): the cap on the model's own task calls.
  maxParallel: null | number;
  maxSpawnDepth: null | number;
  // True when spawning is globally paused (see tools/delegate_tool.py).
  paused: boolean;
  // Monotonic clock of the last successful status fetch.
  updatedAt: null | number;
}

const buildState = (): DelegationState => ({
  dynamic: null,
  maxAgents: null,
  maxConcurrentChildren: null,
  maxParallel: null,
  maxSpawnDepth: null,
  paused: false,
  updatedAt: null,
});

export const $delegationState = atom<DelegationState>(buildState());

export const getDelegationState = () => $delegationState.get();

export const patchDelegationState = (next: Partial<DelegationState>) =>
  $delegationState.set({ ...$delegationState.get(), ...next });

export const resetDelegationState = () => $delegationState.set(buildState());

// ── Overlay accordion open-state ──────────────────────────────────────
//
// Lifted out of OverlaySection's local useState so collapse choices
// survive:
//   - navigating to a different subagent (Detail remounts)
//   - switching list ↔ detail mode (Detail unmounts in list mode)
//   - walking history (←/→)
// Keyed by section title; missing entries fall back to the section's
// `defaultOpen` prop.

export const $overlaySectionsOpen = atom<Record<string, boolean>>({});

export const toggleOverlaySection = (title: string, defaultOpen: boolean) => {
  const state = $overlaySectionsOpen.get();
  const current = title in state ? state[title]! : defaultOpen;

  $overlaySectionsOpen.set({ ...state, [title]: !current });
};

export const getOverlaySectionOpen = (
  title: string,
  defaultOpen: boolean,
): boolean => {
  const state = $overlaySectionsOpen.get();

  return title in state ? state[title]! : defaultOpen;
};

/** Merge a raw RPC response into the store.  Tolerant of partial/omitted fields. */
export const applyDelegationStatus = (
  r: DelegationStatusResponse | null | undefined,
) => {
  if (!r) {
    return;
  }

  const patch: Partial<DelegationState> = { updatedAt: Date.now() };

  if (typeof r.max_spawn_depth === "number") {
    patch.maxSpawnDepth = r.max_spawn_depth;
  }

  if (typeof r.max_concurrent_children === "number") {
    patch.maxConcurrentChildren = r.max_concurrent_children;
  }

  if (typeof r.max_agents === "number" || typeof r.max_agents === "string") {
    patch.maxAgents = r.max_agents;
  }

  if (typeof r.dynamic === "boolean") {
    patch.dynamic = r.dynamic;
  }

  if (typeof r.max_parallel === "number") {
    patch.maxParallel = r.max_parallel;
  }

  if (typeof r.paused === "boolean") {
    patch.paused = r.paused;
  }

  patchDelegationState(patch);
};
