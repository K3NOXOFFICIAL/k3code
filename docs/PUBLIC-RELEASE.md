# Making the repository public

The repository is private. This is the checklist for the day it becomes public. Everything that could be done
without rewriting history or deleting anything is already done (see [Already done](#already-done)). The rest
rewrites history or deletes branches, so it waits until the switch: doing it earlier would diverge every clone
and open branch while development continues.

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

## Still in history (needs a rewrite)

| What | Where | Fix |
|---|---|---|
| The owner's personal e-mail as author | 8 commits (the initial commit and the GitHub merge commits) | `--mailmap` |
| A 37.5 MB built binary `panes/k3` | added in `30f66efe`, untracked in `cf85da5b` | `--path panes/k3 --invert-paths` |
| The tailnet IP, the owner's domains, SSH host names, first name, other people's names | earlier versions of the files scrubbed above, plus `GOAL.md` history | `--replace-text` |
| `Claude-Session:` trailers | 276 commit messages | `--replace-message` |

## On the day

1. **Decide what stays.** `docs/reports/`, `scripts/dev/`, `scripts/exit/` and `GOAL.md` are the build record.
   They are scrubbed, but they describe the owner's workflow. Keep them, or move them to a private repo and
   delete them here (`scripts/release/build_release.sh` already leaves them out of release archives).
2. **Merge or close every open branch and pull request**, then stop all agents and soaks that push.
3. **Rewrite history** in a fresh mirror clone with [git-filter-repo](https://github.com/newren/git-filter-repo).
   Keep `replacements.txt` and `mailmap` *outside* the repository: they contain the values being removed.

   ```bash
   git clone --mirror https://github.com/K3NOXOFFICIAL/k3code k3code-rewrite.git
   cd k3code-rewrite.git
   # replacements.txt, one rule per line:  literal==>replacement   (or regex:...==>...)
   #   <tailnet-ip>==><omniroute-host>
   #   regex:[a-z]+\.<owner-domain>==><owner-host>
   #   /home/<name>/==>~/
   #   <first name>==>the owner
   #   <ssh host a>==><server-a>
   #   <ssh host b>==><server-b>
   # messages.txt:  regex:(?m)^Claude-Session: .*$==>
   # mailmap:  K3NOX <46091052+K3NOXOFFICIAL@users.noreply.github.com> <personal address>
   git filter-repo \
     --path panes/k3 --invert-paths \
     --replace-text ../replacements.txt \
     --replace-message ../messages.txt \
     --mailmap ../mailmap
   ```

   Check the result before pushing:

   ```bash
   git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u                     # only noreply addresses
   git log --all -p | grep -cE '<tailnet-ip>|<owner-domain>|<first name>'         # 0
   git rev-list --objects --all | git cat-file --batch-check='%(objectsize) %(rest)' | sort -n | tail -3
   gitleaks git . --log-opts=--all --redact                                     # fixtures only; new fingerprints
   ```

   The rewrite changes every commit id, so the fingerprints in `.gitleaksignore` (`<commit>:<file>:<rule>:<line>`)
   must be regenerated from the new gitleaks report, and `.git-blame-ignore-revs` must be rewritten with the new
   ids of the formatting commits.
4. **Push the rewrite**: temporarily allow force pushes on `Main`, `git push --mirror --force`, then restore
   branch protection.
5. **Delete stale branches** on GitHub (`claude/*`, `m0/*` … `m6/*`, `exit/verify`, `m0-scaffold`) once their
   work is confirmed merged.
6. **Re-clone everywhere** (laptop, desktop, servers). Old clones still hold the old history and must not be
   pushed again.
7. **Update the wording**: `README.md` (the "private for now" note and the `gh auth login` install step) and
   `GOAL.md` (the repository row, the visibility decision and the "repository is private" line).
8. **Switch visibility** in Settings → General, then enable: private vulnerability reporting (SECURITY.md relies
   on it), Dependabot alerts, secret scanning with push protection, and branch protection on `Main` requiring CI.
9. Optional: pin the actions in `.github/workflows/` by commit SHA (most useful for `release.yml`, which has
   `contents: write`), and remove the inert upstream workflows under `panes/.github/`.
