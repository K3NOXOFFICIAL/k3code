# M4c-automation report

## Built
All new code is under `core/src/k3code/automation/` unless noted.

- `clock.py` – `Clock` protocol, `SystemClock`, `FakeClock` (`advance()` wakes sleepers in deadline order; nothing sleeps for real in tests).
- `cronexpr.py` – own 5-field cron matcher + `5m`/`every 2h` intervals + `daily 09:00` (`Schedule`, `cron_next`). No `croniter` dependency.
- `store.py` – `$K3CODE_HOME/automation.db` (WAL; tables `loops`, `jobs`, `job_runs`, `automations`, `suggestions`). The CLI edits it while the daemon runs.
- `runner.py` – the seam to the gateway (`Runner` protocol, `RunResult`, `LOOP_COMPLETE`, `clamp_pacing` 60–3600 s, `--until` judge). `server_runner.py` implements it on the gateway: every unattended run is a background session (auto mode, bwrap sandbox, hardline denies), the `schedule_next(seconds, reason)` tool for self-paced loops, sandboxed `run_shell`, `start_goal`, notifications.
- `loops.py` – `/loop`: `--times`, `--until` (cheap-tier judge), `--max-ticks` (default 50), sentinel, self-paced, persisted in SQLite and resumed on daemon start. Missed or offline ticks fire **once** (next run is always computed from *now*).
- `scheduler.py` + `retry_policy.py` – `/schedule` jobs: run history, `run`/`pause`/`resume`/`rm`, governor slots (`automation.max_concurrent`, default 2), unreachable ladder 5/15/30 min (zero API calls only; resets once the model was reached; failure notice suppressed while a retry is pending), quota hold (retry-after + 60 s slack parks the job), missed-run grace window (default 6 h: fire once, otherwise skipped and recorded in run history).
- `nlcron.py` – natural language → cron via the cheap tier, validated, then confirmed through `clarify`.
- `automations.py`, `triggers.py`, `webhook.py` – triggers `cron`, `file_change` (watchfiles, glob + debounce), `git` (poll `rev-parse`: commit / checkout), `webhook` (127.0.0.1 only, per-automation token, `hmac.compare_digest`, 401/404/405/413, **disabled unless `automation.webhook_port` is set**), `session_event` (completed/failed/needs_input; the daemon's own automation sessions are ignored unless `include_automation: true`), `net_state` (back online), `idle`. Actions `prompt` (new or existing session), `shell` (sandboxed), `notify`, `goal`. Policy: `cooldown_s`, `max_per_hour`, no overlapping runs of one automation.
- `suggestions.py` – port of Hermes' dedup latch (a dismissed or accepted key is never offered again, `MAX_PENDING` = 5) with the four starters: nightly test run, morning git summary, dependency update check, notify on `needs_input`.
- `engine.py` – owns all of the above in the daemon (own netwatch for the offline gate, own governor, `⟳` counts).
- Commands: `commands/loop.py`, `schedule.py`, `automations_cmd.py` (registered in `builtin.py`). `/automations add` takes a YAML snippet, or runs a guided wizard through `clarify`. CLI: `k3code schedule add|list|rm|pause|resume|run`, and `k3code slash "<command>"` (runs any slash command against the daemon socket).
- Gateway (`gateway/server.py`): session `state` now also reports `completed`/`failed` for unattended runs; `_run_turn` returns `(status, text)`; per-session `extra_tools`; `session_event` hook; `last_user_activity`; `broadcast()`; `session.active_list` carries `automation: {loops, jobs, automations, active}`; the daemon starts the engine before READY. `config.py`: `automation:` section (`cheap_model`, `max_concurrent`, `grace_hours`, `webhook_port`, `git_poll_seconds`, `idle_poll_seconds`).
- TUI: strip maps `needs_input`/`failed`/`completed`; `⟳ N` badge in the status rule (`automationCount`, from the existing 1.5 s `session.active_list` poll).
- `VENDOR.toml`: six `port = true` entries (loops, cron/jobs, unreachable_retry, quota_hold, suggestions, suggestion_catalog). Nothing was copied line-for-line. `pyproject.toml`: `watchfiles`.

## Verification
- `cd core && uv run pytest -q -o addopts=""` → `374 passed` (new: `test_auto_cron.py`, `test_auto_loops.py`, `test_auto_scheduler.py`, `test_auto_triggers.py`, `test_auto_gateway.py`; 53 tests). One flaky test (`1` read as an id prefix in the suggestion lookup) was found by repeated runs and fixed; 3 consecutive full runs afterwards were green.
- `uv run ruff check .` → `All checks passed!`. `uv run python ../scripts/vendor_check.py` → `All checks passed` (it WARNs on the pre-existing `modified = true` entries, unchanged).
- `cd tui && npm ci && npm run build` → `built …/tui/dist/entry.js`. `npx tsc --noEmit` is clean.
- Live acceptance (daemon started before the final session-eviction fix; unit-tested separately) in a temp home (`K3CODE_HOME=/tmp/k3acc/home`, `K3CODE_FAKE_PROVIDER=/tmp/k3acc/fake.json`, config with netwatch off), `k3code daemon` running:

```
$ k3code schedule add "* * * * *" say hello --name hello
Scheduled 1b3e1461 “hello” (* * * * *).
$ k3code slash "/automations add {name: watch-py, trigger: {type: file_change, glob: \"**/*.py\", debounce: 0.3}, action: {type: shell, command: \"echo changed {{path}} >> work/fired.log\"}}"
Added automation e824687e “watch-py”.
$ echo "x = 1" > work/demo.py ; cat work/fired.log
changed demo.py
$ k3code slash /automations list
e824687e  watch-py  [active]  file_change {'glob': '**/*.py', 'debounce': 0.3} → shell “echo changed {{path}} >> /tmp/k3acc/work”, fired 1x

$ k3code schedule list
1b3e1461  hello  [* * * * *]  active  next: in 37s  runs: 2
    #3 completed 22s ago  api_calls=1 — cron says hello
    #2 completed 1m ago  api_calls=1 — cron says hello
```

(`/schedule list` through the daemon socket showed the same two completed runs; `/automations suggest` listed the four starters.)

## Deviations
- **Loop ticks use the main tier.** `routing/tiers.py` (M4a) is not merged here, so there is no `loop_tick` task kind. `TODO(M4a)` in `server_runner.py` and `config.py`. NL→cron and `--until` use `automation.cheap_model` (default: `goal.judge_model`, i.e. `cheap`).
- Unattended sessions now report `completed`/`failed` instead of `idle`; four assertions in `tests/test_daemon.py` were updated to `completed`.
- Own cron implementation instead of `croniter` (not installed; smaller dependency surface, fake-clock friendly). DST edge cases are not special-cased (local time via `datetime`).
- `k3code schedule add` accepts only cron/interval/`daily HH:MM`; natural language needs `/schedule add` (in the TUI, or `k3code slash`). `k3code slash` auto-answers `clarify` with the first choice, so a natural-language `/schedule add` through it auto-confirms.
- The `⟳` badge counts loops + cron jobs + automations. It comes from the `session.active_list` poll, not a dedicated event, so it can lag up to 1.5 s.
- Offline gating uses a daemon-level netwatch built from `reliability` config; with `reliability.flags.netwatch: false` jobs and loops run without the offline gate.
- Finished unattended sessions are evicted from `server.live` beyond the newest 20 and their netwatch is stopped (a `* * * * *` job would otherwise leak both); they stay in the session store.
- TUI vitest: 68 suites fail to load because `packages/hermes-ink/dist/entry-exports.js` is missing in this checkout (environment issue, untouched); only `npm run build` and `tsc` were required and pass.

## Open TODOs
- Route loop ticks to the cheap tier once `routing/tiers.py` lands (`loop_tick` task kind).
- `k3code slash` leaves one empty throwaway session per call; add a delete.
- Webhook automations print the token once on creation and store it in plain text in `automation.db` (mode follows the home dir); consider a hash.
- A dedicated `automation.update` event (the badge currently polls); a TUI view for automation run history.
- Real-provider run of the live acceptance (only the fake provider was used).
