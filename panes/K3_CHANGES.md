# K3 Changes — Upstream File Modifications

The `k3` binary (`cmd/k3`) is a variant of TUIOS that installs a modal,
zellij-style keymap. Everything new lives in `panes/internal/k3keys/` and
`panes/cmd/k3/` — no upstream behavior changes unless both flags below are
nil, which is the case for the stock `tuios` binary.

## Upstream files touched

Exactly **two** upstream files are modified (verified with `git diff --stat`):

### 1. `internal/input/handler.go` — PreHandler hook (+10 lines)

Adds a package-level hook variable and a guard at the top of `HandleInput`:

```go
// k3: PreHandler hook for k3keys keymap
var PreHandler func(tea.Msg, *app.OS) (bool, tea.Model, tea.Cmd)
```

```go
if PreHandler != nil {
    if ok, m, c := PreHandler(msg, o); ok {
        return m, c
    }
}
```

If `PreHandler` is nil (stock tuios), behavior is byte-for-byte identical to
upstream. If it is set and returns `ok=true`, the key was consumed by the k3
keymap and `HandleInput` returns immediately; `ok=false` falls through to the
normal upstream handling.

### 2. `internal/app/mode_legend.go` — LegendOverride hook (+9 lines)

Adds a package-level hook variable and a guard at the top of `modeLegend()`:

```go
// k3: LegendOverride hook for k3keys keymap hints
var LegendOverride func(*OS) []overlay.Hint
```

```go
if LegendOverride != nil {
    return LegendOverride(o)
}
```

If `LegendOverride` is nil (stock tuios), the upstream legend renders exactly
as before. The k3 binary replaces the hint strip with the mode's keymap hints.

The legend is visible in every k3 mode, including typing/terminal mode: the
default dock plan lists the `copy-help` component (`config/dock.go`'s
`defaultDockRight`), which is the one that carries every mode's keys, so
`dockModeLegend()` (`internal/app/dock_helpers.go`) lets the legend through,
and `render_dock.go` draws it whenever `modeLegend()` returns non-nil. No
separate visibility hook is needed — the single `LegendOverride` hook is
sufficient.

### Test-only timing fix

`internal/app/dock_reload_leak_test.go` waits for the custom dock component to
draw (up to 30 s) instead of assuming it has after 50 ms of silence. On a busy
machine the component's shell command can take longer than that to start, so
the upstream version failed the full check under load. No production code
changes; an upstream merge can take either side of this file.

## New files (no upstream impact)

| Path | Purpose |
|---|---|
| `internal/k3keys/keymap.go` | Mode enum, default bindings table, `KeyState` state machine (`Handle`), `MergeBindings` |
| `internal/k3keys/hints.go` | Pure-Go `Hint` type, per-mode hint strips, `ModeLabel` |
| `internal/k3keys/hook.go` | `Install` — wires `input.PreHandler` and `app.LegendOverride`, running actions via `input.RunActionByName` |
| `internal/k3keys/config.go` | TOML user overrides from `~/.config/k3/keys.toml` (`go-toml/v2`) |
| `internal/k3keys/keymap_test.go` | Table tests for the state machine and hints |
| `cmd/k3/main.go` | Entry point: loads config, calls `k3keys.Install`, runs the TUI |

## Package boundary

`internal/k3keys` never imports `overlay`: conversion from `k3keys.Hint` to
`overlay.Hint` happens in `hook.go`'s `Install`, at the package boundary.
Per the spec, `Install` sets **both** hooks. `k3keys` imports `app` and
`input` (only in `hook.go`); neither imports `k3keys`.
