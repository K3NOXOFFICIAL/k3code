import { atom } from "nanostores";

// Affection-heart beat: a monotonic tick the status-bar ♥ flashes on. Bumped by
// the gateway `reaction` event (core-detected ily / <3 / good bot) — the TUI's
// share of the same signal that plays the desktop's floating hearts.
export const $goodVibesTick = atom(0);

export const flashGoodVibes = () =>
  $goodVibesTick.set($goodVibesTick.get() + 1);
