# Upstream policy

k3code is assembled from other open-source projects. How each part is kept in step with its origin depends on how much it has been changed. Every vendored or ported file or tree is listed in [`VENDOR.toml`](../VENDOR.toml) with its origin commit; licenses are in [`LICENSES/`](../LICENSES) and [`NOTICE`](../NOTICE).

| Part | Origin | Policy |
|---|---|---|
| `panes/` | [TUIOS](https://github.com/Gaurav-Gosain/tuios) at `f3d8929` | **Mergeable.** Our changes are two small hooks plus the `internal/k3keys` package (see `panes/K3_CHANGES.md`), so upstream can be merged. Target: fewer than 10 conflicting files per sync. |
| `tui/` and `tui/shared/` | Hermes Agent TUI at `4127d78` | **Frozen fork.** The TUI was heavily changed (rebrand, agent list under the input, focus mode, removed Nous-specific features, new slash handling). A sync of upstream reports about 48 conflicting files, so merging is not realistic. Upstream fixes are cherry-picked by hand. |
| Single vendored Python files (`router/classifier.py`, `router/cooldown.py`, `providers/retry_utils.py`, `tools/fuzzy_match.py`, …) | Hermes Agent | **Snapshots.** Adapted once; updates are optional and read from upstream by hand. They count toward the "fewer than 10 conflicts" target. |
| Ported designs (goals, loops, cron, permission rules, research flow, …) | Hermes, opencode, Atomic Agents, Codex | **Reimplemented.** Not tracked line by line; the ledger names the upstream file the design came from. |

## Checking what changed upstream

```sh
scripts/sync-upstream.sh --dry-run
```

This fetches the upstream repositories into temporary bare clones (nothing in the checkout changes) and does a three-way comparison of each vendored subtree against its recorded base commit. For every subtree it prints the number of files that conflict, and for the frozen fork it also prints how many files changed upstream since the base. `--frozen NAME` marks another subtree as frozen, `--json` prints the full result, `--threshold N` changes the limit.

## Cherry-picking a fix into the frozen TUI

1. Run the dry run and read the list of files changed upstream (`--json` gives the file names).
2. For a fix you want, look at the upstream commit and apply the same change to the corresponding file under `tui/` by hand; where our file has diverged, port the intent rather than the lines.
3. Run the TUI checks: `(cd tui && npm run build:ink && npm run build && npx vitest run)`.
4. Record it: keep the base commit in `VENDOR.toml` unchanged, and note the cherry-picked upstream commit in the commit message.

## Merging TUIOS

1. Run the dry run; for `panes/` it should list fewer than 10 conflicting files.
2. Fetch upstream into a scratch directory, merge the changed files into `panes/`, and re-apply the two hooks listed in `panes/K3_CHANGES.md` if they conflict.
3. Run only the packages we depend on: `(cd panes && go build ./cmd/k3 && go test ./internal/k3keys/... ./internal/harness/... ./internal/input/ ./internal/app/)`. Do not run the whole upstream test tree: its remote-sync tests recurse without bound.
4. Update the base commit in `panes/UPSTREAM.toml` and `VENDOR.toml`.

## Rules that always apply

- Only permissive licenses (MIT, Apache-2.0). Keep copyright notices and add new ones to `NOTICE` and `LICENSES/`.
- Never use proprietary or leaked code, or closed binaries; `scripts/vendor_check.py` refuses listed entries from banned sources.
