import { afterEach, describe, expect, it } from "vitest";

import {
  $delegationState,
  applyDelegationStatus,
  resetDelegationState,
} from "../app/delegationStore.js";

describe("applyDelegationStatus", () => {
  afterEach(() => resetDelegationState());

  it("keeps the effective caps and the agent switches the gateway reports", () => {
    applyDelegationStatus({
      dynamic: true,
      max_agents: "auto",
      max_concurrent_children: 3,
      max_parallel: 4,
      max_spawn_depth: 2,
      paused: false,
    });

    expect($delegationState.get()).toMatchObject({
      dynamic: true,
      maxAgents: "auto",
      maxConcurrentChildren: 3,
      maxSpawnDepth: 2,
      paused: false,
    });
  });

  it("is tolerant of a gateway that sends none of the new fields", () => {
    applyDelegationStatus({ max_concurrent_children: 3, max_spawn_depth: 2 });

    expect($delegationState.get()).toMatchObject({
      dynamic: null,
      maxAgents: null,
      maxConcurrentChildren: 3,
    });
  });

  it("takes a number for max_agents and ignores values of the wrong type", () => {
    applyDelegationStatus({ dynamic: false, max_agents: 20 });
    applyDelegationStatus({
      dynamic: "yes" as unknown as boolean,
      max_agents: {} as unknown as number,
    });

    expect($delegationState.get()).toMatchObject({
      dynamic: false,
      maxAgents: 20,
    });
  });
});

describe("applyDelegationStatus max_parallel", () => {
  afterEach(() => resetDelegationState());

  it("keeps the per-session cap apart from the fan-out width", () => {
    applyDelegationStatus({ max_concurrent_children: 3, max_parallel: 4 });

    expect($delegationState.get()).toMatchObject({
      maxConcurrentChildren: 3,
      maxParallel: 4,
    });
  });

  it("leaves it unset for a gateway that does not send it", () => {
    applyDelegationStatus({ max_concurrent_children: 3 });

    expect($delegationState.get().maxParallel).toBeNull();
  });
});
