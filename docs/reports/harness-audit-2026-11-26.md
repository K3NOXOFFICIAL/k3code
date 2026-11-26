# Harness audit, 2026-11-26

Branch `harden/harness-audit`, base `origin/Main` `dc13baf0`.
Baseline before any change: `uv run pytest -q -n auto` in `core/`: 9585 passed, 24 skipped, 0 failed; `ruff check` clean.

Each item was treated as a hypothesis and checked against the source by a read-only probe.

## Confirmed vs unconfirmed

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| 1 | Prefix-glob bash rules can be bypassed by compound commands | PARTIAL, real bypasses found | A shell splitter already exists (`permissions/hardline.py` `parse()`), and `;` `&&` `\|\|` `\|` `&` newline `$()` backticks, env prefixes, `eval`, `bash -c`, subshells, heredocs and `>`/`>>` redirects all already ask. Still bypassed: process substitution `ls <(rm -rf x)` was **auto-allowed by the built-in rules alone**, because `_SUBSTITUTION` (`permissions/engine.py:797`) matched only `$(` and backticks. Read-write redirect `echo x <>~/.bashrc` was allowed under a user `echo *` rule (`_REDIRECT`/`_WRITE_REDIRECT`, `engine.py:793-796`). A user rule `mount *` allowed `mount -o remount,rw /`. |
| 2 | `ask_user_question` names a missing `exit_plan` tool | PARTIAL | `exit_plan` is registered (`tools/__init__.py:1689-1703`), but `agent/loop.py:226-230` hides it outside plan mode, while `ask_user.py:43-44` always names it. Also `tools/fuzzy_match.py:354,423,451` tells the model to use `write_file`/`read_file`, which do not exist (the tools are `write`/`read`). |
| 3 | Guidance conflicts (big steps vs smaller steps, 30 s timeout, terse vs Tips) | PARTIAL | A precedence line exists (`prompts/system.md:13`), but "Few, large steps" (`system.md:25`) and the Tips footer (`system.md:21`) were unconditional, the `concise` style (`outputstyle.py:11-14`) did not drop Tips, and the prompt suggested whole test suites as one bash call with a 30 s default timeout (`tools/__init__.py:485-529`). |
| 4 | 80+ skill descriptions injected into every prompt | NOT REPRODUCIBLE | Listing is ranked and capped at `PROMPT_LIMIT = 30` (`skills.py:16`, `skills_prompt` `skills.py:196-215`), with a `skill` tool that searches by `query` (`extratools.py:46-76`). No load-always flag exists. Measured on this machine: 73 skills found; injected section 3417 chars (about 854 tokens, chars/4); listing all 73 would be about 33.8k chars (about 8.4k tokens). |
| 5 | Contradictory environment info when layered on another backend | NOT REPRODUCIBLE | Environment text has one source, `prompting.scope_prompt` (`prompting.py:49-98`): cwd, git root, added dirs. No date, branch or model identity is sent. The `claude_cli` backend is told to ignore its host's sandbox environment (`providers/claude_cli.py:66-71`), and this is tested (`tests/test_claude_cli_provider.py:273,791-794,836`). Gap, not fixed: no date is supplied at all. |
| 6 | Tool-call protocol: weak validation, no retry hints | PARTIAL | Native tool calling for `anthropic` and `openai_compat`; a text `<tool_calls>` block only for `claude_cli` (`providers/claude_cli.py:59`). Validation exists (`tools/validate.py`) but only at the top level. Unknown tool returned only `Unknown tool: X` (`agent/loop.py:575`). No system-reminder retry hint after a malformed call. |
| 7 | Memory preferences lack structure, dedupe, inspect/edit/delete | PARTIAL | `learning/preferences.json` had key, text, confidence, evidence (`learning/distiller.py:338-365`) but no timestamp or expiry, no text dedupe across keys, and no edit. `/learned` list/forget/purge existed (`commands/learned_cmd.py`); no CLI equivalent. |
| 8 | Truncation does not say how to get the rest; unclear read-only .git error | PARTIAL | Read/grep/glob hints are fine. Web fetch/browse appended the cut marker and then sliced it off (`research/tools.py:277-279,293-295,680-682`). Bash capture drop gave no hint (`tools/__init__.py:718`). Git errors were raw; key `sandbox.git_writable` exists (`reliability/sandbox.py:108-120`) but no tool result named it. |
| 9 | No regression eval suite | CONFIRMED | Only per-tool corpora (`tests/test_tool_call_robustness.py`, permission/classifier corpora) and live-model scripts (`scripts/exit/`). Offline fakes exist (`providers/fake.py`, `tests/test_agent.py` FakeProvider). |

## What changed (branch `harden/harness-audit`, base `dc13baf0`)

| Item | Change | Evidence it works |
|---|---|---|
| 1 permissions | Closed: process substitution `<(..)`/`>(..)` riding on an allow (builtin rules alone allowed `ls <(rm -rf x)`), `<>` / `>|` / `&>|` / `>>|` redirects, `mount -t` and `findmnt` values the shell expands, `$IFS` / brace / `$[..]` expansion in command names and arguments, `$(..)` output feeding allowed commands, yolo fail-opens for over-cap and re-expanded payloads. `mount` is auto-allowed only as a read-only listing. | Each fix shipped with tests that fail without it (reverse-patch gate). About 1100 new permission tests. |
| 1 (user request) | Read-only inspection commands run without a prompt: `free ps top uptime lscpu lsusb lspci sensors hostname ip ss nvidia-smi lsblk systemctl journalctl`, and read-only commands outside the project (secret-looking names and trees always need a human, in every mode). Write forms still ask. | `tests/test_permission_sysinspect.py`, `..._systemd_readonly.py`, `..._net_gpu_readonly.py`, `..._outside_reads.py`. |
| 2 | `ask_user_question` names `exit_plan` only conditionally; `read_file`/`write_file` in edit errors now say `read`/`write`. | `tests/test_tool_name_refs.py`: scanner found 2 unknown names before, 0 after. |
| 3 | Defaults (step size, Tips footer, timeouts) yield to the user, USER.md and output style; precedence written once in `README.md` ("Guidance precedence"); `concise` style drops Tips. | Base prompt 1055 -> 1092 tokens (cap 1100). |
| 4 | Not reproducible. Listing already capped at 30. Test added. | 73 skills here; injected section ~854 tokens vs ~8.4k if all were listed. |
| 5 | Not reproducible (one source: `prompting.scope_prompt`). | See table above. |
| 6 | Unknown tool: close-match hint + tool list; invalid block echoes its start; nested argument validation; one retry-hint reminder after a malformed call. | New tests in `test_tool_call_robustness.py`, `test_agent.py`. |
| 7 | Preferences keep `first_seen`/`updated`, expire after 180 days, dedupe by text, `/learned edit`, `k3code learned list|forget`. | 7 new tests. |
| 8 | Web fetch/browse always show a cut marker; bash capture drop says how to get the rest; read-only `.git` failures name `sandbox.git_writable` and the config path. | 9 new tests. |
| 9 | Offline eval suite: 16 scripted tasks, pass rate and steps per task (`core/scripts/eval_score.py`). | Branch 16/16, mean 2.81 steps; original code 14/16 (the two malformed-call tasks fail). |

## Verification (final tree `13cbc6ea`)

- `cd core && uv run pytest -p no:cacheprovider -n auto`: **12461 passed, 24 skipped, 0 failed** (baseline before changes: 9585 passed, 24 skipped).
- `uv run ruff check . ../scripts` and `ruff format --check`: clean. `tests/test_release_export.py` passes. No absolute home path or tailnet address in the diff; `gitleaks`: no leaks.
- `uv run pytest tests/test_agent_evals.py -s`: `eval pass rate: 16/16, mean steps: 2.81` (original code: 14/16).
- Each permission fix ships with tests that fail without it (the fix is set aside as a patch, the new tests must fail, the fix is restored).
- **Security review history (fresh read-only reviewer each time): rounds 1 to 6 all returned FAIL.** Every finding from rounds 1 to 6 was fixed with tests. Round 7's reviewer ran out of context before it probed anything, so there is no verdict; I probed its suspicions myself with `decide()` (script kept out of the repo), found two real classes (glued/clustered option values; launcher variants such as `flock --wa 5`, `tmux split`, `gdb -ex shell`, `npm exec`), and fixed both structurally. **That last fix round has not been re-reviewed by an independent reviewer.**

## NOT fixed, and why

- **Docker/podman bind mounts**: `docker run -v ~/.ssh:/x` (and `--mount ...src=`) is not scanned for secret sources; the builder ran out of context.
- `cd DIR && cat shadow`: names are resolved against the decide cwd, not the `cd` target (secret-name check on a bare relative name).
- Auto mode: `cat src/$X` with `X=../../..` assigned on the same line allows (as on base); `cat $(...)` and backtick substitution words keep the old behaviour.
- Readers not on the builtin list (`less`, `base64`, `xxd`, `awk`, `sed`) on secret paths get a plain ask, which auto mode answers.
- Recursive listings (`ls -R ~`, `du -a ~`, `tree -a ~`, `find ~ -name id_rsa`, `grep -r x ~`) can reveal names under dot folders that `ls ~/.ssh` protects.
- `busctl get-property ... Environment` and `systemd-analyze dump` leak unit environments (plain ask).
- `_abs` resolves `link/../x` textually: `cd -P lnk/..; echo x > f` allows (write outside the project through a symlink).
- Digit-suffixed secret names (`server.pem1`) are not matched by the name filter; `firejail dd if=/dev/sda` is exempt through `strict=False`; `xterm -e rm -rf ~` is not caught by the new scan (`-e` values are skipped to spare `grep -e`).
- `nohup foo 'cd /; rm -rf *'`: a `cd /` inside a quoted payload is not followed (as on base).
- Over-asking accepted: `/etc/krb5.conf`, `tokenize.py`/`secrets.py` under `/usr`, `/sys` reads; `git commit -m 'block /etc/shadow'`, `rg -n '/etc/shadow' src`, `git log -S/etc/shadow` and `docker run -v /var/run/docker.sock:/x` now force a human because the word scan sees a secret path; `ls ~/.cache`, `du -sh ~/.cargo`, `ls ~/.local/share` ask; `cd /tmp && echo x > a` asks in default and accept-edits (as the direct `> /tmp/a` does); `echo x > f; cd ..` over-asks; brace words over 4096; a glob budget can make a large harmless glob outside the project ask.
- Item 6: no text-JSON tool-call fallback for local models (not required). Item 4: cold-start ranking is alphabetical.
- Flaky tests, unexplained: `test_sandbox_git.py::test_a_symlinked_worktrees_dir...` and `test_panes.py::test_bg_pane_sends_start_agent` fail occasionally under `-n auto` and pass alone and on re-run; `test_cli_headless_output.py::test_a_headless_answer_ends_with_a_newline` failed once with `No module named 'k3code.providers.retry_utils'` (module present, passes alone). None touches the permission code; none was checked against the base commit.

## Residual risk

The permission engine is a heuristic shell parser, not a proof. Seven review rounds each found new ways around it, mostly wrapper programs and operator spellings nobody had listed; the last rounds replaced listing by structural scans (every argument word, every command run), which should shrink the tail but is not independently confirmed. Default and accept-edits modes run bash unsandboxed, so a parser miss there is an unprompted host action. Treat the new read-only allowances as a convenience, not a boundary: prefer auto mode (bwrap) or a human prompt for anything sensitive, and consider running bash in the sandbox in every mode. Commits carry the author `Claude <noreply@anthropic.com>` because the repo has no git identity configured.
