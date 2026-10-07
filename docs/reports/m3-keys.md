# M3-keys — build report

Worktree: `w-m3-keys` · date: 2026-10-07 · package: `panes/internal/k3keys/` + `panes/cmd/k3/`

## What was built

A zellij-style modal keymap ("k3") for the vendored tuios, delivered through
exactly **two upstream seams** (19 added lines total, verified via
`git diff --stat` below):

1. `internal/input/handler.go` (+10): `PreHandler` hook variable + guard at the
   top of `HandleInput`. Nil in stock tuios → byte-identical behavior.
2. `internal/app/mode_legend.go` (+9): `LegendOverride` hook variable + guard at
   the top of `modeLegend()`. Nil in stock tuios → unchanged legend.

New code (no upstream impact): `internal/k3keys/` (`keymap.go` mode enum /
default bindings / `KeyState.Handle` state machine / `MergeBindings`,
`hints.go` pure-Go hint strips, `hook.go` `Install` wiring both hooks
(`input.RunActionByName` for keys, `app.LegendOverride` for the dock legend),
`config.go` TOML overrides from `~/.config/k3/keys.toml`) plus the `cmd/k3`
entry point.

Key behaviors per spec: leader `ctrl+g` → Chooser (`p/t/s/r///a/?`);
one-shot actions auto-return to Typing with `enter_terminal_mode` appended;
lock via repeated mode letter; Resize locked by default; Esc always returns to
Typing; global `alt+←/→/↑/↓`, `alt+n`, `alt+1..9`, `alt+z`, `ctrl+p` work in
every mode, including locked modes.

## Fixes found during verification

- `ModeSessions` bindings had a duplicate `"s"` key (switcher vs `k3:lock`);
  the switcher moved to `"w"` (lock = mode's own letter per spec) and the
  hints strip was updated to match. Upstream docs (`docs/k3-keymap.md`) agree.
- `TestNoEmojiInSourceStrings` (upstream `internal/app` lint) flagged the 🔒
  lock marker in hints. Replaced with ASCII `[locked]` — the single lesson
  from verification: **no emoji anywhere in source strings**. Test now passes.

## Verification outputs

```
$ go build ./cmd/k3 ./cmd/tuios
BUILD_OK

$ go vet ./internal/k3keys/ ./cmd/k3/
VET_OK

$ go fmt ./internal/k3keys/ ./cmd/k3/
FMT_CLEAN (no changes)

$ go test ./internal/k3keys/ -count=1 -v
ok  github.com/Gaurav-Gosain/tuios/internal/k3keys  0.010s
11 top-level tests, 22 `=== RUN` incl. subtests, 22 `--- PASS`, 0 FAIL
  - TestLeaderToModeToActionToTyping (4 scenarios: panes split_vertical,
    panes round-trip, tabs next_workspace, sessions new_session)
  - TestLockViaRepeatedLetter (lock, action holds mode, Esc exits)
  - TestResizeLockByDefault (auto-locked on entry, holds through actions)
  - TestEscFromEveryMode (7 modes × locked state)
  - TestPassthroughInTyping (8 keys unconsumed)
  - TestGlobalAltKeysInEveryMode (9 keys × 8 modes, locked)
  - TestChooserSelectsEveryMode (6 routes)
  - TestHintsPerMode (non-empty, essential esc, lock-marker diff, chooser
    count = 8: 6 modes + ? + esc)
  - TestModeLabel
  - TestConfigOverride (re-bind, new bind, surviving default, typing override)
  - TestMergeBindingsNilSafe (nil-safe, copies not aliases)

$ go test ./internal/input/ -count=1
ok  github.com/Gaurav-Gosain/tuios/internal/input  5.040s

$ go test ./internal/app/ -run TestNoEmojiInSourceStrings -count=1
ok  github.com/Gaurav-Gosain/tuios/internal/app  0.676s

$ go test ./internal/app/  (full suite, final run after all fixes)
ok    github.com/Gaurav-Gosain/tuios/internal/app  479.572s (exit 0, zero failures)
  Earlier failure (TestNoEmojiInSourceStrings, 🔒 in hints.go) fixed with
  ASCII `[locked]`; baseline on pristine HEAD (git archive, no k3 changes)
  had been green, and the final full-suite run confirms zero regressions.
```

Smoke checks: `go build -o /tmp/k3-bin ./cmd/k3` succeeds; the binary starts
and exits on missing TTY in this headless environment (expected — it is a TUI).
Config format documented in `docs/k3-keymap.md` (`~/.config/k3/keys.toml`,
flat `key = "action"` pairs with optional `mode:` prefix; unknown modes
ignored; user wins over defaults).

## Upstream footprint (git diff --stat)

```
internal/app/mode_legend.go |  9 +++++++++
internal/input/handler.go   | 10 ++++++++++
2 files changed, 19 insertions(+)
```

New (untracked, intended): `K3_CHANGES.md`, `cmd/k3/main.go`,
`docs/k3-keymap.md`, `internal/k3keys/{config,hints,hook,keymap,keymap_test}.go`.

## Open TODOs

- None blocking. Possible follow-ups: an E2E tape test driving `cmd/k3`
  through leader → mode → action; agent-mode (`a`) bindings are currently a
  locked placeholder per spec.

## Legend visibility analysis (spec line 45)

The spec's conditional third hook ("If the legend is not always visible in
typing/terminal mode, find the single place that decides visibility") does
not apply. The full chain, verified by reading each link:

- `render_dock.go:386`: `legend := m.dockModeLegend()` — the only assignment;
  drawn whenever it is non-empty and no live notification block holds the
  right-hand end.
- `dock_helpers.go:762`: `dockModeLegend()` returns nil **only** when the dock
  plan lacks the `copy-help` component. The default plan
  (`config/dock.go`, `defaultDockRight`) lists `copy-help`, so a session with
  no `[dock]` table lets the legend through.
- `modeLegend()`: `LegendOverride` sits at the very top, before any mode
  checks, so it returns the k3 hints unconditionally — including in
  typing/terminal mode, where upstream returns nil and shows nothing. This is
  intended: `TYPING ctrl+g modes · alt+←→ focus · …` is spec line 60's
  example strip.

Conclusion: one hook (`LegendOverride`) fully controls visibility; no second
hook was added, staying under the spec's budget of two upstream files.

## Hook-functor consolidation (spec line 46)

The spec requires `k3keys.Install()` to set **both** hooks. Initially
`Install` set only `input.PreHandler` and `cmd/k3/main.go` set
`app.LegendOverride` (a mistaken belief that k3keys could not import `app`).
Fixed: `Install` now sets both and imports `overlay` only to convert
`k3keys.Hint` → `overlay.Hint` at the boundary (k3keys' own `Hint` type stays
pure for unit tests). `cmd/k3/main.go` is now just config load + `Install` +
program start. Remaining boundary rule honored: `input` and `app` never
import `k3keys`; stock `cmd/tuios` is untouched.
