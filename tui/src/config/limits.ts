export const LARGE_PASTE = { lines: 5 };

export const LIVE_RENDER_MAX_CHARS = 16_000;
export const LIVE_RENDER_MAX_LINES = 240;

// Persisted verbose tool-trail blocks (Args/Result embedded in a completed
// tool line) are kept for the WHOLE session in transcript Msg.tools[] and
// rendered expanded by default, so a render-node tree is built for every one
// of up to MAX_HISTORY messages at once. Capping these to the live-render
// budget (16KB) let a heavy browser/large-output session retain ~12MB of
// strings that exploded into a few hundred MB of Ink nodes and silently OOM-
// killed the Node parent (→ stdin EOF, gateway death; issue #34095). The live
// streaming tail still uses the larger LIVE_RENDER budget — only the persisted
// per-call block shrinks to a readable preview here. Full output remains in the
// agent context and the SQLite session; the trail is a glance, not a log.
export const VERBOSE_TRAIL_MAX_CHARS = 800;
export const VERBOSE_TRAIL_MAX_LINES = 12;

// Rows the live area paints for one tool trail. The live area re-renders on
// every gateway event, so a burst of N tool calls costs N renders of every
// row so far: CPU grew with N squared and RSS peaked near 250 MB at N=160
// (#164). Settled transcript messages are painted once and stay uncapped.
export const LIVE_TOOL_ROWS_MAX = 24;

export const LONG_MSG = 300;
export const MAX_HISTORY = 800;
export const THINKING_COT_MAX = 160;

// Rows per wheel event (pre-accel). 1 keeps Ink's DECSTBM fast path live
// (each scroll < viewport-1) and produces smooth motion. wheelAccel.ts
// ramps this on sustained scrolls.
export const WHEEL_SCROLL_STEP = 1;
