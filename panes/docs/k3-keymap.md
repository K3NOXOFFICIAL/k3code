# k3 keymap cheat sheet

The `k3` binary adds a modal, zellij-style keymap on top of TUIOS. While
typing, keys pass through to the terminal untouched — nothing changes until
you summon the Chooser.

User overrides live in `~/.config/k3/keys.toml` (see bottom).

## The leader: `ctrl+g`

Press `ctrl+g` anywhere to open the **Chooser** (MODES). Then press one letter:

| Key | Mode | What it does |
|-----|------|--------------|
| `p` | PANES | Split, close, focus, zoom panes |
| `t` | TABS | Workspace (tab) navigation |
| `s` | SESSIONS | Session management |
| `r` | RESIZE | Resize pane splits |
| `/` | SEARCH | Scrollback search & inbox |
| `a` | AGENTS | Agent placeholder (reserved) |
| `?` | — | Toggle help |
| `esc` | — | Back to typing |

## One-shot vs. locked

Most actions are **one-shot**: run, then you're back to typing immediately.

To stay in a mode, press the mode's letter again — the hint strip shows
`[locked]`. Resize mode is **locked by default**. Press `esc` to leave a
locked mode.

## PANES (from Chooser: `p`)

| Key | Action |
|-----|--------|
| `n` | New window (pane) |
| `x` | Close focused pane |
| `v` | Split vertically |
| `h` | Split horizontally |
| `←/→/↑/↓` | Focus pane in direction |
| `z` | Toggle zoom |
| `f` | Toggle tiling |
| `=` | Equalize splits |
| `p` | Lock mode (stay in PANES) |
| `esc` | Back to typing |

## TABS (from Chooser: `t`)

| Key | Action |
|-----|--------|
| `n` / `p` | Next / previous workspace |
| `1`–`9` | Switch to workspace N |
| `r` | Rename workspace |
| `t` | Lock mode |
| `esc` | Back to typing |

## SESSIONS (from Chooser: `s`)

| Key | Action |
|-----|--------|
| `n` | New session |
| `j` / `k` | Next / previous session |
| `w` | Session switcher |
| `r` | Rename session |
| `d` | Detach |
| `s` | Lock mode |
| `esc` | Back to typing |

## RESIZE (from Chooser: `r` — locked by default)

| Key | Action |
|-----|--------|
| `←` / `→` | Shrink / grow height |
| `↑` / `↓` | Grow / shrink master |
| `r` | Toggle lock |
| `esc` | Back to typing |

## SEARCH (from Chooser: `/`)

| Key | Action |
|-----|--------|
| `/` | Scrollback search |
| `i` | Inbox |
| `s` | Lock mode |
| `esc` | Back to typing |

## Global keys — work in *every* mode

| Key | Action |
|-----|--------|
| `alt+←/→/↑/↓` | Focus terminal in direction |
| `alt+n` | New window |
| `alt+1`–`alt+9` | Switch workspace N |
| `alt+z` | Toggle zoom |
| `ctrl+p` | Command palette |
| `ctrl+g` | Open the Chooser (from typing) |

## User overrides: `~/.config/k3/keys.toml`

Flat `key = "action"` pairs; prefix with `mode:` to bind in a mode other than
typing. Unknown modes are ignored; user bindings win over defaults.

```toml
# typing mode (no prefix)
"ctrl+g" = "k3:chooser"

# mode-prefixed
"panes:n" = "rotate_split"
"panes:q" = "close_window"
"tabs:1" = "toggle_tiling"
```

Valid modes: `typing` (default, no prefix), `chooser`, `panes`, `tabs`,
`sessions`, `resize`, `search`, `agents`. Actions are TUIOS action names
(see `internal/app/action_runner.go`); the pseudo-actions `k3:chooser`,
`k3:mode:<mode>`, `k3:lock` and `k3:typing` are also valid.
