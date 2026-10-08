#!/usr/bin/env bash
# Rewrite the repository's history for going public (see docs/PUBLIC-RELEASE.md), verify it, and optionally push it.
#
#   scripts/release/rewrite_history.sh [--rules DIR] [--work DIR] [--source URL] [--main-only] [--push URL]
#
# --main-only publishes just Main (and tags): the milestone, worker and agent branches stay in the old repository.
# Without --push nothing leaves the machine: it is a dry run that leaves the result in --work for inspection.
# DIR (default ~/.config/k3code-release) holds three files kept OUT of the repository, since they name what is removed:
#   replacements.txt  git-filter-repo --replace-text rules for file contents
#   messages.txt      the same rules plus `regex:(?m)^Claude-Session: [^\n]*\n?==>` for commit messages
#   mailmap           maps the personal author address to the GitHub noreply address
# Needs: git, uv (runs git-filter-repo), gitleaks, python3. Uses GitHub credentials only for clone and --push.
set -euo pipefail

RULES=${HOME}/.config/k3code-release
WORK=${TMPDIR:-/tmp}/k3code-rewrite
PUSH=""
MAIN_ONLY=""
SRC=https://github.com/K3NOXOFFICIAL/k3code
while [ $# -gt 0 ]; do
  case $1 in
    --rules) RULES=$2; shift 2 ;;
    --work) WORK=$2; shift 2 ;;
    --push) PUSH=$2; shift 2 ;;
    --main-only) MAIN_ONLY=1; shift ;;
    --source) SRC=$2; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for f in replacements.txt messages.txt mailmap; do
  [ -s "$RULES/$f" ] || { echo "missing $RULES/$f" >&2; exit 2; }
done
HERE=$(cd "$(dirname "$0")" && pwd)

rm -rf "$WORK"
mkdir -p "$WORK"
# Branches and tags only: GitHub's read-only refs/pull/* would bring the old history back.
git init -q --bare "$WORK/repo.git"
git -C "$WORK/repo.git" fetch -q "$SRC" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
git -C "$WORK/repo.git" symbolic-ref HEAD refs/heads/Main
if [ -n "$MAIN_ONLY" ]; then
  # Commits reachable only from the other branches are neither rewritten nor pushed.
  git -C "$WORK/repo.git" for-each-ref --format='%(refname)' refs/heads | grep -v '^refs/heads/Main$' |
    xargs -r -n1 git -C "$WORK/repo.git" update-ref -d
fi

(cd "$WORK/repo.git" && uv tool run --from git-filter-repo git-filter-repo --force \
  --path panes/k3 --invert-paths \
  --replace-text "$RULES/replacements.txt" \
  --replace-message "$RULES/messages.txt" \
  --mailmap "$RULES/mailmap")

# Commit ids written inside files (.gitleaksignore fingerprints, .git-blame-ignore-revs, docs) follow the rewrite.
git clone -q "$WORK/repo.git" "$WORK/co"
python3 "$HERE/remap_hashes.py" "$WORK/repo.git/filter-repo/commit-map" "$WORK/co"
if [ -n "$(git -C "$WORK/co" status --porcelain)" ]; then
  git -C "$WORK/co" -c user.name=K3NOXOFFICIAL -c user.email=46091052+K3NOXOFFICIAL@users.noreply.github.com \
    commit -qam "Point commit ids in files at the rewritten history"
  git -C "$WORK/co" push -q origin Main
fi

echo "== verification"
fail=0
cd "$WORK/repo.git"
# every rule's left-hand side must be gone from all contents and messages
while IFS= read -r rule; do
  [ -z "$rule" ] && continue
  lhs=${rule%%==>*}
  if [[ $lhs == regex:* ]]; then lhs=${lhs#regex:}; mode=-P; else mode=-F; fi
  n=$(git log --all -p --format=%B | grep -c $mode -- "$lhs" || true)
  [ "$n" = 0 ] || { echo "FAIL: a removed value is still present ($n lines)"; fail=1; }
done < "$RULES/messages.txt"
addr=$(sed -n 's/.*> <\(.*\)>$/\1/p' "$RULES/mailmap")
n=$(git log --all --format='%ae%n%ce' | grep -cF -- "$addr" || true)
[ "$n" = 0 ] || { echo "FAIL: the personal address is still an author/committer"; fail=1; }
big=$(git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' |
  awk '$1=="blob" && $2>5000000' | wc -l)
[ "$big" = 0 ] || { echo "FAIL: $big blob(s) over 5 MB"; fail=1; }
gitleaks git . --log-opts=--all --config "$WORK/co/.gitleaks.toml" --gitleaks-ignore-path "$WORK/co/.gitleaksignore" \
  --redact --no-banner >/dev/null 2>&1 || { echo "FAIL: gitleaks reports findings"; fail=1; }
echo "branches: $(git for-each-ref refs/heads | wc -l), tags: $(git for-each-ref refs/tags | wc -l), size: $(du -sh . | cut -f1)"
[ $fail = 0 ] || { echo "verification failed; nothing pushed" >&2; exit 1; }
echo "verification passed"

if [ -n "$PUSH" ]; then
  echo "== pushing all branches and tags to $PUSH"
  git push --mirror "$PUSH"
else
  echo "dry run: nothing pushed; result in $WORK/repo.git"
fi
