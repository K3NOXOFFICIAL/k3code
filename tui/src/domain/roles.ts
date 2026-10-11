import type { Theme } from "../theme.js";
import type { Role } from "../types.js";

/**
 * Per-role transcript styling. Each role reads differently at a glance:
 * the user's prompt sits on a tinted `fill`, the assistant's reply is marked
 * by an accent gutter glyph, and tool/system chrome stays dim.
 */
export const ROLE: Record<
  Role,
  (t: Theme) => { body: string; fill?: string; glyph: string; prefix: string }
> = {
  assistant: (t) => ({
    body: t.color.text,
    glyph: t.brand.tool,
    prefix: t.color.accent,
  }),
  system: (t) => ({ body: "", glyph: "·", prefix: t.color.muted }),
  tool: (t) => ({
    body: t.color.toolDim,
    glyph: "⚡",
    prefix: t.color.toolDim,
  }),
  user: (t) => ({
    body: t.color.label,
    fill: t.color.userBg,
    glyph: t.brand.prompt,
    prefix: t.color.label,
  }),
};
