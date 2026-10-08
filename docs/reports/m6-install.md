# M6-install report

## Built
- `VERSION` (repo root, single source) → `core/src/k3code/_version.py` → `k3code.__version__`, hatch dynamic version, `k3code --version`.
- `install/install.sh` (POSIX sh, idempotent, no root): OS/arch detect, uv (asks, or `--yes`), Node ≥22 (downloads official tarball + checksum into `~/.local/share/k3code/node/<ver>`), Go only for `--from-source` (downloaded if missing), versioned layout `versions/<ver>/{venv,tui,bin/k3}`, `current` symlink, `previous` file, `~/.local/bin/{k3code,k3}` links, doctor, setup (skipped by `--headless`/`--no-setup`), `--from-bundle` (uses `k3code import` then only the new `setup --step secrets` (asks just for `api_key_env` names from the config that are missing in the env file)), `--channel`, `--version`, `--no-activate`, `--print-version`. Release mode needs a GitHub token (`GITHUB_TOKEN`/`GH_TOKEN`/`gh auth token`), documented in the script header; assets are checksum-verified against `SHA256SUMS`.
- `install/uninstall.sh` (keeps `~/.k3code` and `~/.config/k3code` unless `--purge`), `install/answers.sample.yaml`.
- `core/src/k3code/setup/`: `state.py` (setup_state.json, 0600 env file), `detect.py`, `probe.py` (presets, live `/models` test with latency), `prompter.py` (prompt_toolkit arrow-key select / answers-file), `steps.py` (12 steps), `wizard.py` (resume, `--step`). CLI: `k3code setup [--step N] [--non-interactive --answers F] [--restart] [--no-probe]`; plain `k3code` on a tty with no config starts setup. `/settings` shows the re-run hint.
- `core/src/k3code/update.py`, `k3code update [--check|--yes|--channel|--from-source|--rollback]`, `/update [now|rollback]` (shows current/latest/changelog; applying is a second explicit command): version dir staging, smoke test (`--version` + `doctor --json --no-probe` with 0 fails), atomic symlink switch, daemon restart + 120 s health wait, automatic rollback, prune (keep 3).
- `.github/workflows/ci.yml` (core ruff+pytest; TUI npm ci/build:ink/build/vitest with `tui/.ci-test-excludes`; panes go build + k3keys test; vendor_check) and `release.yml` (tag `v*`: wheel, TUI tarball, k3 linux amd64/arm64, SHA256SUMS, `gh release create`; checks tag == VERSION).
- Tests: `test_setup.py`, `test_update.py`, `test_installer.py`, `test_ci_yaml.py`.

## Verification
- `cd core && uv run pytest` (run to a file, stdin from /dev/null; piping hangs, the known pipe issue) → 344 passed (6 warnings), exit 0. `uv run ruff check . ../scripts` → All checks passed. `python3 scripts/vendor_check.py` → All checks passed. `cd panes && go test ./internal/k3keys/...` → ok.
- `HOME=$(mktemp -d) sh install/install.sh --from-source --yes --no-setup` (real build: uv venv + editable core, `npm ci` + TUI build, `go build ./cmd/k3`) → exit 0. Tail:
```
k3code-install: current -> 0.0.1-src.2a20ce4
k3code-install: running k3code doctor
✗ providers: no providers configured     (expected before setup)
...
11 ok, 1 warn, 1 fail
k3code-install: done. Version 0.0.1-src.2a20ce4 installed. Run: k3code
```
`ls versions/` → `0.0.1-src.2a20ce4` containing `bin  tui  venv  .complete`; `current` → that dir; `~/.local/bin/k3code` and `k3` are symlinks into `current/`. A second run printed "already installed" and a before/after file+mtime snapshot was identical (also asserted in `test_installer.py`). `k3code --version` → `k3code, version 0.0.1`.
- `k3code setup --non-interactive --answers install/answers.sample.yaml --no-probe` in a temp home → config.yaml (redacted by construction, only `api_key_env` names):
```yaml
providers:
- {name: gw, kind: openai, base_url: http://127.0.0.1:9/v1, api_key_env: OMNIROUTE_API_KEY, models: {default: m-main, strong: m-strong, cheap: m-cheap, fast: m-fast}}
- {name: anthropic, kind: anthropic, base_url: https://api.anthropic.com, api_key_env: ANTHROPIC_API_KEY, models: {...same...}}
permission_mode: ask
output_style: concise
display: {theme: midnight, focus_mode: true}
tiers: {main: default, strong: strong, cheap: cheap, fast: fast, background: cheap}
autonomy: {plan_first: true, fanout_cap: 2}
panes: {keymap: tuios}
permissions: {hardline: ['ssh \S+ systemctl']}
mcp: {servers: {docs: {url: http://127.0.0.1:9/mcp}}}
skills: {roots: [/opt/skills]}
```
`~/.config/k3code/env` is mode `-rw-------` with `OMNIROUTE_API_KEY=<redacted>`, `ANTHROPIC_API_KEY=<redacted>`; `USER.md` written to `$K3CODE_HOME/memory/`.
- `--step X` rewrites only the config keys that step owns (`setup/steps.py::OWNS`) from the saved step data; `test_single_step_rerun` asserts config.yaml changes (theme, tiers) while providers stay. State is kept after completion (`done: true`); a plain `k3code setup` afterwards starts a fresh run.
- `cd core && uv build --wheel` works with the out-of-tree `../VERSION` (wheel metadata Version: 0.0.1). `update.install_release` builds the venv in its final dir (venvs are not relocatable).
- Resume: `test_resume_after_interrupt` interrupts at step 5, state lists steps 1-4, rerun prints "Resuming at step 'providers'" and skips 1-4.
- CI YAML parsed with PyYAML in `test_ci_yaml.py`. `actionlint` is not installed here, so it was not run.
- Nothing touched the real `~/.k3code`, `~/.config`, `~/.local` or systemd; all runs used temp homes.

## Deviations
- Not verified: live release install/update (no release exists, private repo; the GitHub API/download path is untested end to end), `vitest` and the release workflow have not run on GitHub, interactive prompt_toolkit screens were not driven by a tty test.
- Theme list: the TUI has no enumerable built-in skin list in the source I could find, so the wizard offers `default, midnight, light, solarized, mono` plus any `$K3CODE_HOME/skins/*.yaml`; the live colour preview is a fixed ANSI sample.
- Tiers are written to per-provider `models` (`main`→`default`) plus a top-level `tiers:`/`autonomy:` block; M4a/M4b are not merged here, so nothing consumes those keys yet.
- `k3code update` for releases builds the version dir in Python (duplicating a little of install.sh); `--from-source` stages via `install.sh --no-activate`. Source installs are editable installs, so the checkout must stay in place.
- The "daemon unhealthy" test uses an injected health probe; the real systemd path (`active_state`) was not exercised.

## Open TODOs
- Create a first tag `v0.0.1` and test release.yml + a real token install; run actionlint.
- `.ci-test-excludes` is empty; fill it once the known vitest failure is identified.
- Drive the interactive wizard under a pty (pexpect) and test the `/update` gateway command.
