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

## Verification (final tree `f0d2b57f`)

- `cd core && uv run pytest -p no:cacheprovider -n auto`: **11203 passed, 24 skipped, 0 failed** (baseline before changes: 9585 passed, 24 skipped).
- `uv run ruff check . ../scripts` and `ruff format --check`: clean. `tests/test_release_export.py` passes (the release personal-data scan flagged one comment; fixed).
- `uv run pytest tests/test_agent_evals.py -s`: `eval pass rate: 16/16, mean steps: 2.81`.
- Security review by a fresh reviewer ran three times on the permission work. All three returned FAIL; every finding was fixed with a test that fails without the fix. **The last fix round (outside-read allowlist, `systemctl` connection options, brace cap 4096) was not re-reviewed.**

## NOT fixed, and why

- Quoted literals such as `grep -rn "List<>" src` prompt again; the quote-mask fix is delicate (it must not reopen `bash -c '...'`, `eval "..."`, `awk '{print > "/x"}'`) and three builders ran out of context on it.
- A `cd` earlier on the line is not followed when an expanded command is checked (`cd / && bash -c 'rm${IFS}-rf${IFS}*'` may slip through).
- Auto mode allows `echo x > ../out.txt` (write outside the project; relies on the sandbox, same as plain `>`).
- Allow rules written by the user (for example `sudo *`) skip the forced secret check.
- Readers not on the builtin list (`less`, `base64`, `xxd`, `awk`, `sed`) reading secret paths get a plain ask, which auto mode's classifier answers.
- `grep -r x ~` / `rg x ~` walk home dot folders with a plain ask.
- A brace word with ~1200 single-value groups makes `decide()` raise `RecursionError`. The agent loop turns any exception into a deny; the gateway and server-runner callers of the hardline check were not checked.
- Over-asking accepted: `/etc/krb5.conf`, files named `tokenize.py` / `secrets.py` under `/usr`, `/sys` reads, brace words over 4096.
- Item 5: no date is given to the model; item 6: no text-JSON tool-call fallback for local models (not requested as required); item 4: cold-start ranking is alphabetical.
- `test_sandbox_git.py::test_a_symlinked_worktrees_dir...` failed twice under `-n auto` and passed when re-run; looks flaky, not confirmed.
- Git: git-in-another-repo (`git -C x log`) still asks outside the project because a foreign `.git/config` can run programs (`core.fsmonitor`).

## Residual risk

The permission engine is a heuristic shell parser; three review rounds each found new bypasses, so more probably exist. Default and accept-edits modes run bash unsandboxed, so a parser miss there is an unprompted host action. Treat the new read-only allowances as a convenience, not a boundary, and prefer auto mode (bwrap) or a human prompt for anything sensitive. Commits carry the author `Claude <noreply@anthropic.com>` because the repo has no git identity configured.
