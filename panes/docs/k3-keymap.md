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

## AGENTS (from Chooser: `a`)

| Key | Action |
|-----|--------|
| `i` | Inbox: approvals, questions and finished turns from every agent pane |
| `n` | Jump to the next pane that is waiting for you |
| `s` | Agent settings |
| `a` | Lock mode |
| `esc` | Back to typing |

**Pane badges.** A pane that runs an agent carries a state badge in its title
and a row on the rail: `working`, `needs input`, `idle`, `done` (a turn
finished, listed in the Inbox until you look) or `errored`. k3code reports its
own state (`working` while a turn runs, `done` when it ends, `needs input`
while it waits for an approval or an answer, `errored` after a failed
unattended run), so no screen reading is involved. The manifest is
`internal/harness/manifests/k3code.toml`.

**Inbox approvals.** Add k3code to the harnesses whose approvals the Inbox may
answer, in the tuios config:

```toml
[agents.approvals]
enabled = ["k3code"]
```

When a k3code pane asks for permission and you are not looking at that pane,
the request appears in the Inbox. Answer there: `once` and `deny` always work;
`always` is offered when the request has a rule to persist (it writes the same
project rule as answering "always" in k3code). The pane's own prompt also has
"session" (allow for this session only), which the Inbox protocol has no key
for; that answer is only available in the pane. Answering in the pane first
drops the Inbox item; answering in the Inbox dismisses the pane's prompt.
If you are looking at the pane (or the hold times out) nothing is sent and the
pane's own prompt decides.

**New panes from k3code.** `/bg --pane <prompt>` and `/fork --pane` start the
session in the daemon and open `k3code attach <session>` in a new pane beside
it (outside k3 panes they are a plain `/bg` / `/fork`). With
`autonomy.fanout.panes: true`, every fan-out child gets a read-only pane
(`k3code tail <subagent-id>`). `k3code attach <id> --readonly` is the same for
any session: prompts and approvals from that pane are refused.

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
