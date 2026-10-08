# Making the repository public

The repository is private. This is the checklist for the day it becomes public. Everything that could be done
without rewriting history or deleting anything is already done (see [Already done](#already-done)). The history
rewrite is scripted, verified and rehearsed on the final `Main` (see below), and an earlier run of it is already in a
private staging repository, `K3NOXOFFICIAL/k3code-public`. Replacing this repository with the staging one waits
until the switch: doing it earlier would diverge every clone and open branch while development continues.

## Already done

- The tree has no secrets: gitleaks over the full history (all refs) finds only reviewed test fixtures, listed in
  `.gitleaksignore`.
- The tree has no personal or infrastructure details: the owner's tailnet address, domains, SSH host names, home
  paths and name are replaced by placeholders in `docs/reports/` and `scripts/dev/tasks/`, and by settings in code:
  `K3_OMNIROUTE_URL` (`scripts/exit/lib.py`), `K3DEV_DIRECT_URL` / `K3DEV_PUBLIC_URL` (`scripts/dev/omni-worker.sh`),
  `K3_SEARXNG_HOST` (`scripts/exit/m4_autonomy.py`). Set them locally to run those scripts against your own gateway.
- The names and hosts the export scan looks for are not listed in the repository either: `scripts/release/build_release.sh`
  reads them from the rules folder (see below). Its tests use invented names.
- `core/.k3code/project.json` (a runtime artifact) is no longer tracked; `.k3code/` is ignored.
- `ci.yml` runs with a read-only token (`permissions: contents: read`); no workflow uses `pull_request_target`,
  self-hosted runners or secrets that a fork's pull request could reach.
- Every text file is LF in the repo and in every checkout (`.gitattributes`).
- LICENSE (MIT), NOTICE, `LICENSES/` and `VENDOR.toml` cover all vendored code; `scripts/vendor_check.py` passes.
- Issue forms (bug report, feature request), a pull request template, and a contact link that sends security
  reports to private vulnerability reporting are in `.github/`.
- The wording no longer says the repository is private (README, GOAL.md, docs/PLAN.md).
- Every CI job was run locally and passes (core, tui, panes, vendor_check, installer-windows via Windows
  PowerShell 5.1); GitHub Actions itself refused all jobs for a billing reason on 2026-10-08.
- **Rehearsal (2026-10-09).** The rewrite was run on a copy of the repository whose `Main` was the last pull request
  (`--main-only`): `verification passed`, 433 commits on one branch, 14 MB. Every file of that `Main` is identical to
  the branch it came from, except nine that hold commit ids. An independent scan of the result found no
  first name, host, tailnet address, personal address, `/home/<owner>` path or `Claude-Session:` trailer, and only
  noreply author addresses. What is left that looks like a name is the public account name, the Go word "nils" in
  vendored code, and example addresses in tests and upstream fixtures.

## Still in history (needs a rewrite)

| What | Where | Fix |
|---|---|---|
| The owner's personal e-mail as author | 13 commits (the initial commit and the GitHub merge commits) | `--mailmap` |
| A 37.5 MB built binary `panes/k3` | added in `556cfe82`, untracked in `e90211c3` | `--path panes/k3 --invert-paths` |
| The tailnet IP, the owner's domains, SSH host names, first name, other people's names, a bootstrap-prompt phrase from the owner's gateway | earlier versions of the files scrubbed above, plus `GOAL.md` history and four commit messages | `--replace-text` |
| `Claude-Session:` trailers | about 300 commit messages | `--replace-message` |

The rewrite is `scripts/release/rewrite_history.sh`. Its rules live in `~/.config/k3code-release/` on the owner's
machine, outside the repository, because they name what is removed:

| File | Used by | What |
|---|---|---|
| `replacements.txt` | the rewrite | `git-filter-repo --replace-text` rules for file contents |
| `messages.txt` | the rewrite | the same rules, the `Claude-Session:` trailer rule and one message-only rule |
| `mailmap` | the rewrite | the personal author address to the GitHub noreply address |
| `personal.re`, `names.re` | `build_release.sh` | the names and hosts the release export must not contain |

The folder was missing on 2026-10-09 and was rebuilt from a scan of the history. Keep a copy somewhere safe, outside
the repository: nothing else holds these rules.

**Why a fresh repository:** GitHub keeps every pull request's head as a read-only `refs/pull/<n>/head`. A force-push
cannot remove them, so the old commits would stay reachable in this repository once it is public. Publish the
rewritten history to a new, empty repository instead (or ask GitHub Support to purge the old refs, which takes
longer). `--main-only` publishes `Main` and tags only: the milestone, worker and agent branches stay in the old
repository, which becomes the archive.

## On the day

1. **Merge the open pull request into `Main`** (and close any other you still want). With `--main-only` the other
   branches are not published, so they can stay as they are.
2. **Dry run**, and check the summary says `verification passed`:

   ```bash
   scripts/release/rewrite_history.sh --main-only       # needs git, uv, gitleaks; pushes nothing
   ```

3. **Refresh the staging repository and swap.** `K3NOXOFFICIAL/k3code-public` (private) holds an earlier rewrite.
   Never open pull requests there before the swap (they would pin commits again). `--push` mirrors, so it replaces
   what is there:

   ```bash
   scripts/release/rewrite_history.sh --main-only --push https://github.com/K3NOXOFFICIAL/k3code-public
   ```

   Then rename the current repository to `k3code-archive` (keep it private, it holds the old history) and rename
   `k3code-public` to `k3code`.
4. **Re-clone everywhere** (laptop, desktop, servers, the Windows PC). The install and release URLs keep working,
   since the name is unchanged. Old clones still hold the old history and must never be pushed to the new repository.
   A Windows install built from an old clone keeps updating from it (`k3code update` pulls the clone); re-run
   `install\install.ps1` from a fresh clone to move it to the new repository.
5. **Switch visibility** in Settings → General, then enable: private vulnerability reporting (SECURITY.md relies
   on it), Dependabot alerts, secret scanning with push protection, and branch protection on `Main` requiring CI.
   Actions need a working billing setup on the account first.
6. **Publish the first release.** Releases do not carry over from the archive: tag `v0.1.0` in the new repository
   (`release.yml` builds and attaches the archive), or build it with `scripts/release/build_release.sh`.
7. **Clean up.** This file and the two single-use scripts (`rewrite_history.sh`, `remap_hashes.py`) can be deleted in
   a last commit. Optional: pin the actions in `.github/workflows/` by commit SHA (most useful for `release.yml`,
   which has `contents: write`), remove the inert upstream workflows under `panes/.github/`, and delete the archive
   once nothing needs its history.
