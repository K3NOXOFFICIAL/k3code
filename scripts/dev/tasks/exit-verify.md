# exit-verify: automate the M0–M6 exit criteria and run them

`docs/PLAN.md` lists the exit criteria for each milestone. Build `scripts/exit/` as runnable checks plus a runner, `scripts/exit/run_all.sh`. The runner writes `docs/reports/exit-status.md`, a table with one row per criterion: criterion, how it was checked, PASS/FAIL/PENDING and evidence, meaning the command output tail.

## Rules

- **Fake provider.** Use the fake provider (`K3CODE_FAKE_PROVIDER`) wherever a criterion is about harness behaviour.
- **Live models.** Use them only where the criterion is about model quality: `/review` finding a seeded bug, a live `/preview` under 30 s, and `/ultraresearch` citations.
  - Use the OmniRoute config at base URL `http://<omniroute-host>:20128/v1`, with key `OMNIROUTE_API_KEY` from the env, which is set.
  - If OmniRoute answers 429 "daily usage quota", mark the check PENDING with the reset time. Do NOT use any other paid provider.
- **No real system changes.** Use temp `HOME` / `K3CODE_HOME` only. Never install the systemd service on this machine. Never toggle the real network (`nmcli`); use the existing flaky proxy chaos scripts.

## Checks to implement

### M0
Re-run the M0 checks:
- a headless read/edit/bash task, using the fake provider for the mechanics and live if available;
- failover on a blocked primary;
- `vendor_check`;
- `~/.hermes` untouched (no files newer than the run start).

### M1
- Build a pexpect harness (extend `scripts/e2e_tui_file.py`) that drives the real TUI, with the gateway running the fake provider, through these flows:
  - plan mode: Shift+Tab to plan, a write is refused, then `exit_plan` approval;
  - ask/allow/deny with "always";
  - `/compact`, `/model`, `/effort`, `/resume`, `/rename`, `/fork`, `/branch` (temp git repo).
- Export/import round trip into a second temp home.
- `/goal` reaches done.
- The strip shows a `/bg` session going from working to completed.
- A needs-input approval is answered inline.
- Focus mode hides tool output. Assert on the screen text.
- `/review` finds a seeded off-by-one bug: **live**, or PENDING.

### M2
Chaos, via the proxy:
- (a) an invalid primary key fails over to the secondary within 30 s, and `/stats` shows the failover;
- (b) proxy down mid-goal gives paused, proxy up resumes within 10 s;
- (c) `kill -9` during bash gives INTERRUPTED and no re-run;
- (d) a 429 with Retry-After 120 parks then resumes; use the fake upstream with a 5 s Retry-After for speed and note that;
- (e) a provider down while the internet is up gives PROVIDER_DOWN with failover, never paused;
- (f) the soak: add `scripts/exit/soak.sh`, which starts a daemon in a temp home with a `/loop` and 2 cron jobs on the fake provider and logs memory, lost turns and errors every 5 minutes. **Run it for 30 minutes here** and mark 72 h as PENDING. Print how to continue it: `nohup scripts/exit/soak.sh --hours 72 &`.

### M3
- `go test` for k3keys and harness.
- Hint bar correctness via a k3keys test.
- Upstream tuios tests with `keymap=tuios`.
- Pane badges update within 1 s: use the live panes demo from `docs/k3-panes-test.md` and measure.
- After a tuios daemon restart, the panes resume.
- Inbox approval shows up.
- The hallway test is PENDING, as it needs a human.

### M4
- **Eval set.** Create `scripts/exit/scope_eval.jsonl` with 30 diverse tasks, each labelled with a **proposed** scope. Mark labels as `"label_source":"proposed-by-claude"`; the owner must confirm them. Run the scope classifier **live**, or PENDING.
- **Fan-out.** HUGE tasks produce a fan-out whose workers merge and pass. Use the fake demo, `demo_ultracode.py`.
- **Tiers.** `/stats` shows background, cron and loop turns on cheap tiers.
- **Degradation cost.** Compare token costs using the fake provider with a cost table; a live comparison is PENDING.
- **`/preview`.** Under 30 s live, or PENDING.
- **`/ultraresearch`.** At least 10 resolving citations live, or PENDING.
- **Missed jobs.** A job missed while offline fires exactly once.
- **MCP context.** MCP schemas stay under 15% of the context with deferred loading. Measure prompt tokens with a fake MCP server that has 300 tools.

### M5
- Dismissed proposals never recur.
- No config change without acceptance or an allowlisted key: audit the config backups and decision log during the scripted demo.
- A planted bad overlay is auto-reverted.
- "After 2 weeks of real use: at least 3 accepted rules and at least 40% fewer prompts" is PENDING (needs real use).
- A mem0 preference changes behaviour: mocked test PASS; live is PENDING.

### M6
- **Fresh install.** Install into a **fresh Fedora 44 container**:
  - `podman run --rm -v <repo>:/src:ro,Z registry.fedoraproject.org/fedora:44 sh -c '…'`;
  - inside it, install git, curl, gcc and other basics with `dnf`, copy `/src` to `/tmp/k3code`, run `sh install/install.sh --from-source --yes --no-setup`, then run `k3code setup --non-interactive --answers install/answers.sample.yaml --no-probe`;
  - **time it**, then run `k3code doctor`;
  - the target is under 10 minutes, excluding the `dnf` and image pull;
  - pulling the image is allowed;
  - if networking or a download prevents this, mark the check PENDING with the error.
- `--from-bundle` restore.
- Interrupted setup resumes.
- A broken update rolls back.
- The upstream-sync dry run:
  - write `scripts/sync-upstream.sh --dry-run`, which fetches tuios and hermes-agent upstream into temp dirs and computes a 3-way diff against the recorded base commits in `VENDOR.toml` / `panes/UPSTREAM.toml`;
  - report the number of conflicting files;
  - the target is fewer than 10 per subtree.

## Acceptance
- `scripts/exit/run_all.sh` runs end to end (allow about 60 min including the 30 min soak) and writes `docs/reports/exit-status.md`.
- Every row is PASS, FAIL or PENDING, with concrete evidence and, for PENDING rows, the exact action that would close it.
- Fix any FAIL that is a bug in k3code and rerun. Mark it FAIL only if it can't be fixed in this task, and explain why.
- Commit everything, plus a short `REPORT.md`.
