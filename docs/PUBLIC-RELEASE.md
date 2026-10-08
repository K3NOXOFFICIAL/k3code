# Making the repository public

The repository is private. This is the checklist for the day it becomes public. Everything that could be done
without rewriting history or deleting anything is already done (see [Already done](#already-done-2026-10-08)).
The history rewrite is scripted, verified, and already applied to a private staging repository,
`K3NOXOFFICIAL/k3code-public`, which is ready to be switched to public. Replacing this repository with it waits
until the switch: doing it earlier would diverge every clone and open branch while development continues.

## Already done (2026-10-08)

- The tree has no secrets: gitleaks over the full history (all refs) finds only reviewed test fixtures, listed in
  `.gitleaksignore`.
- The tree has no personal or infrastructure details: the owner's tailnet address, domains, SSH host names, home
  paths and name are replaced by placeholders in `docs/reports/` and `scripts/dev/tasks/`, and by settings in code:
  `K3_OMNIROUTE_URL` (`scripts/exit/lib.py`), `K3DEV_DIRECT_URL` / `K3DEV_PUBLIC_URL` (`scripts/dev/omni-worker.sh`),
  `K3_SEARXNG_HOST` (`scripts/exit/m4_autonomy.py`). Set them locally to run those scripts against your own gateway.
- `core/.k3code/project.json` (a runtime artifact) is no longer tracked; `.k3code/` is ignored.
- `ci.yml` runs with a read-only token (`permissions: contents: read`); no workflow uses `pull_request_target`,
  self-hosted runners or secrets that a fork's pull request could reach.
- Every text file is LF in the repo and in every checkout (`.gitattributes`).
- LICENSE (MIT), NOTICE, `LICENSES/` and `VENDOR.toml` cover all vendored code; `scripts/vendor_check.py` passes.
- Every CI job was run locally and passes (core, tui, panes, vendor_check, installer-windows via Windows
  PowerShell 5.1); GitHub Actions itself refused all jobs for a billing reason on 2026-10-08.

## Still in history (needs a rewrite)

| What | Where | Fix |
|---|---|---|
| The owner's personal e-mail as author | 8 commits (the initial commit and the GitHub merge commits) | `--mailmap` |
| A 37.5 MB built binary `panes/k3` | added in `30f66efe`, untracked in `cf85da5b` | `--path panes/k3 --invert-paths` |
| The tailnet IP, the owner's domains, SSH host names, first name, other people's names | earlier versions of the files scrubbed above, plus `GOAL.md` history | `--replace-text` |
| `Claude-Session:` trailers | 276 commit messages | `--replace-message` |

The rewrite is `scripts/release/rewrite_history.sh`. Its rules live in `~/.config/k3code-release/` on the owner's
machine, outside the repository, because they name what is removed. It was dry-run on 2026-10-08 against all 27
branches and passed every check: each removed value at 0 occurrences in contents and messages (each rule matched
2 to 276 lines before), only noreply author addresses, no blob over 5 MB (13 MB packed), gitleaks clean over the
whole rewritten history, and `Main`'s files identical to the current `Main` apart from commit ids, which
`scripts/release/remap_hashes.py` points at the new commits (`.gitleaksignore`, `.git-blame-ignore-revs`, a few
docs).

**Why a fresh repository:** GitHub keeps every pull request's head as a read-only `refs/pull/<n>/head` (here #1,
#4–#8, #16, #17). A force-push cannot remove them, so the old commits would stay reachable in this repository
once it is public. Publish the rewritten history to a new, empty repository instead (or ask GitHub Support to
purge the old refs, which takes longer).

## On the day

1. **Decide what stays.** `docs/reports/`, `scripts/dev/`, `scripts/exit/` and `GOAL.md` are the build record.
   They are scrubbed, but they describe the owner's workflow. Keep them, or delete them in a last commit before
   the rewrite (`scripts/release/build_release.sh` already leaves them out of release archives).
2. **Update the wording** in that same commit: `README.md` (the "private for now" note and the `gh auth login`
   install step) and `GOAL.md` (the repository row, the visibility decision and the "repository is private" line).
3. **Merge or close every open branch and pull request**, then stop the agents, soaks and timers that push.
4. **Dry run**, and check the summary says `verification passed`:

   ```bash
   scripts/release/rewrite_history.sh          # needs git, uv, gitleaks; pushes nothing
   ```

5. **Refresh the staging repository and swap.** `K3NOXOFFICIAL/k3code-public` (private) already holds the
   rewritten history as of 2026-10-08 (`Main` = PR #18, verified from a fresh clone: noreply authors only, every
   removed value at 0, largest blob 301 KB, gitleaks clean, no `refs/pull/*`). Never open pull requests there
   before the swap (they would pin commits again). Bring it up to date (`--push` mirrors, so it replaces what is
   there), then rename the current repository to `k3code-archive` (keep it private, it holds the old history) and
   rename `k3code-public` to `k3code`:

   ```bash
   scripts/release/rewrite_history.sh --push https://github.com/K3NOXOFFICIAL/k3code-public
   ```
6. **Re-clone everywhere** (laptop, desktop, servers). The install and release URLs keep working, since the name
   is unchanged. Old clones still hold the old history and must never be pushed to the new repository.
7. **Switch visibility** in Settings → General, then enable: private vulnerability reporting (SECURITY.md relies
   on it), Dependabot alerts, secret scanning with push protection, and branch protection on `Main` requiring CI.
   Actions need a working billing setup on the account first.
8. Optional: pin the actions in `.github/workflows/` by commit SHA (most useful for `release.yml`, which has
   `contents: write`), remove the inert upstream workflows under `panes/.github/`, and delete the archive once
   nothing needs its history.
