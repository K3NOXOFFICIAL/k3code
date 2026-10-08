# M6-install: installer for new devices, guided first-run setup, setup mode, `/update` with rollback, release CI

## Current state

The repo has:
- `core/`: a Python package `k3code` (uv project);
- `tui/`: an Ink TUI, built with `npm run build:ink && npm run build`, producing `tui/dist/entry.js`;
- `panes/`: a Go tuios fork, built with `go build ./cmd/k3`;
- `install/systemd/k3code.service` and `k3code service install|uninstall|status [--dry-run]`;
- `k3code doctor [--json]`.

`GOAL.md` lists the guided-setup requirements. Read `docs/reports/m2-ops.md`.

## Build

### 1. `install/install.sh`: one-line installer (POSIX sh, idempotent, no root)

**Usage:**
```
curl -fsSL <raw url>/install/install.sh | sh
```
or, from a local checkout:
```
sh install/install.sh --from-source
```

**Steps:**
1. Detect the OS and arch (Linux x86_64/arm64 first; macOS best-effort).
2. Ensure `uv`. Install it user-locally via its official installer if it is missing, and ask first unless `--yes`.
3. Ensure Node 22+. If it is missing or too old, download the official Node tarball into `~/.local/share/k3code/node/<ver>`; no system Node is needed.
4. Ensure Go only for `--from-source`. Otherwise download the prebuilt `k3` binary from the GitHub release.
5. Install the core: `uv tool install` from the release wheel, or `uv tool install -e core` with `--from-source`.
6. Place the TUI dist in `~/.local/share/k3code/versions/<ver>/tui`.
7. Set up `~/.local/bin/k3code` and `~/.local/bin/k3` (symlinks or shims).
8. Run `k3code doctor`.
9. Run `k3code setup`, unless `--headless` / `--no-setup`.

**Other flags:**
- `--from-bundle <file.k3bundle>` imports settings and sessions (uses the existing import) and asks only for secrets.
- `--channel stable|dev` and `--version X`.
- Uninstall with `install/uninstall.sh`, which keeps `~/.k3code` unless `--purge`.

**Private repo.** The repo is private, so the download needs a GitHub token (`GITHUB_TOKEN`, or `gh auth token` if `gh` is present). Document this clearly. `--from-source` from a cloned checkout must always work.

**Versioned layout:**
- `~/.local/share/k3code/versions/<ver>/` holds the core venv or `uv tool` env pointer, the TUI dist and the `k3` binary.
- `current` is a symlink to the active version.
- This layout lets an update roll back.

### 2. `k3code setup`: guided first-run wizard (resumable)
- This runs in the terminal before the TUI. Use Python `prompt_toolkit` (or `questionary` as a dependency) to give arrow-key selection.
- State is stored in `~/.k3code/setup_state.json` so an interrupted setup resumes at the same step.
- `--non-interactive --answers answers.yaml` runs it non-interactively for scripted installs.
- **Steps:**
  1. **Welcome and import.** Fresh setup, or import a `.k3bundle`.
  2. **About you.** Name or handle, preferred language (de/en), experience level, and how verbose k3code should be. This goes to `USER.md` in memory.
  3. **System and environment.** Auto-detect the OS, shell, terminal, editor, git identity, available toolchains (python/node/go/rust/docker) and whether tailscale is present, then let the user confirm or edit.
  4. **Primary use.** Coding (which languages and frameworks), ops/infra, research, or mixed. This sets defaults: output style, the default permission mode, whether plan-first is on, and the fan-out cap.
  5. **Providers and fallback chain.**
     - Add a primary, secondary and tertiary entry. Presets: OmniRoute/OpenAI-compatible (base URL plus key env), Anthropic, OpenAI, OpenRouter and local (Ollama/llama.cpp URL).
     - Keys are stored in `~/.config/k3code/env` (0600, used by the systemd `EnvironmentFile`). The config only references `api_key_env`; it never holds secrets.
     - Test each entry live, showing latency and OK/FAIL.
     - Warn if no entry bypasses a self-hosted gateway.
  6. **Model tiers and degradation.** For each tier (`main`, `strong`, `cheap`, `fast`), pick a model from that provider's `/v1/models` list, or type one. Explain that background, loop and cron work uses `cheap`. There are no hardcoded defaults: the user chooses.
  7. **Permissions.** Pick the default mode and review the hardline denies. Optionally add extra hardline rules, such as a `ssh <host> systemctl` deny.
  8. **Integrations.** MCP servers (add a URL or stdio command), the mem0 URL, skills roots and the SearXNG URL. Each is optional and tested.
  9. **Theme and UI.** Default theme: the list comes from the TUI's skin system, with a live preview of colors in the terminal if feasible. Also focus mode on/off by default, and the keymap style for `k3` panes (`k3` or `tuios`).
  10. **24/7 service.** Optionally run `k3code service install`. This step offers it; it never auto-enables it. Then print the linger advice.
  11. **Keymap tour.** A 6-line cheat sheet for the TUI (Shift+Tab, Ctrl+F, ↓ strip, /commands) and for `k3` panes (Ctrl+G, Esc, Alt+arrows).
  12. **Summary.** Write `config.yaml`, then run `k3code doctor`.
- `k3code setup --step <name>` re-runs a single step.
- `/settings` in the TUI shows a hint that setup steps can be re-run with `k3code setup --step …`.
- When `k3code` runs with no config, it starts setup automatically.

### 3. `/update` and `k3code update`
- Check GitHub releases on the configured channel (via the `gh` API with a token) or a git checkout (`--from-source`: `git pull` + rebuild).
- Download into a new `versions/<ver>`, run a smoke test (`k3code --version` + `k3code doctor --json` with no fails), and only then switch the `current` symlink.
- If the smoke test fails, or the daemon fails to start within 2 minutes after the switch, roll back to the previous version automatically.
- `k3code update --rollback` switches back manually.
- Restart the daemon service if it is installed.
- `/update` in the TUI shows the current and latest version, and the changelog, and asks before updating.

### 4. Release CI (`.github/workflows/`)
- **`ci.yml`:**
  - core: pytest and ruff;
  - TUI: `npm ci`, build:ink, build and vitest (allow the known failure via an exclude list);
  - panes: `go build` and `go test ./internal/k3keys/...`;
  - `vendor_check`.
- **`release.yml`** on tag `v*`:
  - build the core wheel, the TUI dist tarball and `k3` binaries (linux amd64/arm64);
  - write `SHA256SUMS`;
  - create a GitHub release with those assets.
- Add `VERSION` / `__version__`, single-sourced.

## Tests
- The installer runs in a temp `HOME` with `--from-source --yes --no-setup` and no network-dependent downloads. Mock Node and uv as present, or skip the download step via env flags. It is idempotent: a second run makes no changes.
- Wizard steps use the non-interactive answers file, including resuming after an interruption (kill after step 4, rerun, and it continues at step 5). Secrets go only to the env file, which has 0600 permissions.
- Update logic uses fake versions dirs: switch, smoke-test failure triggers a rollback, manual rollback works.
- Validate the CI YAML with `python -c "import yaml…"`, plus `actionlint` if available.

## Acceptance (put the outputs in REPORT.md)
- Tests pass and ruff is clean.
- `HOME=$(mktemp -d) sh install/install.sh --from-source --yes --no-setup` from this checkout succeeds. Paste the output tail and `ls` of the versions dir.
- `k3code setup --non-interactive --answers <sample>` in a temp home writes config and env files. Paste the redacted config.
- **Do NOT install the systemd service or touch the real `~/.k3code` / `~/.config` / `~/.local` of this machine:** all tests use temp homes.
