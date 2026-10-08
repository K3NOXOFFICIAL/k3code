# M4c-automation: `/loop`, `/schedule` (cron), automations and event triggers, all running 24/7 in the daemon

## Current state

`core/` has:
- the gateway: multi-session and multi-client, with a Unix-socket daemon (`k3code daemon`) and background sessions;
- the reliability stack: netwatch, persistent retry with offline pause/resume, journal, governor with budgets and PSI caps;
- `/goal`, a port of Hermes' goals;
- the command registry and the usage db.

Read `docs/reports/m2-ops.md`, `docs/reports/m1-commands.md` and `docs/reports/merge-m1-commands.md` first.

Reference, read-only, MIT: Hermes Agent at `~/.hermes/hermes-agent`:
- `hermes_cli/loops.py`
- `cron/jobs.py` (schedule math: `compute_next_run`)
- `cron/unreachable_retry.py`
- `cron/quota_hold.py`
- `cron/suggestions.py` and `suggestion_catalog.py`

Port the designs. Vendor only small, self-contained helpers, and record every port or vendored file in `VENDOR.toml`.

## Build

### 1. `/loop`
- **Syntax:** `/loop [interval] <prompt>`. The interval is like `5m`, `1h` or `daily 09:00`. If it is omitted, the loop is **self-paced**: after each tick the model calls a `schedule_next(seconds, reason)` tool, which is clamped to between 60 s and 1 h.
- **Options:**
  - `--times N`
  - `--until "<condition>"`: a cheap-tier judge checks the condition after each tick
  - `--max-ticks` (default 50) as a backstop
- **Behaviour:**
  - Each tick sends the prompt as a user message into the **same** session, which then runs as a background session.
  - Ticks run on the `cheap` tier, using the `loop_tick` task kind if `routing/tiers.py` exists (M4a, merged in parallel). Otherwise use the main tier and leave a TODO.
  - If the model emits the sentinel `LOOP_COMPLETE`, the loop ends.
- **Control:** `/loop status`, `/loop stop [id]`, `/loop list`. Loops survive daemon restarts: persist them in SQLite and resume them on daemon start.
- **Offline:** if offline, ticks are deferred (they use netwatch's `wait_until_usable`). A tick that is missed while offline fires **once** when the network returns. It never fires repeatedly to catch up.

### 2. `/schedule` (cron)
- **Syntax:** `/schedule add "<cron expr or natural language>" <prompt> [--cwd DIR] [--model tier] [--name NAME]`. Natural language such as "every weekday at 9" is converted to cron by the `cheap` tier and confirmed with the user.
- **Other subcommands:** `/schedule list`, `rm <id>`, `pause <id>`, `resume <id>`, `run <id>` (run now).
- **Jobs:**
  - Jobs run in the daemon as background sessions. Each run's result is stored, and a notification is emitted to attached clients.
  - Add the CLI `k3code schedule …`.
- **Reliability rules** (port Hermes' `unreachable_retry` and `quota_hold`):
  - a run that failed for network reasons with zero API calls is retried after 5, 15 and 30 minutes;
  - a provider quota hold parks the job until its reset;
  - a missed run (daemon down, laptop asleep or offline) fires once on recovery if it is still within a grace window (default 6 h); otherwise it is skipped and logged.
- **Concurrency:** the governor caps job concurrency. Jobs default to the sandbox (bwrap) and to permission mode `auto` with hardline denies.

### 3. Automations and event triggers (`core/src/k3code/automation/`)
- An `automations` table: each automation has a trigger, an action and a policy.
- **Triggers:**
  - `cron` (reuses the scheduler);
  - `file_change` (glob plus debounce, via inotify; use `watchfiles` as a dependency);
  - `git` (a new commit on a branch, or a checkout; poll `git rev-parse` every N seconds);
  - `webhook` (an HTTP POST to the daemon on `127.0.0.1:<port>` with a per-automation secret token; disabled unless configured);
  - `session_event` (session completed, failed or needs_input);
  - `net_state` (back online);
  - `idle` (no user activity for N minutes).
- **Actions:**
  - run a prompt in a new or existing session;
  - run a shell command (sandboxed);
  - send a notification;
  - start a `/goal`.
- **Commands:** `/automations list|add|rm|pause|resume|test <id>`. `add` takes a small YAML snippet or a guided wizard through clarify requests.
- **Suggestions** (port Hermes' `cron/suggestions.py`):
  - a catalog of starter automations: nightly test run, morning git summary, dependency update check, "when a session needs input, notify";
  - `suggest()` returns suggestions not yet dismissed;
  - accept creates the automation; dismiss latches its dedup key.
  - Expose them through `/automations suggest`.

### 4. TUI
- Background loop, cron and automation runs appear in the agent strip under the input (they are sessions with state `working` / `completed` / `failed` / `needs_input`).
- A small `⟳` badge in the status line shows the number of active loops and automations.

## Tests (fake provider plus a fake clock: inject a time source, never sleep for real)
- interval parsing; self-paced clamping; `--times`; `--until` with the fake judge; the `LOOP_COMPLETE` sentinel; the max-ticks backstop;
- persistence across a simulated daemon restart;
- an offline tick fires once on recovery;
- cron next-run math (port tests from Hermes if present); the natural-language conversion (fake model);
- network retry backoff; quota hold; the missed-run grace window;
- file_change and git triggers (temp dir and temp repo); webhook auth (wrong token → 401); the idle trigger;
- the suggestions dedup latch.

## Acceptance (put the outputs in REPORT.md)
- `uv run pytest -q -o addopts=""` passes, ruff is clean, and `npm run build` succeeds.
- Live in a temp `K3CODE_HOME`:
  - start `k3code daemon`;
  - add a `/schedule` job with `* * * * *` using the fake provider (`K3CODE_FAKE_PROVIDER`);
  - wait for 2 runs, then show `/schedule list` with the run history;
  - add a `file_change` automation, touch a file, and show that it fired.

  Paste the outputs.
