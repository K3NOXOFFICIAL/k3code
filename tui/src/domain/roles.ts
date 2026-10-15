import type { Theme } from "../theme.js";
import type { Role } from "../types.js";

/**
 * Per-role transcript styling. Each role must read differently at a glance:
 * the user's prompt wears a `❯ you` header plus a gutter bar in the user
 * accent, the assistant's answer a `● name` header in the assistant accent,
 * and system notes stay muted.
 */
export const ROLE: Record<
  Role,
  (t: Theme) => { body: string; glyph: string; prefix: string }
> = {
  assistant: (t) => ({
    body: t.color.text,
    glyph: " ",
    prefix: t.color.assistantAccent,
  }),
  system: (t) => ({ body: "", glyph: "·", prefix: t.color.muted }),
  tool: (t) => ({ body: t.color.muted, glyph: "⚡", prefix: t.color.muted }),
  user: (t) => ({
    body: t.color.label,
    glyph: "┃",
    prefix: t.color.userAccent,
  }),
};

/** `❯ you` / `● k3code`: the header line above a user prompt or an answer. */
export const roleHeader = (role: Role, t: Theme): string =>
  role === "user" ? `${t.brand.prompt} you` : `● ${t.brand.name}`;

// Prompts the harness writes itself are labelled, not shown as the user's
// words: goal kicks and continuations (core goals.py) start with
// `[Standing goal]` / `[Continuing toward your standing goal…]`; the
// max_tokens continuation (agent/loop.py CUT_OFF_CONTINUE) is a fixed
// sentence.
const GOAL_PROMPT_RE = /^\[(?:Standing goal|Continuing toward your standing goal)[^\]\n]*\]\n/;
const CUT_OFF_PREFIX = "Your previous answer was cut off at the output token limit.";

export type UserPromptSource = "goal" | "system" | "user";

/** Who wrote a user-role prompt: the person, the goal runner, or the harness. */
export const userPromptSource = (text: string): UserPromptSource => {
  if (GOAL_PROMPT_RE.test(text)) {
    return "goal";
  }

  if (text.startsWith(CUT_OFF_PREFIX)) {
    return "system";
  }

  return "user";
};
