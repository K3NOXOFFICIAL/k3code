# Long-run audit, 2026-10-07

The M2 72 h soak was started on the fake provider and **died after 2 h 05 min**. That was the first of
several bugs that unit tests and a 30-minute soak could not see. This report records how they were found,
what was fixed (every fix has a regression test that fails on the old code), and what is still open.

## How the bugs were found

1. **The 72 h soak** (daemon + a `/loop 30s` + two cron jobs on the scripted fake provider) failed at turn
   ~490: `RecursionError` in `persistent_retry.observed`. See bug 1 below.
2. **A fast soak** (`scripts/exit/soak.sh --loop-every 1`, new): 1 s loop ticks reach thousands of turns in
   minutes. Its first run exposed that the soak harness itself was wrong (a fixed-rate schedule check on a
   fixed-delay loop; the fake provider's request log retaining every conversation, i.e. quadratic RSS).
   After the fixes it ran **1,817 turns, 0 lost, 0 errors, RSS +7.5 MB**.
3. **Live-path reading**: following one tool-using session through the real OpenAI-compatible wire format
   found bug 3.
4. **A 9-area read-only audit** by independent reviewers (gateway, router/providers, reliability, automation,
   autonomy/sub-agents, tools/permissions, learning/MCP, install/update, TUI), each told to *reproduce* what it
   reports, followed by skeptic verifiers on the top findings. About 40 findings, almost all reproduced.

## The three soak/live bugs

| # | Bug | Effect | Fix |
|---|-----|--------|-----|
| 1 | `PersistentRetry` re-wrapped the shared `router.on_event` once per `AgentLoop` (twice per turn) | daemon died with `RecursionError` after ~1000 wrappers (about 2 h of a 30 s loop + a 1-min cron) | one shared observer per router, listeners held by weak reference; `attach_router` idempotent |
| 2 | the per-session `LoopGuard` was never reset per turn | identical replies in separate turns (a `/loop` answering "nothing changed") counted as a doom loop: corrective note, `needs_input`, cheap→main escalation on ~40 % of ticks | `Reliability.begin_turn()` resets it |
| 3 | `LiveSession.history` rebuilt messages without their `tool_calls` | **turn 2 of every tool-using session sent an orphan `tool` message; OpenAI-compatible and Anthropic APIs answer HTTP 400.** The fake provider and the claude-cli provider (flattens to text) hid it | `history` restores tool calls; `normalize_tool_pairs` repairs any pairing at the wire boundary; the chaos fake upstream now enforces the real rule and `test_strict_upstream_e2e` drives a 4-turn session through the real provider |

## Fixed from the audit (all with tests)

**Data loss / safety**
- bash tool: unbounded output buffering (a `cat` of a 324 MB log took the daemon to 656 MB; `yes` ~1 GB/s → the OOM killer takes every session), orphan process groups on `/stop`, hanging timeout path. Now bounded streaming capture, kill past 64 MB, whole-group kill on every exit path.
- file tools: `edit` rewrote CRLF as LF, replaced non-UTF-8 bytes with U+FFFD, wrote non-atomically; `read` line numbers disagreed with editors; `grep` treated one unreadable dir as failure. Now byte-preserving, atomic, strict.
- hardline / permissions: `ls & rm -rf x` was one command matching `ls *`; `rm -fr /`, `$HOME`, quoted `ssh protected-host-a "… && docker restart …"`, `$(…)`, backticks, `bash -c` all bypassed it; an "always allow git commit" also allowed `> ~/.bashrc`. New quote-aware shell parser; token-based `rm` check; redirects/substitutions void allows at every layer (project-internal redirects stay allowed); `rg --pre` / `git diff --output` ask; launchers (sh, ssh, python, sudo, xargs, find…) are never offered as "always allow".
- sandbox: a failed bwrap probe latched the sandbox off for the daemon's lifetime and blocked the event loop; the daemon's environment (provider API keys) reached sandboxed commands. Now re-probed after 60 s in a thread, `--clearenv` + allow-list, `--unshare-ipc`.
- sub-agent merges into the user's checkout: unserialized; the unconditional `git merge --abort` destroyed the user's own merge. Now per-repo lock, refuses while a merge is in progress, never aborts a merge it did not start, `git` is killed on cancel.
- in-flight turn lost on `/stop`, SIGTERM and any crash (store written once, at the end; `CancelledError` skipped the persist). Now `turn_messages` tracks the turn, persist is in `finally`, checkpoints after the tool call and after every tool result (3 s throttle), `close()` waits for cancelled turns before closing the store.
- `/update now` deleted the live install; the in-daemon update restarted its own unit and could never roll back; `doctor` ignored `~/.config/k3code/env`. Now guarded, run through a transient `systemd-run --user` unit, fixed.
- export bundles / `/debug` dumps leaked `DB_PASSWORD=…`, Authorization/Cookie headers, `?api_key=…`, `--token x`, PEM keys, MCP env/headers. Redaction widened.

**Daemon stability**
- `/stop` poisoned its session (`CancelToken` never cleared: every later turn "interrupted" with no model call).
- budget guard raised `TypeError` instead of `BudgetExceeded`; the "day" budget was per session and never reset (now a process-wide ledger rolling at local midnight); sub-agents ran with no budgets at all.
- providers accepted a truncated or in-band-error stream as a complete answer; the router replayed text after a mid-stream retry (new `reset` stream event); a 400 about `max_tokens` aborted the whole fallback walk.
- gateway transport: a command waiting for the client's answer (clarify) deadlocked its own connection; a stalled peer made every emit O(backlog) forever; graceful stop hung in `wait_closed()` until systemd's SIGKILL; a prompt sent mid-turn was answered "queued" and dropped; `session.resume` hid that a turn was running.
- one NetWatch (two forever-tasks) per live session even when idle, and re-subscribed on every re-arm; providers/cooldown store/routers rebuilt whenever sessions alternated between model keys.
- MCP: one cancelled call killed the server for every session; a dead or boot-time-down server never came back; calls ran one at a time.
- second `k3code daemon` stole the live socket; now flock + live-socket probe + inode-checked unlink.
- park ladder and Retry-After never reset in a session's life (every later blip parked 10 min).
- automation: `/stop` on an unattended turn killed its own supervisor (cron job re-fired at once, loops died while "active"); webhook actions were cancelled after 10 s; goal automations leaked a session and a NetWatch per fire; two runs on one session interleaved; supervisors died on one exception; run history and unattended sessions grew without bound; `cron_next` went 55 min into the past in the DST fall-back hour (2026-10-25).
- sub-agents: `/stop` ignored (`wait()` swallowed the waiter's cancellation), failed children leaked a checkout.
- TUI: proposal cards were accepted/dismissed by the first letter `a`/`d` of any message (now Alt+A / Alt+D) and accept also sent the action to the model for already-applied kinds; `/new` and `/clear` never released the old session; the agent strip drew running sub-agents as "completed".

## Still open (known, not fixed)

- `CooldownStore` instances sharing `cooldowns.json` can overwrite each other's arms (one per provider config now, but one-shot routers still share the file).
- The cooldown exponential ladder is dead code (`backoff_count` is never passed).
- `ModelCaller` labels usage rows from a process-global "last router attempt" (concurrent sessions can mislabel rows).
- Optimizer A/B experiments are judged per turn rather than per session, and rollback does not revert the live config in memory.
- A loop's DB state does not follow its task when the task ends for an unexpected reason.
- The daemon does not resume `journal/<sid>.messages.json` after a hard kill (the periodic checkpoint makes the loss seconds, not the turn).
- The sandbox still fails open when bwrap is unusable (documented; `/doctor` warns). Making unattended modes fail closed is a policy decision.
- Idle detached interactive sessions are not evicted server-side (the idle sweeper only stops their NetWatch).
- TUI: `$stripSessions` is not cleared when the connection drops.

## How to re-run the checks that found these

```
scripts/exit/soak.sh --minutes 30 --interval 60 --loop-every 1   # fast soak: ~1,800 turns in 30 min
scripts/exit/run_all.sh --only m4                                  # partial runs now merge into exit-status.md
cd core && uv run pytest                                           # ~700 tests incl. the regressions above
```
