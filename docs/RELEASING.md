# Checks, merges and releases without GitHub Actions

k3code runs no GitHub Actions. Every check, merge and release runs on a maintainer's machine with three scripts, so
they cost no CI minutes:

| Script | Replaces | What it does |
| --- | --- | --- |
| `scripts/ci/check.sh` | `ci.yml`, `gitleaks.yml` | the full local check; `--post` sets the commit status `local-ci` |
| `scripts/ci/merge-pr.sh N` | waiting for the checks, then pressing merge | merges `Main` into the PR, runs the full check, pushes, posts `local-ci`, merges |
| `scripts/release/release.sh VER` | `release.yml` | checks, builds the assets, tags, publishes the GitHub release |

`installer.yml` (macOS and Windows runners) is replaced, as far as Linux can, by `scripts/ci/platforms.sh`; see
[macOS and Windows](#macos-and-windows).

## One-time repository settings (owner)

1. **Branch protection on `Main`:** under Settings → Branches (or Rules), replace the required checks `core`, `tui`,
   `panes`, `vendor_check` and `scan` with one required status check named **`local-ci`**. Keep "require a pull
   request" if you want it; "require branches to be up to date" works with `merge-pr.sh`, which merges `Main` in
   before it checks.
2. **Actions:** Settings → Actions → General → "Disable actions". Nothing in the repository needs them any more.
3. Each maintainer runs `scripts/dev/install-hooks.sh` once per clone (it sets `core.hooksPath` in that clone only).

## The local check

```sh
scripts/ci/check.sh              # full: every area, as CI ran it
scripts/ci/check.sh --quick      # lint, format and the fast checks (the pre-push hook runs this)
scripts/ci/check.sh --changed    # only the areas this branch and your uncommitted changes touch
scripts/ci/check.sh --only core --only shell
scripts/ci/check.sh --plan       # print the commands, run nothing
scripts/ci/check.sh --post       # full run, then set local-ci on HEAD (clean tree, HEAD pushed)
```

Areas: `core` (ruff check, ruff format --check, pytest; needs uv and bubblewrap), `tui` (npm ci, build, typecheck,
eslint, prettier, vitest minus `tui/.ci-test-excludes`; needs Node 22+), `panes` (go build, go vet and go test on the
packages k3 uses, never `go test ./...`; needs Go and a C compiler for `-race`), `vendor` (`scripts/vendor_check.py`),
`secrets` (gitleaks on the commits over `origin/Main` and on uncommitted changes), `shell` (shellcheck, `sh -n` /
`bash -n`). A missing tool fails its area with an install hint. The run is niced (`nice -n 10`); logs and a summary
table go to `.k3dev/ci/<time>/`.

`npm ci` runs only when `tui/package-lock.json` changed since the last install (a hash in
`tui/node_modules/.k3ci-lock`); delete `tui/node_modules` to force a clean install.

`--post` refuses a partial run, a dirty tree and a commit that is on no `origin` branch: the status always describes a
commit everyone can fetch, checked from exactly that tree. A failed run posts `local-ci=failure`.

## Hooks

`scripts/dev/install-hooks.sh` points git at `.githooks/`:

- `pre-commit` checks the staged files only: `ruff format --check` for Python, `prettier --check` for `tui/` (when
  `tui/node_modules` exists) and `gitleaks protect --staged` (when gitleaks is installed). A few seconds.
- `pre-push` runs `check.sh --quick` for a branch push and the **full** check for a push to `refs/heads/Main` or of any
  tag. Deleting a remote branch runs nothing.

`K3CODE_SKIP_HOOKS=1 git push …` skips a hook and says so loudly. Use it only when you ran the check yourself.
`scripts/dev/install-hooks.sh --uninstall` removes the setting.

## Merging a pull request

```sh
scripts/ci/merge-pr.sh --dry-run 42   # everything except push, status and merge
scripts/ci/merge-pr.sh 42
```

The PR must be open, based on `Main`, and its branch must be in this repository (a fork's branch cannot be pushed
to). The script fetches, creates a worktree in `.k3dev/merge-pr/42-<time>/wt` at the PR head, merges `origin/Main` into
it (`--no-ff`), and runs the full check of the merged tree. When it passes it pushes the merge commit to the PR branch
(a fast-forward; never forced), sets `local-ci=success` on it, marks a draft PR ready and runs
`gh pr merge 42 --merge --match-head-commit <sha>`, so GitHub merges exactly the commit that was checked. On a
conflict or a failed check it stops and prints the log folder; nothing is pushed. The worktree is removed in every
case; the logs stay in `.k3dev/merge-pr/42-<time>/ci/`.

## Releasing

1. Set the version in `VERSION` (the core wheel reads it from there), add a `## [X.Y.Z] - <date>` section to
   `CHANGELOG.md`, merge that to `Main`.
2. On an up-to-date, clean `Main`: `scripts/release/release.sh --dry-run X.Y.Z`, then `scripts/release/release.sh X.Y.Z`.

`release.sh` refuses unless `Main` is checked out, the tree is clean, `HEAD` is `origin/Main`, `X.Y.Z` equals `VERSION`,
`CHANGELOG.md` has the heading and the tag does not exist. Then it runs `check.sh --post`, runs the personal-data scan
of `scripts/release/build_release.sh` on the source export (the export itself is not published; GitHub attaches
source archives to every release), builds the assets into `.k3dev/release/X.Y.Z/assets/`, creates the annotated tag
`vX.Y.Z`, pushes it and runs `gh release create` with the CHANGELOG section as notes. A version with a `-`
(`1.0.0-rc.1`) is published as a prerelease.

`--dry-run` reports every failed precondition instead of stopping, still runs the full check (add `--no-check` to skip
it), builds everything and prints what it would publish. `release.sh --list-assets X.Y.Z` prints the asset names.

### Assets and how `k3code update` uses them

| Asset | Built with | Used by the updater |
| --- | --- | --- |
| `k3code-<pep440>-py3-none-any.whl` | `uv build --wheel` in `core/` | the one `*.whl`, installed into a fresh venv |
| `k3code-X.Y.Z-requirements.txt` | `uv export --locked --no-dev --no-emit-project` (hashed) | `*-requirements.txt`: the locked dependencies, for an updater that installs with `--require-hashes` (an older one downloads and ignores it) |
| `k3code-tui-X.Y.Z.tar.gz` | `npm ci`, `build:ink`, `build`, `tar` of `tui/dist` | `k3code-tui-*.tar.gz`, unpacked into the version's `tui/` |
| `k3-linux-amd64`, `k3-linux-arm64`, `k3-darwin-amd64`, `k3-darwin-arm64` | `CGO_ENABLED=0 GOOS=… GOARCH=… go build -trimpath -ldflags "-s -w" ./cmd/k3` | `k3-<platform>-<arch>`, copied to the version's `bin/k3` |
| `SHA256SUMS` | `sha256sum` of every other asset | each asset it lists is verified before anything is installed |

`k3code update` (`core/src/k3code/update.py`) downloads every asset of the newest release on its channel, verifies
them against `SHA256SUMS` and picks the files by these names. `core/tests/test_local_ci.py` feeds the names
`release.sh --list-assets` prints through `update.install_release`, so a rename on either side fails the tests.

## macOS and Windows

The macOS and Windows installer jobs ran on GitHub's paid runners. `scripts/ci/platforms.sh` covers what can run on a
Linux machine with podman or docker:

```sh
scripts/ci/platforms.sh                     # ubuntu, ubuntu2204, debian, fedora, alpine and wsl; core only
scripts/ci/platforms.sh fedora ubuntu2204   # only these targets (about a minute each once the images are pulled)
scripts/ci/platforms.sh --full ubuntu2204   # also builds the TUI and the k3 binary (two to three minutes)
```

Each target installs a clone of `HEAD` as a normal user in a clean container, commits on top, runs `k3code update`,
checks that the version switched and that a second update is a no-op, rolls back and uninstalls; the result line names
each step with its time, or the step that failed. `ubuntu` is the distribution `wsl --install` sets up. `ubuntu2204`
is a bare 22.04 with only git and curl added (the run fails if Go, unzip or python3 came along): it has no Go new
enough for `panes/go.mod`, so `--full ubuntu2204` checks that the installer fetches its own. `fedora` (44) prepares
with dnf. `debian` runs the installer under dash and `alpine` under BusyBox on musl. `wsl`
runs `install.ps1` and `uninstall.ps1` with PowerShell 7 (`pwsh`) against a `wsl.exe` stand-in that behaves like the
WSL Windows 10 ships with (no `--cd`) and runs each command in an Ubuntu container, on a checkout that belongs to
another user, as a clone made by Windows git can when WSL sees it; it updates with `k3code update --from-source`.
Run it before a release and after a change to `install/`, `update.py` or `service.py`.

Still manual:

- **macOS:** run `cd core && uv run pytest -q tests/test_installer.py`, then `sh install/install.sh` and
  `k3code update` on a Mac before a release that changes `install/`.
- **Windows:** `pwsh` is not Windows PowerShell 5.1, and the stand-in is not WSL. Before a release that changes
  `install/*.ps1`, run `install.ps1 -Help`, an install and `k3code update` from PowerShell 5.1 on a Windows machine.
