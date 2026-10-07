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
`hints.go` pure-Go hint strips, `hook.go` `Install` wiring
`input.RunActionByName`, `config.go` TOML overrides from
`~/.config/k3/keys.toml`) plus the `cmd/k3` entry point.

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

$ go test ./internal/app/  (full suite, ~5-7 min per run)
FAIL  github.com/Gaurav-Gosain/tuios/internal/app  327.817s
  --- FAIL: TestNoEmojiInSourceStrings  (only failure; fixed as above)
  Baseline on pristine HEAD (git archive, no k3 changes): pass (baseline
  `--- FAIL` list was empty; after-fix list matches it).
  The full app suite's other tests were green both before and after the fix.
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
