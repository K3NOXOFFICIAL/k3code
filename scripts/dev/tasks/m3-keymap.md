# M3-keymap: k3 panes, an intuitive zellij-style keymap for the vendored tuios (`panes/`)

`panes/` is a vendored snapshot of tuios (MIT, Go 1.26, Bubble Tea v2), from commit f3d892942bfb45a0a0775d14895ae77ff9c06468. It builds with `cd panes && go build ./cmd/tuios`.

Users find tuios' window-management mode and shortcuts unintuitive:
- Esc doesn't leave a pane.
- The same action has three different keys depending on mode.
- There is no visible mode indicator.
- The prefix chords are case-sensitive and nested.
- There are too many scopes.

Replace the default UX with the following, and keep the old behaviour behind `keymap = "tuios"` in config.

## Target keymap (`keymap = "k3"`, the new default)

**Typing mode (default)**
- Every key goes to the focused pane, like a normal terminal or Claude Code. Start in this mode, not in WM mode.

**Leader `Ctrl+G`**
- Opens a one-shot mode chooser with a hint popup: `p` panes, `t` tabs/workspaces, `s` sessions, `r` resize, `/` search/scrollback, `?` help, `a` agents/inbox.
- Inside a mode, single unshifted letters and arrows act. For example in panes mode: `n` new, `x` close, `v` split vertical, `h` split horizontal, arrows move focus, `z` zoom, `f` float/tile toggle.
- After one action, return to typing mode automatically. Pressing the mode letter again (e.g. `Ctrl+G p p`) locks the mode until Esc.

**Esc**
- Always returns to typing mode from any k3 mode or popup.
- In typing mode, Esc is passed through to the program.

**Global Alt shortcuts (work in every mode)**
- `Alt+←/→/↑/↓` focus a neighbour.
- `Alt+n` opens a new pane running `k3code` (configurable command, falls back to `$SHELL`).
- `Alt+1..9` jump to a workspace.
- `Alt+z` zoom.
- `Alt+x` close the pane (with confirm).

**Palette**
- `Ctrl+P` opens the existing command palette. Every entry must show its k3 key binding, if any, right-aligned.

**Always-visible bottom hint bar**
- One line showing the current mode name (e.g. `TYPING`, `PANES`, `PANES 🔒`) and the 6–8 valid keys for that mode. In typing mode show `Ctrl+G modes · Alt+←→ focus · Alt+n new · Ctrl+P palette`.
- It can be hidden by config (`hint_bar = false`) but is on by default.

**Configuration**
- Every k3 binding must be rebindable in the config, using the existing config/keybinding system where possible.
- Each action has exactly one default key per mode.

## Implementation constraints (keep future upstream merges cheap)

- Put the new logic in a new package `panes/internal/k3keys/` (mode state machine, mode tables, legends/hints) with unit tests.
- Touch upstream files minimally: input dispatch hook points, default config, status bar/hint rendering. List every touched upstream file and why in `panes/K3_CHANGES.md`.
- Reuse the existing action registry (named actions) instead of duplicating behaviour.
- Rename the user-facing binary to `k3` by adding `cmd/k3/main.go`, which reuses the tuios main with the k3 defaults. Keep `cmd/tuios` building.

## Acceptance (put the outputs in REPORT.md)

- `cd panes && go build ./cmd/k3 ./cmd/tuios` succeeds.
- `go test ./internal/k3keys/...` passes with tests covering:
  - leader → mode → action → auto-return to typing;
  - lock via repeated mode letter;
  - Esc from every mode;
  - hint text per mode;
  - Alt shortcuts in every mode;
  - rebinding through config.
- `go test ./...` for the packages you touched still passes. Record any pre-existing failures that are unrelated to you, and verify them by running the same test on the untouched code first.
- `go vet` is clean for `internal/k3keys`.
- Write a short `panes/docs/k3-keymap.md` cheat sheet.
