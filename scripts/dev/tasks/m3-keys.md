# M3-keys: k3 keymap for `panes/` (vendored tuios) via ONE pre-handler hook

Read `scripts/dev/tasks/m3-keymap.md` for the UX goal: what the keys should do and why. This spec tells you exactly **where** to implement it, so do NOT explore the whole tuios codebase. The facts below are verified. Read only the files named here.

## Verified seams (tuios @f3d8929, in `panes/`)

- **Key entry.** `internal/input/handler.go:18` defines `func HandleInput(msg tea.Msg, o *app.OS) (tea.Model, tea.Cmd)`. This is the single entry point for all key and mouse input, registered with `app.SetInputHandler(input.HandleInput)` in `cmd/tuios/run.go` and other places.
- **Run any action by name.** `internal/input/action_runner.go:28` defines `func RunActionByName(name string, o *app.OS) (handled bool, cmd tea.Cmd)`, which runs an action exactly as its key would.
- **Action names you need.** All of these exist:
  - focus: `terminal_focus_left|right|up|down`, `focus_up`, `focus_down`
  - mode switching: `enter_terminal_mode`, `enter_window_mode`, `terminal_exit_mode`
  - panes: `new_window`, `close_window`, `split_vertical`, `split_horizontal`, `toggle_zoom`, `toggle_tiling`, `equalize_splits`, `rotate_split`, `swap_left|right|up|down`
  - resize: `resize_height_grow`, `resize_height_shrink`, `resize_master_grow`, `resize_master_shrink`
  - workspaces: `next_workspace`, `prev_workspace`, `switch_workspace_` + digit (prefix action), `rename_workspace`
  - sessions: `new_session`, `next_session`, `prev_session`, `rename_session`, `prefix_session_switcher`
  - other: `command_palette`, `prefix_scrollback`, `prefix_inbox`, `toggle_help`, `prefix_keybinds`, `prefix_detach`
- **Mode legend (bottom hint area).** `internal/app/mode_legend.go:32` has `func (m *OS) modeLegend() []overlay.Hint`. `renderModeLegend` draws it.

## Implementation

1. **New package `panes/internal/k3keys/`** (pure Go, unit-testable):
   - `keymap.go`: the mode state machine. Modes: `Typing`, `Chooser` (after the leader), `Panes`, `Tabs`, `Sessions`, `Resize`, `Search`, `Agents`, plus a `locked` flag.
   - Default bindings as a table: `map[Mode]map[string]string`, mapping a key string to a tuios action name, plus internal pseudo-actions such as `k3:lock` and `k3:typing`.
   - `Handle(key string) (actions []string, consumed bool)`: pure logic, no tuios types, so it is easy to test.
   - `hints.go`: `Hints(mode, locked) []Hint` returns the 6–8 keys to show for the current mode.
   - `hook.go`: glue to tuios. It turns a `tea.KeyPressMsg` into a key string (`msg.String()`), calls `Handle`, runs the returned actions with `input.RunActionByName`, and batches the cmds. After a one-shot action it returns to typing mode and runs `enter_terminal_mode`, so keys go to the pane again.
   - `config.go`: reads overrides from the existing tuios config if trivial. Otherwise use `~/.config/k3/keys.toml` (`[panes] n = "new_window"`). Defaults apply if the file is missing.
2. **Upstream hooks.** Keep each to the ~3 lines shown. Mark each `// k3:` and list it in `panes/K3_CHANGES.md`.
   - `internal/input/handler.go`: add
     ```go
     var PreHandler func(tea.Msg, *app.OS) (bool, tea.Model, tea.Cmd)
     ```
     At the very top of `HandleInput`:
     ```go
     if PreHandler != nil { if ok, m, c := PreHandler(msg, o); ok { return m, c } }
     ```
   - `internal/app/mode_legend.go`: add
     ```go
     var LegendOverride func(*OS) []overlay.Hint
     ```
     At the top of `modeLegend()`:
     ```go
     if LegendOverride != nil { if h := LegendOverride(m); h != nil { return h } }
     ```
     If the legend is not always visible in typing/terminal mode, find the single place that decides visibility and add one hook there as well.
   - `k3keys.Install()` sets both hooks. Because `k3keys` imports `input`, `input` must never import `k3keys`.
3. **`panes/cmd/k3/main.go`**: copy `cmd/tuios/main.go`, call `k3keys.Install()` before the program runs, and start in terminal/typing mode. Keep `cmd/tuios` unchanged so it behaves exactly like upstream.
4. **Keys, from m3-keymap.md:**
   - **Leader.** `ctrl+g` opens the Chooser. In the Chooser, `p`/`t`/`s`/`r`/`/`/`a`/`?` select a mode.
   - **Panes mode.** `n` new_window, `x` close_window, `v` split_vertical, `h` split_horizontal, arrows focus, `z` toggle_zoom, `f` toggle_tiling, `=` equalize_splits.
   - **Tabs/workspaces mode.** `n`/`p` next/prev, `1`–`9` switch, `r` rename.
   - **Sessions mode.** `n` new, `j`/`k` next/prev, `s` switcher, `r` rename, `d` detach.
   - **Resize mode.** Arrows map to the resize actions. It is lock-by-default until Esc.
   - **Lock and Esc.** Pressing a mode's letter again locks the mode. Esc always returns to Typing.
   - **Global (typing mode too).** `alt+left|right|up|down` focus, `alt+n` new_window, `alt+1..9` switch workspace, `alt+z` zoom, `ctrl+p` command_palette.
   - **Typing mode.** Every other key passes through untouched: return `consumed=false`, so `HandleInput` handles it as usual.
5. **Hints.**
   - Typing: `TYPING  ctrl+g modes · alt+←→ focus · alt+n new · ctrl+p palette`.
   - Chooser: `MODES  p panes · t tabs · s sessions · r resize · / search · a agents · esc back`.
   - Each mode lists its keys, with a `🔒` suffix when locked.

## Do not
- Do not reuse the earlier failed attempt's files.
- Do not rewrite tuios' keybinding config system or touch any other upstream files beyond the hooks above, unless a hook is impossible. If you hit that case, explain why in K3_CHANGES.md.

## Tests (`go test ./internal/k3keys/...`)
Table tests on `Handle` covering:
- leader → mode → action → back to Typing;
- lock via a repeated letter;
- Esc from every mode;
- passthrough in Typing;
- global Alt keys in every mode;
- hints per mode;
- config override.

## Acceptance (put the outputs in REPORT.md)
- `cd panes && go build ./cmd/k3 ./cmd/tuios` succeeds.
- `go vet ./internal/k3keys/` is clean, and `go test ./internal/k3keys/...` passes.
- `go test ./internal/input/ ./internal/app/` reports no new failures. Run it on a clean checkout first (`git stash -u -m k3base` is NOT allowed; use `git worktree` or compare with `git diff`) or record the pre-existing failures.
- Add `panes/docs/k3-keymap.md` as a cheat sheet.
- Only these files change upstream: `handler.go`, `mode_legend.go`, plus at most one visibility hook. Verify with `git diff --stat`.
