# Exit-criteria status: M0–M6 (as of 2026-10-07 14:30)

This page collects evidence that was already verified in this session; the worker and merge reports in `docs/reports/` hold the details.

The automated runner (`scripts/dev/tasks/exit-verify.md`) has **not** completed yet. Claude Code stopped it because the laptop was low on memory, and it may only be restarted on request.

| Status | Meaning |
|---|---|
| PASS | Verified, with the evidence named in the table. |
| PASS (fake) | Verified with the scripted fake provider; the live model was not used. |
| PENDING | Not verified yet; the last column names the action that closes it. |

## M0: scaffold

| Criterion | Status | Evidence / closing action |
|---|---|---|
| A read/edit/bash task completes through OmniRoute | PASS | `m0-exit.md`: a live task returned `hello, k3!`; the strict TUI e2e test created `e2e.txt` with `works` |
| A blocked primary fails over | PASS | `m0-exit.md`: network → failover → tailnet entry returned 200 |
| vendor_check | PASS | It passes in every merge report (latest: `merge-m5.md`) |
| `~/.hermes` untouched | PASS | 0 newer files (`m0-exit.md`) |

## M1: daily driver

| Criterion | Status | Evidence / closing action |
|---|---|---|
| Plan mode, approvals (once/session/always/deny), modes | PASS (fake) | `m1-permissions.md`: gateway round-trip tests; the TUI e2e test reported `approval-prompts-answered=2` |
| `/compact /model /effort /resume /rename /fork /branch` | PASS (unit/gateway) | `m1-gateway.md`, `m1-commands.md`. A scripted run of each command in the real TUI is PENDING: exit-verify runner |
| Export → import round trip | PASS | `m1-commands.md`: 0 secrets in the bundle, and the settings and sessions were re-imported |
| `/review` finds a seeded bug | PENDING | Needs the live model after the OmniRoute quota resets (about 2026-10-08 05:00) |
| `/goal` reaches done | PASS (fake) | `merge-m4a.md` and `m1-commands.md` goal tests |
| The strip shows a `/bg` session going working → completed; needs-input is answered inline | PASS (unit) | `m1-tui.md` and `m4b-fanout.md`. A screen-level run in the real TUI is PENDING: exit-verify runner |
| Focus mode shows only the essentials | PASS (unit) | `m1-tui.md` focusPolicy table tests |
| the owner uses it for 3 days without blocking bugs | PENDING | Needs real use by the owner |

## M2: running 24/7

| Criterion | Status | Evidence / closing action |
|---|---|---|
| A bad key fails over to the secondary within 30 s | PASS (unit) | `merge-m4a.md` router cooldown tests. A proxy chaos run is PENDING: runner |
| Offline mid-goal pauses, and it resumes within 10 s once online | PASS (fake) | `m2-reliability.md` and my own rerun of `offline_pause.sh`: PASS |
| `kill -9` during bash gives INTERRUPTED and no re-run | PASS | My own rerun of `kill_during_bash.sh`: PASS |
| A 429 with Retry-After parks, then resumes | PASS (unit) | `merge-m4a.md`: a short Retry-After waits inline, a long one fails over or parks until the reset |
| An OmniRoute outage gives PROVIDER_DOWN, never a pause | PASS (unit) | `m2-reliability.md` netwatch tests |
| 72 h stability run | PENDING | `scripts/exit/soak.sh --hours 72` (written by the runner) |

## M3: k3 panes

| Criterion | Status | Evidence / closing action |
|---|---|---|
| Ctrl+G modes, with the hint bar always visible | PASS | `m3-keys.md`: 22 k3keys tests; build and vet clean |
| Upstream tuios tests pass | PASS | `internal/input` and `internal/app` pass after the hooks (my own run) |
| Pane badges update within 1 s | PASS (live, isolated tuios) | `m3-panes.md`: working → done seen through `tuios list-agents` |
| Panes resume after a tuios daemon restart | PENDING | exit-verify runner |
| ACP/Inbox approval | PASS (live display) / PASS (fake answer) | `m3-panes.md`: the Inbox item appeared; answering it needs the person's attach nonce |
| First-time user drives panes from the hints alone | PENDING | Needs a person |

## M4: autonomy

| Criterion | Status | Evidence / closing action |
|---|---|---|
| The scope verdict agrees with the owner's labels on at least 80% of 30 tasks | PENDING | Needs the owner's labels plus the live classifier |
| HUGE tasks fan out, merge and pass | PASS (fake) | `demo_ultracode.py`: 3/3 merged, tests pass (`m4b-fanout.md`, `merge-m4b.md`) |
| Background, cron and loop turns use cheap tiers | PASS | `merge-m4c.md`: `/stats` shows `cron_job` work on the cheap tier |
| Degradation cuts cost by at least 30% at the same pass rate | PENDING | Live comparison after the quota resets |
| `/preview` under 30 s | PENDING | Live after the quota resets |
| `/ultraresearch` with at least 10 citations | PENDING | Live, plus a reachable search source |
| A job missed while offline fires exactly once | PASS (unit) | `m4c-automation.md` scheduler tests |
| MCP schemas stay under 15% of context | PENDING | exit-verify runner (300-tool fake server) |

## M5: learning

| Criterion | Status | Evidence / closing action |
|---|---|---|
| Dismissed proposals never come back | PASS | `demo_m5.py` (`m5-learning.md`) |
| No config change without acceptance | PASS (unit) | `m5-learning.md`: update-config and optimizer tests |
| A bad overlay is rolled back automatically | PASS (unit) | `m5-learning.md`: optimizer A/B rollback test |
| At least 3 accepted rules and 40% fewer prompts after 2 weeks | PENDING | Needs 2 weeks of real use |
| A mem0 preference changes behaviour | PASS (mocked) / PENDING (live) | Live mem0 check after the quota resets |

## M6: install

| Criterion | Status | Evidence / closing action |
|---|---|---|
| A fresh device is set up in under 10 min | PASS (temp HOME) / PENDING (clean Fedora 44 container) | `m6-install.md`: a real from-source install into a temp HOME; the podman container run is in the exit-verify runner |
| `--from-bundle` restore asks only for secrets | PASS (unit) | `m6-install.md` |
| An interrupted setup resumes | PASS | `m6-install.md` `test_resume_after_interrupt` |
| A broken update rolls back | PASS (unit) | `m6-install.md` |
| Upstream-sync dry run shows fewer than 10 conflicting files | PENDING | exit-verify runner (`sync_upstream.py`) |

## What closes the remaining rows

1. **Free memory, then the owner says "restart the exit verification".** This closes all runner-marked rows.
2. **After the OmniRoute quota resets (about 2026-10-08 05:00).** This closes the live rows: `/review`, `/preview`, `/ultraresearch`, degradation cost and the mem0 behaviour check.
3. **the owner.** Confirm the 30 scope labels, run the hallway test, and use k3code for 3 days and then 2 weeks.
4. **The 72-hour soak.**
