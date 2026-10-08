# k3 panes: live and manual test

## Automated live check (headless, isolated tuios daemon)

Uses a private daemon (own `HOME` and `XDG_RUNTIME_DIR`), so it never touches your own tuios sessions.

```sh
cd panes && go build -o /tmp/tuiosbin ./cmd/tuios
export LIVE=" HOME=/tmp/k3live/home XDG_RUNTIME_DIR=/tmp/k3live/run XDG_CONFIG_HOME=/tmp/k3live/cfg XDG_STATE_HOME=/tmp/k3live/cfg XDG_DATA_HOME=/tmp/k3live/cfg"
mkdir -p /tmp/k3live/{home,run,cfg,work}; chmod 700 /tmp/k3live/run
(env $LIVE /tmp/tuiosbin daemon &)                       # a private daemon
cd ../core
env $LIVE /tmp/tuiosbin start-agent -s live --name k3pane --cwd /tmp/k3live/work \
  --env LIVE_TMP=/tmp/k3live/work --ready-timeout 300 \
  "$PWD/.venv/bin/python $PWD/scripts/live_panes_demo.py"
watch -n0.5 "env $LIVE /tmp/tuiosbin list-agents -s live"   # none -> working (thinking) -> done
env $LIVE /tmp/tuiosbin list-attention -s live             # the finished turn is in the Inbox
env $LIVE /tmp/tuiosbin kill-server
```

`scripts/live_panes_demo.py` runs a real `GatewayServer` (fake provider, 4 s turn) with the panes tap on its frames.

## Manual: the full TUI

1. Start `k3code daemon` in one terminal.
2. In tuios/k3 (`k3`, or `tuios`), put `[agents.approvals] enabled = ["k3code"]` in the tuios config.
3. In a pane: `k3code attach <session-id>` (get ids from `k3code` > `/sessions`), or open k3code normally and type
   `/bg --pane summarise this repo`. A new pane opens running `k3code attach <id>`.
4. Badges: `Ctrl+G a` shows the agents mode; the pane title shows working / done; `Ctrl+G a i` opens the Inbox.
5. Approvals: in the new pane ask for something that needs permission (default mode, `run rm -rf build/`), then
   focus another pane. The approval shows in the Inbox: `1` once, `2` always, `3` deny. The pane's prompt disappears.
6. `/fork --pane` forks the session into a new pane. With `autonomy.fanout.panes: true` in `~/.k3code/config.toml`,
   an auto fan-out opens one read-only `k3code tail <subagent>` pane per child.
7. `k3code attach <id> --readonly`: typing a prompt is refused with "this pane is read-only".

Not covered live (needs the real Ink TUI, node and a provider): steps 3 to 7. They are covered by
`core/tests/test_panes.py` against a fake tuios socket.
