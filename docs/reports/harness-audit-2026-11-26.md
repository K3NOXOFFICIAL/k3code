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
| 5 | Contradictory environment info when layered on another backend | NOT REPRODUCIBLE | Environment text has one source, `prompting.scope_prompt` (`prompting.py:49-98`): cwd, git root, added dirs. No date, branch or model identity is sent. The `claude_cli` backend is told to ignore its host's sandbox environment (`providers/claude_cli.py:66-71`), and this is tested (`tests/test_claude_cli_provider.py:273,791-794,836`). Gap found: no date was supplied at all; fixed for the `claude` CLI backend (commit `cabcf462`). |
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

## Verification (final tree `d00cbeb6`)

- `cd core && uv run pytest -p no:cacheprovider -n auto -k "not test_update"`: **12967 passed, 24 skipped, 0 failed** (baseline before changes: 9585 passed). The `test_update_*` tests (about 3 minutes each, they wait on network timeouts) are skipped in this number; they passed in the previous full run (12689 passed in 6 min) and the same slowness shows on the untouched base commit `dc13baf0`, so it is the environment, not this diff (no update code is touched).
- `uv run ruff check . ../scripts` and `ruff format --check`: clean. `tests/test_release_export.py` passes. No absolute home path or tailnet address in the diff; `gitleaks`: no leaks.
- `uv run pytest tests/test_agent_evals.py -s`: `eval pass rate: 16/16, mean steps: 2.81` (original code: 14/16).
- Each permission fix ships with tests that fail without it (fix set aside as a patch, new tests must fail, fix restored).
- **Security review history (fresh read-only reviewer each time): rounds 1 to 6 and round 8 returned FAIL; round 7's reviewer ran out of context before probing (no verdict).** Every finding of every round was fixed with tests. I re-probed round 8's findings myself with `decide()` after the fixes (0 cells not as wanted: container, `file://`, git `-c`, `rg --pre`, `man -P`, `echo | sh`). **The fixes after round 8 have no independent review.**

## Review round 10 (tip `d00cbeb6`): PASS WITH LOW

Independent reviewer, 190 derived-value cases plus 74 everyday commands. Of the derived-value cases the base commit missed 103 and the tip misses 8; every remaining miss was already missed at `dc13baf0`, none is a regression. No exception; worst `decide()` call 1.42 s (200k emoji), everything else 0.71 s or less. Round 9's seven high findings are fixed (glued `-c`, tar exec options, vim/less `!`, env values, `alias` with `!`, `ext::`, `--privileged=true`). First round without a FAIL. Open findings, not fixed:

1. MEDIUM the text-option skip (`-e -m -F -p --file --pattern --message --regexp`) in `hardline.py` applies to every program, so `watch -p rm -rf ~`, `chronic -e rm -rf ~`, `tar -L1 -F 'rm -rf ~' -cf a d` (tar `-F` is `--info-script`) get past the backstop; fix: skip only for text programs.
2. MEDIUM vim filter with an Ex range: `vim -c '%!rm -rf ~' x`, `1,$!`, `.!`, `silent %!`.
3. MEDIUM `sed 's/.*/rm -rf ~/e' f` (the `s///e` flag).
4. LOW-MEDIUM piping to a scheduler: `echo 'rm -rf ~' | at now`, `| batch`, `| crontab -`.
5. LOW ANSI-C quoting `bash -c "$'\x72m' -rf ~"` (yolo allows).
6. LOW over-deny, new: values after `=` that merely mention a destructive command are hard denied in every mode, yolo included: `curl -d 'cmd=rm -rf ~' URL`, `python3 gen.py --title='rm -rf ~ hazard'`, `MSG='rm -rf ~ is bad' ./notify.sh`; also (earlier commits) `python -c 'print("rm -rf ~")'`, `jq '.cmd = "rm -rf ~"'`, `gh pr create --body 'remove rm -rf ~ footgun'`; commands over the size limits (100k chars) are refused in auto and yolo.

## Review round 9 (tip `7d272472`): FAIL, findings NOT fixed

An independent reviewer ran about 115 `decide()` probes. None raised; nothing exceeded 1 s; of 38 everyday commands only `GIT_SSH_COMMAND='ssh -i ~/.ssh/deploy' git fetch` changed (now forced human, an over-ask). New variants of already-fixed classes still pass in default mode under a `prog *` rule, or in auto with no rule:

1. HIGH `git -ccore.pager='rm -rf ~' log` (glued `-cKEY=VAL`; `_git_config_options` only matches the spaced `-c`), also fsmonitor and sshCommand forms, and behind `sudo -u git`.
2. HIGH `tar cf /dev/null src --checkpoint=1 --checkpoint-action=exec='rm -rf ~'`, `tar xf a.tar --to-command=...`, `tar --use-compress-program=...` (short `-I` is denied).
3. HIGH `vim -c '!rm -rf ~' x`, `vim +'!...' x`, `less '+!sh' x`.
4. HIGH `GIT_PROXY_COMMAND='rm -rf ~' git fetch`, `GIT_CONFIG_PARAMETERS="'core.pager'='rm -rf ~'" git log` (auto).
5. HIGH `docker run --privileged=true img` (exact `--privileged` match), `docker exec --privileged c sh`.
6. HIGH `git config alias.x '!rm -rf ~'` (also `--global`); the `core.pager` form is denied.
7. HIGH `git -c protocol.ext.allow=always fetch 'ext::sh -c rm% -rf% ~'`.
8. MEDIUM `git -c uploadpack.packObjectsHook=...`; `ctr run --privileged`; `docker run --device-cgroup-rule='a *:* rwm'`; `kubectl run --overrides='{"spec":{"hostPID":true}}'`; `docker run -v ..:/h`; `git -c core.hooksPath=/tmp`, `include.path`, `--exec-path`; `LD_PRELOAD=/tmp/x.so ls`, `PATH=/tmp:$PATH ls`, `NODE_OPTIONS=--require` (auto).
9. LOW `ls;` x4000 asks; `cd / && rm -rf x` allows in auto (bwrap mitigates).

**Conclusion.** Nine rounds, nine FAILs (round 7 gave no verdict): each round the reviewer finds new spellings of exec-capable options, wrapper programs and environment variables. The structural scans reduce the surface but a text-level parser cannot enumerate every program that runs a command. I stopped fixing variants here. The sound fix is architectural: run bash in the sandbox (bwrap) in every mode, and keep the parser as a convenience and prompt-reduction layer. That needs your decision (it changes default-mode behaviour and needs the sandbox available on every host).

## NOT fixed, and why

- `sudo -u git git -c core.pager='rm -rf ~' log`: the git `-c` exec scan reads only a `git` that starts the command run; a launcher in front hides it. Also not probed: `/usr/bin/git`, `core.hooksPath`, `GIT_SSH_COMMAND`/`MANPAGER`/`GIT_CONFIG_*` env vars, `include.path`.
- Containers: `docker volume create --opt device=/etc --opt o=bind`, `--volumes-from`, and compose-file volumes are not checked (only `run`/`create` flags and `--mount`/`-v`).
- Auto mode: `cat src/$X` with `X=../..` assigned on the same line allows (as on base); `cat $(...)` and backtick substitution words keep the old behaviour; `CMD='rm -rf ~'; sh -c "$CMD"` (same-line assignment) is not resolved; `base64 -d | sh` is structurally undecidable.
- Readers not on the builtin list (`less`, `base64`, `xxd`, `awk`, `sed`) on secret paths get a plain ask, which auto mode answers.
- Recursive listings (`ls -R ~`, `du -a ~`, `tree -a ~`, `find ~ -name id_rsa`, `grep -r x ~`) can reveal names under dot folders that `ls ~/.ssh` protects.
- `busctl get-property ... Environment` and `systemd-analyze dump` leak unit environments (plain ask).
- `_abs` resolves `link/../x` textually: `cd -P lnk/..; echo x > f` allows (write outside the project through a symlink).
- Digit-suffixed secret names (`server.pem1`) are not matched by the name filter; `firejail dd if=/dev/sda` is exempt through `strict=False`; `xterm -e rm -rf ~` is not caught by the new scan (`-e` values are skipped to spare `grep -e`); `nohup foo 'cd /; rm -rf *'` (a `cd /` inside a quoted payload is not followed, as on base).
- Over-asking accepted: `/etc/krb5.conf`, `tokenize.py`/`secrets.py` under `/usr`, `/sys` reads; `git commit -m 'block /etc/shadow'`, `rg -n '/etc/shadow' src`, `git log -S/etc/shadow`; any `docker run --device /...` and `--cap-add` values such as `SYS_PTRACE`, takeover flags after the image name; `ls ~/.cache`, `du -sh ~/.cargo`, `ls ~/.local/share`; `cd ~ && cat .bashrc`, `cd $X && ls proc`; `cd /tmp && echo x > a` in default; `echo x > f; cd ..`; brace words over 4096; a shared glob budget (large harmless globs outside the project ask); commands over the size limits (100k chars, 2k commands, 10k words, or too many `cd` targets x words) ask in default and are refused in auto and yolo.
- Item 6: no text-JSON tool-call fallback for local models (not required). Item 4: cold-start ranking is alphabetical.
- Flaky tests, unexplained: `test_sandbox_git.py::test_a_symlinked_worktrees_dir...`, `test_panes.py::test_bg_pane_sends_start_agent`, `test_tools.py::test_bash_does_not_read_the_gateways_stdin` fail occasionally under `-n auto` and pass alone; `test_cli_headless_output.py` failed once with `No module named 'k3code.providers.retry_utils'` (module present, passes alone). None touches the permission code; none was checked against the base commit.

## Residual risk

The permission engine is a heuristic shell parser, not a proof. Eight review rounds each found new ways around it, mostly wrapper programs, operator spellings and option forms nobody had listed; later rounds replaced listing by structural scans (every argument word, every command run, every cd target), which shrinks the tail but cannot close it. Default and accept-edits modes run bash unsandboxed, so a parser miss there is an unprompted host action. Treat the new read-only allowances as a convenience, not a boundary. The sound long-term fix is to run bash in the sandbox in every mode and keep the parser as a convenience layer; I did not do that because it is a design change. Commits carry the author `Claude <noreply@anthropic.com>` because the repo has no git identity configured.
