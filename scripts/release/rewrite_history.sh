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

# --work is deleted and recreated, so it must be a directory this script made (it leaves a marker file in it) or one
# that does not exist or is empty, and never the home directory, the checkout, or anything that contains them.
MARKER=.k3code-rewrite-work
refuse_work() { echo "rewrite_history: refusing --work $WORK: $1" >&2; exit 2; }
[ -n "$WORK" ] || refuse_work "empty path"
[ ! -L "$WORK" ] || refuse_work "a symbolic link"
if [ -e "$WORK" ]; then
  [ -d "$WORK" ] || refuse_work "not a directory"
  work_real=$(cd "$WORK" && pwd -P)
else
  parent=$(cd "$(dirname -- "$WORK")" 2>/dev/null && pwd -P) || refuse_work "its parent directory does not exist"
  work_real=${parent%/}/$(basename -- "$WORK")
fi
repo_real=$(cd "$HERE/../.." && pwd -P)
home_real=$(cd "${HOME:-/nonexistent}" 2>/dev/null && pwd -P || true)
for guarded in / "$repo_real" "$home_real"; do
  [ -n "$guarded" ] || continue
  # $work_real is $guarded or a directory containing it
  case "${guarded%/}/" in "${work_real%/}"/*) refuse_work "it is, or contains, $guarded" ;; esac
done
if [ -d "$WORK" ] && [ -n "$(ls -A "$WORK")" ] && [ ! -f "$WORK/$MARKER" ]; then
  refuse_work "not empty and not made by this script (no $MARKER inside); pass an empty or new directory"
fi

rm -rf "$WORK"
mkdir -p "$WORK"
: >"$WORK/$MARKER"
# Branches and tags only: GitHub's read-only refs/pull/* would bring the old history back.
git init -q --bare "$WORK/repo.git"
git -C "$WORK/repo.git" fetch -q "$SRC" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
git -C "$WORK/repo.git" symbolic-ref HEAD refs/heads/Main
if [ -n "$MAIN_ONLY" ]; then
  # Commits reachable only from the other branches are neither rewritten nor pushed.
  # (grep -v exits 1 when Main is the only branch, which pipefail would make the end of the script)
  git -C "$WORK/repo.git" for-each-ref --format='%(refname)' refs/heads | { grep -v '^refs/heads/Main$' || true; } |
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

# count_hits MODE PATTERN < input: how many lines of stdin match. grep exits 1 for "none", which is fine, and 2 for an
# error (a pattern this grep cannot read, no -P support), which must not read as "none": it is returned.
count_hits() {
  local n rc=0
  n=$(LC_ALL=C grep -a -c "$1" -e "$2") || rc=$?
  [ "$rc" -le 1 ] || return "$rc"
  echo "$n"
}

# glob_to_ere GLOB: an approximation of the glob as an extended regex, matched anywhere on a line: `*` is any run of
# characters, `?` any one, a [...] class stays a class (a `[!...]` negation is NOT translated: it reads as a literal !).
# Verified against fixtures, not against filter-repo itself (not installed here): a glob rule is the one form to look at
# by hand if the verification ever surprises.
glob_to_ere() {
  printf '%s' "$1" | sed -e 's/[.^$+(){}|\\]/\\&/g' -e 's/\*/.*/g' -e 's/?/./g'
}

# parse_rule RULE: sets `mode` (the grep option) and `lhs` (what to look for) from one git-filter-repo --replace-text
# rule, `[literal:|regex:|glob:]PATTERN[==>REPLACEMENT]`. The replacement starts at the LAST `==>`, as filter-repo reads
# it, and a pattern with no prefix is a literal.
parse_rule() {
  local rule=${1%$'\r'}
  lhs=${rule%==>*}
  case $lhs in
    regex:*) mode=-P; lhs=${lhs#regex:} ;;
    glob:*) mode=-E; lhs=$(glob_to_ere "${lhs#glob:}") ;;
    literal:*) mode=-F; lhs=${lhs#literal:} ;;
    *) mode=-F ;;
  esac
}

# every rule's left-hand side must be gone from all contents and messages
lineno=0
while IFS= read -r rule || [ -n "$rule" ]; do
  lineno=$((lineno + 1))
  [ -n "${rule%$'\r'}" ] || continue
  parse_rule "$rule"
  [ -n "$lhs" ] || continue # filter-repo skips an empty pattern too
  rc=0
  n=$(git log --all -p --format=%B | count_hits "$mode" "$lhs") || rc=$?
  if [ "$rc" != 0 ]; then
    echo "FAIL: messages.txt line $lineno could not be checked (exit $rc); not treating it as clean"
    fail=1
  elif [ "$n" != 0 ]; then
    echo "FAIL: a removed value is still present ($n lines; messages.txt line $lineno)"
    fail=1
  fi
done < "$RULES/messages.txt"
addr=$(sed -n 's/.*> <\(.*\)>$/\1/p' "$RULES/mailmap")
if [ -n "$addr" ]; then
  rc=0
  n=$(git log --all --format='%ae%n%ce' | count_hits -F "$addr") || rc=$?
  { [ "$rc" = 0 ] && [ "$n" = 0 ]; } || { echo "FAIL: the personal address is still an author/committer (or could not be checked)"; fail=1; }
else
  echo "FAIL: no personal address found in the mailmap (expected 'Name <public> <personal>' lines); cannot verify it is gone"
  fail=1
fi
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
