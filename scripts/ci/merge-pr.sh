#!/usr/bin/env bash
# Merge a pull request into Main after the full local check passed on the merge result. Replaces "wait for the GitHub
# Actions checks, then press merge".
#
# Usage: scripts/ci/merge-pr.sh [--dry-run] NUMBER
#
#   1. git fetch origin; the PR must be open, based on Main, with its head branch in this repository
#   2. a temporary worktree (.k3dev/merge-pr/<n>-<time>/wt) at the PR head; origin/Main is merged into it (--no-ff;
#      nothing to do when the head already contains it)
#   3. the full scripts/ci/check.sh on that tree; on failure it stops and prints the log folder
#   4. pushes the merge commit to the PR's head branch (fast-forward only, never forced), sets the commit status
#      local-ci=success on it, marks a draft PR ready and runs gh pr merge --merge for exactly that commit
#
# Run it inside the checkout. --dry-run does 1 to 3 and prints what 4 would do. The worktree is removed afterwards; the logs stay in
# .k3dev/merge-pr/<n>-<time>/ci/.
set -euo pipefail

if [ "${K3CODE_CI_NICED:-}" != 1 ]; then
  export K3CODE_CI_NICED=1
  exec nice -n 10 bash "$0" "$@"
fi

HERE=$(cd "$(dirname -- "$0")" && pwd)
K3CI_NAME=merge-pr.sh
# shellcheck source=scripts/ci/lib.sh
. "$HERE/lib.sh"
strip_git_env
ROOT=$(git rev-parse --show-toplevel) || die "run it inside the k3code checkout"
cd "$ROOT"

DRY=0
PR=""
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *)
      [ -z "$PR" ] || die "one pull request number only"
      PR=$1
      ;;
  esac
  shift
done
case $PR in '' | *[!0-9]*) die "usage: merge-pr.sh [--dry-run] NUMBER" ;; esac
have gh || die "$(install_hint gh)"

git fetch --quiet origin || die "git fetch origin failed"

info=$(gh pr view "$PR" --json state,baseRefName,headRefName,headRefOid,isCrossRepository,isDraft,url \
  --jq '[.state, .baseRefName, .headRefName, .headRefOid, (.isCrossRepository|tostring), (.isDraft|tostring), .url] | @tsv') ||
  die "gh pr view $PR failed"
IFS=$'\t' read -r state base head_ref head_sha cross draft url <<<"$info"

[ "$state" = OPEN ] || die "#$PR is $state, not open"
[ "$base" = Main ] || die "#$PR is based on $base, not Main"
[ "$cross" = false ] || die "#$PR comes from a fork: the merge commit would have to be pushed to the fork's branch"
remote_sha=$(git rev-parse --verify --quiet "refs/remotes/origin/$head_ref" || true)
[ "$remote_sha" = "$head_sha" ] ||
  die "origin/$head_ref is ${remote_sha:-missing}, the PR head is $head_sha: fetch again once GitHub has caught up"

echo "merge-pr: #$PR $url"
echo "merge-pr: head $head_ref at ${head_sha:0:12}, base Main at $(git rev-parse --short=12 origin/Main)"

WORK="$ROOT/.k3dev/merge-pr/$PR-$(date +%Y%m%dT%H%M%S)"
WT="$WORK/wt"
LOGS="$WORK/ci"
mkdir -p "$WORK"
git worktree add --quiet --detach "$WT" "$head_sha"
cleanup() {
  git -C "$ROOT" worktree remove --force "$WT" 2>/dev/null || true
}
trap cleanup EXIT

if git -C "$WT" merge-base --is-ancestor origin/Main HEAD; then
  echo "merge-pr: $head_ref already contains origin/Main; nothing to merge"
else
  if ! git -C "$WT" merge --quiet --no-ff --no-edit -m "Merge branch 'Main' into $head_ref" origin/Main; then
    git -C "$WT" merge --abort || true
    die "origin/Main does not merge cleanly into $head_ref: merge it on the branch, resolve, push, then run this again"
  fi
  echo "merge-pr: merged origin/Main into $head_ref"
fi
merged=$(git -C "$WT" rev-parse HEAD)

# The check that the merged tree defines; a tree from before scripts/ci existed is checked with this script's copy.
check="$WT/scripts/ci/check.sh"
if [ ! -f "$check" ]; then
  note "the merged tree has no scripts/ci/check.sh; checking it with $HERE/check.sh"
  check="$HERE/check.sh"
fi
echo "merge-pr: full check of ${merged:0:12} (logs: $LOGS)"
if ! K3CODE_CI_ROOT="$WT" K3CODE_CI_LOG_DIR="$LOGS" bash "$check"; then
  die "the full check failed on ${merged:0:12}; nothing was pushed or merged. Logs: $LOGS"
fi

platform="$(uname -s | tr '[:upper:]' '[:lower:]')/$(uname -m)"
desc="full local check passed on $platform (merge-pr.sh)"
if [ "$DRY" = 1 ]; then
  echo "merge-pr: --dry-run, would now:"
  [ "$merged" = "$head_sha" ] || echo "  git push origin ${merged:0:12}:refs/heads/$head_ref   (fast-forward)"
  echo "  set $K3CI_CONTEXT=success on ${merged:0:12}: $desc"
  [ "$draft" = false ] || echo "  gh pr ready $PR"
  echo "  gh pr merge $PR --merge --match-head-commit $merged"
  exit 0
fi

if [ "$merged" != "$head_sha" ]; then
  # No --force: GitHub refuses the push unless it fast-forwards the branch.
  git -C "$WT" push origin "$merged:refs/heads/$head_ref" || die "push to $head_ref refused (did the branch move?)"
fi
post_status "$merged" success "$desc"
if [ "$draft" = true ]; then
  gh pr ready "$PR"
fi
gh pr merge "$PR" --merge --match-head-commit "$merged"
echo "merge-pr: #$PR merged"
