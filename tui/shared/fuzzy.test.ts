import { describe, expect, it } from "vitest";

import { fuzzyRank, fuzzyScore, fuzzyScoreMulti, searchFold } from "./fuzzy";

describe("fuzzyScore", () => {
  it("matches a query as a subsequence (g4o → gpt-4o)", () => {
    expect(fuzzyScore("gpt-4o", "g4o")).not.toBeNull();
    expect(fuzzyScore("gpt-4o", "gpt")).not.toBeNull();
    expect(fuzzyScore("gpt-4o", "4o")).not.toBeNull();
  });

  it("returns null when characters are out of order or absent", () => {
    expect(fuzzyScore("gpt-4o", "o4g")).toBeNull();
    expect(fuzzyScore("gpt-4o", "xyz")).toBeNull();
    expect(fuzzyScore("gpt-4o", "gptx")).toBeNull();
  });

  it("returns matched positions into the original target", () => {
    const m = fuzzyScore("gpt-4o", "g4o");
    // g@0, 4@4, o@5
    expect(m?.positions).toEqual([0, 4, 5]);
  });

  it("treats an empty query as a zero-score match", () => {
    expect(fuzzyScore("anything", "")).toEqual({ score: 0, positions: [] });
  });

  it("scores an exact match highest", () => {
    const exact = fuzzyScore("sonnet", "sonnet")!.score;
    const prefix = fuzzyScore("sonnet-extended", "sonnet")!.score;
    // s,o,n,n,e,t all present in order but scattered across word boundaries.
    const scattered = fuzzyScore("snorkel-online-nnet", "sonnet")!.score;

    expect(exact).toBeGreaterThan(prefix);
    expect(prefix).toBeGreaterThan(scattered);
  });

  it("ranks a prefix match above a scattered subsequence", () => {
    const prefix = fuzzyScore("gpt-4o-mini", "gpt")!.score;
    const scattered = fuzzyScore("a-g-p-t", "gpt")!.score;

    expect(prefix).toBeGreaterThan(scattered);
  });
});

describe("fuzzyScoreMulti", () => {
  it("requires every space-separated token to match (AND)", () => {
    expect(fuzzyScoreMulti("claude-sonnet-4", "clad snnt")).not.toBeNull();
    expect(fuzzyScoreMulti("claude-sonnet-4", "claude haiku")).toBeNull();
  });

  it("unions matched positions across tokens, sorted", () => {
    const m = fuzzyScoreMulti("claude-sonnet", "son cla");
    expect(m).not.toBeNull();
    expect(m!.positions).toEqual([...m!.positions].sort((a, b) => a - b));
  });

  it("treats whitespace-only query as a zero-score match", () => {
    expect(fuzzyScoreMulti("x", "   ")).toEqual({ score: 0, positions: [] });
  });
});

describe("fuzzyRank", () => {
  const models = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4",
    "claude-haiku",
    "o1-preview",
  ];

  it("drops non-matching items and ranks matches by score", () => {
    const ranked = fuzzyRank(models, "g4o", (m) => m);
    const ids = ranked.map((r) => r.item);

    expect(ids).toContain("gpt-4o");
    expect(ids).toContain("gpt-4o-mini");
    expect(ids).not.toContain("claude-haiku");
    // Shorter exact-ish prefix should outrank the longer variant.
    expect(ids.indexOf("gpt-4o")).toBeLessThan(ids.indexOf("gpt-4o-mini"));
  });

  it("ranks son4 so a sonnet model surfaces", () => {
    const ranked = fuzzyRank(models, "son4", (m) => m);
    expect(ranked[0]?.item).toBe("claude-sonnet-4");
  });

  it("returns all items in original order for an empty query", () => {
    const ranked = fuzzyRank(models, "", (m) => m);
    expect(ranked.map((r) => r.item)).toEqual(models);
    expect(ranked.every((r) => r.positions.length === 0)).toBe(true);
  });

  it("is stable for equal scores (original index tiebreak)", () => {
    const items = ["ab", "ab", "ab"];

    const ranked = fuzzyRank(
      items.map((v, i) => ({ v, i })),
      "ab",
      (x) => x.v,
    );

    expect(ranked.map((r) => r.item.i)).toEqual([0, 1, 2]);
  });

  // Ids use hyphens, display names spaces, quants underscores, versions dots;
  // a user typing from memory picks any of them and must still get the row.
  it.each([
    ["gpt.4o", "gpt-4o"],
    ["claude_3", "claude-3-opus"],
    ["qwen3-8", "qwen3.8-flash"],
  ])("folds separators on both sides so %s finds %s", (query, expected) => {
    const catalog = ["gpt-4o", "claude-3-opus", "qwen3.8-flash", "o1-preview"];
    const ranked = fuzzyRank(catalog, query, (m) => m);

    expect(ranked[0]?.item).toBe(expected);
    // Positions still index the ORIGINAL target (fold is length-preserving).
    expect(
      ranked[0]!.positions.every((i) => i >= 0 && i < expected.length),
    ).toBe(true);
  });

  it("matches across a derived key, not just the raw string", () => {
    const providers = [
      { slug: "openai", name: "OpenAI" },
      { slug: "anthropic", name: "Anthropic" },
    ];

    const ranked = fuzzyRank(providers, "anth", (p) => `${p.name} ${p.slug}`);
    expect(ranked[0]?.item.slug).toBe("anthropic");
  });
});

// `toLowerCase()` is not length-preserving: "İ" (U+0130) lower-cases to "i" plus a
// combining dot (two UTF-16 units). Positions found in the folded string then
// indexed past the original and `isBoundary` threw a TypeError on `undefined`,
// which took the model picker's filter down with it.
describe("searchFold keeps the length of what it folds", () => {
  it.each([
    "İstanbul",
    "İ-x",
    "straße",
    "ẞTRASSE",
    "ﬁsh",
    "ǅ", // titlecase digraph
    "Σίσυφος", // final sigma
    "𐐀bc", // Deseret capital (astral, two UTF-16 units)
    "😀 İ😀",
    "gpt-4o_İ.1",
  ])("%s", (value) => {
    expect(searchFold(value)).toHaveLength(value.length);
  });

  it("still lower-cases and folds the separators", () => {
    expect(searchFold("GPT-4o_Mini.v2")).toBe("gpt 4o mini v2");
    expect(searchFold("Σίσυφος")).toBe("σίσυφος");
    expect(searchFold("𐐀bc")).toBe("𐐨bc");
  });

  it("folds İ to a plain i so a user typing without the dot finds it", () => {
    expect(searchFold("İstanbul")).toBe("istanbul");
  });
});

describe("fuzzyScore on characters whose lower-case form is longer", () => {
  it.each([
    ["İstanbul", "l", [7]],
    ["İ-x", "x", [2]],
    ["İstanbul", "ist", [0, 1, 2]],
    ["istanbul", "İst", [0, 1, 2]],
    ["Straße-4", "e", [5]],
    ["Straße-4", "4", [7]],
    ["ﬁsh-mix", "mix", [4, 5, 6]],
    ["𐐀bc", "b", [2]],
    ["😀İ-x", "x", [4]],
  ])(
    "fuzzyScore(%j, %j) positions %j index the original",
    (target, query, positions) => {
      const match = fuzzyScore(target, query);

      expect(match).not.toBeNull();
      expect(match!.positions).toEqual(positions);
    },
  );

  it("does not throw for any single character of the target as the query", () => {
    for (const target of ["İstanbul", "İ-x", "Straße", "ﬁsh", "😀İ"]) {
      for (const ch of target) {
        expect(() => fuzzyScore(target, ch)).not.toThrow();
      }
    }
  });
});

describe("fuzzyRank on a catalog with İ in it", () => {
  const catalog = ["gpt-4o", "İstanbul", "İ-x", "claude-haiku"];

  it.each(["l", "x", "i", "ist", "İ"])(
    "ranks without throwing for %j",
    (query) => {
      expect(() => fuzzyRank(catalog, query, (m) => m)).not.toThrow();
    },
  );

  it("finds İstanbul and keeps the positions inside the original text", () => {
    const ranked = fuzzyRank(catalog, "l", (m) => m);

    expect(ranked.map((r) => r.item)).toEqual(["İstanbul", "claude-haiku"]);
    expect(ranked[0]!.positions).toEqual([7]);
    expect(fuzzyRank(catalog, "ist", (m) => m)[0]?.item).toBe("İstanbul");
  });
});
