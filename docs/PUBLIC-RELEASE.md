# Making the repository public

The repository is public: `github.com/K3NOXOFFICIAL/k3code` is readable without signing in (checked on
2026-10-15). The switch followed the checklist below. The rewritten history from the staging repository
`K3NOXOFFICIAL/k3code-public` replaced this repository's: the old name now leads to `k3code`, and pull request
numbers start again at #1, merged on 2026-10-09. What is still open is in [Still open](#still-open). The rest of
this file records how the switch was prepared and done.

## Still open

Checked from outside on 2026-10-15 at `Main` = `236d1eb`. The GitHub settings cannot be seen from outside, so only
the owner can confirm or finish them:

- **Repository settings** (step 5 below): private vulnerability reporting is on (the Security page offers
  "Report a vulnerability"). Not checkable from outside, so confirm them in Settings: Dependabot alerts, secret
  scanning with push protection, and branch protection on `Main` requiring the `local-ci` status
  ([`docs/RELEASING.md`](RELEASING.md)).
- **The archive** (step 3): `K3NOXOFFICIAL/k3code-archive` is not readable without signing in. If it exists, keep
  it private: it holds the old history.
- **Old clones** (step 4): a clone made before the switch still holds the old history. Re-clone and never push from
  an old one; whether every machine was re-cloned cannot be checked from here.
- **The first release** (step 6): there is no release and no tag yet.
- **Clean-up** (step 7): this file, `rewrite_history.sh`, `remap_hashes.py` and the inert upstream workflows under
  `panes/.github/` are still in the tree.
- **History since the switch:** the rewritten part of `Main` (443 commits, up to `27700b0` "Point commit ids in files
  at the rewritten history") has only noreply author and committer addresses and no `Claude-Session:` trailer. Of
  the 930 commits made since, 345 have an author or committer address that is not a noreply one, and 155 have a
  `Claude-Session:` trailer. They are public. The owner decides whether that matters and which address and trailers
  new commits use.

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
- There are no GitHub Actions workflows (since 2026-10-09): the checks, merges and releases run locally
  ([`docs/RELEASING.md`](RELEASING.md)), so no workflow token or secret exists that a fork's pull request could reach.
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

## What the rewrite removed

The rewrite removed what this table lists from the published history. Checked on 2026-10-15: `panes/k3` is in no
commit of `Main` or of any pull request, and the rewritten commits have only noreply addresses and no trailers. The
`--replace-text` rules were not checked again, since only the rules folder names what they remove.

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
cannot remove them, so the old commits would have stayed reachable in this repository once it was public. Publish the
rewritten history to a new, empty repository instead (or ask GitHub Support to purge the old refs, which takes
longer). `--main-only` publishes `Main` and tags only: the milestone, worker and agent branches stay in the old
repository, which becomes the archive.

## On the day

The rewrite and the swap (steps 2 and 3) were done by 2026-10-09, when the new repository's first pull request was
merged, and the repository was public by 2026-10-15 (step 5). What is left of steps 4 to 7 is in
[Still open](#still-open).

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
   on it), Dependabot alerts, secret scanning with push protection, and branch protection on `Main` requiring the
   `local-ci` status ([`docs/RELEASING.md`](RELEASING.md)). Actions can stay disabled.
6. **Publish the first release.** Releases do not carry over from the archive: tag `v0.1.0` in the new repository
   with `scripts/release/release.sh 0.1.0` ([`docs/RELEASING.md`](RELEASING.md)).
7. **Clean up.** This file and the two single-use scripts (`rewrite_history.sh`, `remap_hashes.py`) can be deleted in
   a last commit. Optional: remove the inert upstream workflows under `panes/.github/`, and delete the archive
   once nothing needs its history.
