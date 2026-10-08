import { afterEach, describe, expect, it, vi } from "vitest";

import {
  $proposals,
  addProposal,
  clearProposals,
  glyphFor,
  handleProposalKey,
  PROPOSALS_MAX,
  removeProposal,
  setProposalHandlers,
} from "../../k3/proposalsStore.js";

const card = (id: string) => ({
  action: `do ${id}`,
  id,
  kind: "improvement" as const,
  text: `text ${id}`,
});

afterEach(() => {
  $proposals.set([]);
  setProposalHandlers(null);
});

describe("proposal cards", () => {
  it("dedupes by id and keeps the newest three", () => {
    ["p1", "p1", "p2", "p3", "p4"].forEach((id) => addProposal(card(id)));
    expect($proposals.get().map((p) => p.id)).toEqual(["p2", "p3", "p4"]);
    expect($proposals.get().length).toBeLessThanOrEqual(PROPOSALS_MAX);
  });

  it("alt+y accepts and alt+n dismisses the top card; a bare letter never does", () => {
    const accept = vi.fn();
    const dismiss = vi.fn();
    setProposalHandlers({ accept, dismiss });
    addProposal(card("p1"));
    addProposal(card("p2"));

    // the first letter of "add a test ..." / "do ..." must reach the composer, not the card
    expect(handleProposalKey("y", false)).toBe(false);
    expect(handleProposalKey("n", false)).toBe(false);
    expect(handleProposalKey("a", true)).toBe(false); // not the keys
    expect(handleProposalKey("d", true)).toBe(false); // alt+d is the input's kill-word
    expect(handleProposalKey("x", true)).toBe(false);
    expect(accept).not.toHaveBeenCalled();
    expect(dismiss).not.toHaveBeenCalled();
    expect($proposals.get().map((p) => p.id)).toEqual(["p1", "p2"]);

    expect(handleProposalKey("y", true)).toBe(true);
    expect(accept).toHaveBeenCalledWith(card("p1"));
    expect(handleProposalKey("n", true)).toBe(true);
    expect(dismiss).toHaveBeenCalledWith(card("p2"));
    expect(handleProposalKey("y", true)).toBe(false); // no cards left
  });

  it("clearProposals drops every card (cards belong to the session that produced them)", () => {
    addProposal(card("p1"));
    addProposal(card("p2"));
    clearProposals();
    expect($proposals.get()).toEqual([]);
  });

  it("removeProposal drops one card", () => {
    addProposal(card("p1"));
    removeProposal("p1");
    expect($proposals.get()).toEqual([]);
  });
});

describe("kind icons", () => {
  it("has a distinct icon for every learned kind and a fallback for unknown ones", () => {
    const kinds = [
      "permission_rule",
      "preference",
      "project_setup",
      "skill",
      "optimizer",
      "consequence",
      "improvement",
    ];
    const icons = kinds.map(glyphFor);

    expect(new Set(icons).size).toBe(kinds.length);
    expect(glyphFor("something_new")).toBe("•");
  });
});
