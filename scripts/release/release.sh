#!/usr/bin/env bash
# Publish a k3code release from this machine. Replaces the GitHub Actions workflow release.yml: the same checks and
# assets, without CI minutes.
#
# Usage: scripts/release/release.sh [--dry-run [--no-check]] VERSION
#        scripts/release/release.sh --list-assets VERSION
#
# A release needs: the Main branch checked out, a clean tree, HEAD == origin/Main (after git fetch), VERSION ==
# the VERSION file, a "## [VERSION]" heading in CHANGELOG.md with nothing left under "## Unreleased" (the notes are
# the "## [VERSION]" section only), and no tag vVERSION yet. Then:
#   1. scripts/ci/check.sh --post: the full local check, and the local-ci status on the commit
#   2. the personal-data scan of the source export (scripts/release/build_release.sh; the export is not published)
#   3. the assets, in .k3dev/release/VERSION/assets/:
#        k3code-<pep440 version>-py3-none-any.whl   uv build --wheel
#        k3code-VERSION-requirements.txt            uv export --locked: the hashed runtime dependencies
#        k3code-tui-VERSION.tar.gz                  the built TUI (tui/dist)
#        k3-{linux,darwin}-{amd64,arm64}            the k3 pane binary, CGO_ENABLED=0 cross builds
#        SHA256SUMS                                 a sha256 line for every other asset
#   4. the release notes: the CHANGELOG.md section of VERSION
#   5. gh release create --draft with every asset (--prerelease when VERSION contains "-"), a check that the draft holds
#      them all, then gh release edit --draft=false. GitHub creates the tag vVERSION at the checked commit only when it
#      publishes, so a run that fails earlier leaves no tag: rerun the same command (it replaces a leftover draft).
#
# --dry-run checks the preconditions without stopping on them (each one that fails is printed), runs the full check
# without --post, builds everything into .k3dev/release/VERSION/ and prints what it would publish. --no-check (only
# with --dry-run) skips the full check. --list-assets prints the asset names for VERSION (the wheel as a pattern).
set -euo pipefail

if [ "${K3CODE_CI_NICED:-}" != 1 ]; then
  export K3CODE_CI_NICED=1
  exec nice -n 10 bash "$0" "$@"
fi

HERE=$(cd "$(dirname -- "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
K3CI_NAME=release.sh
# shellcheck source=scripts/ci/lib.sh
. "$ROOT/scripts/ci/lib.sh"
strip_git_env
cd "$ROOT"

# asset_names VERSION: every file a release publishes. update.py (k3code update) picks them by these names: the
# *.whl, k3code-tui-*.tar.gz, k3-<os>-<arch>, *-requirements.txt, and SHA256SUMS to verify the rest.
asset_names() {
  echo "k3code-*-py3-none-any.whl"
  echo "k3code-$1-requirements.txt"
  echo "k3code-tui-$1.tar.gz"
  local os arch
  for os in linux darwin; do
    for arch in amd64 arm64; do
      echo "k3-$os-$arch"
    done
  done
  echo "SHA256SUMS"
}

DRY=0
CHECK=1
LIST=0
VER=""
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --no-check) CHECK=0 ;;
    --list-assets) LIST=1 ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *)
      [ -z "$VER" ] || die "one version only"
      VER=$1
      ;;
  esac
  shift
done
[ -n "$VER" ] || die "usage: release.sh [--dry-run [--no-check]] VERSION | --list-assets VERSION"
printf '%s\n' "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$' ||
  die "not a version: $VER (expected 1.2.3 or 1.2.3-rc.1, without a leading v)"
if [ "$LIST" = 1 ]; then
  asset_names "$VER"
  exit 0
fi
[ "$CHECK" = 1 ] || [ "$DRY" = 1 ] || die "--no-check is only allowed with --dry-run"
TAG="v$VER"

# --- preconditions -----------------------------------------------------------------------------------------------
problems=0
problem() {
  problems=$((problems + 1))
  if [ "$DRY" = 1 ]; then
    note "a real release would refuse: $*"
  else
    note "refused: $*"
  fi
}

git fetch --quiet origin || problem "git fetch origin failed"
branch=$(git symbolic-ref --quiet --short HEAD || echo "(detached)")
[ "$branch" = Main ] || problem "on $branch, not Main"
tree_clean || problem "the working tree has uncommitted or untracked changes"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/Main 2>/dev/null || true)" ] ||
  problem "HEAD $(git rev-parse --short=12 HEAD) is not origin/Main; pull or push first"
file_ver=$(tr -d ' \t\r\n' <VERSION)
[ "$file_ver" = "$VER" ] || problem "the VERSION file says $file_ver, not $VER (core/pyproject.toml reads its version from VERSION)"
grep -Eq "^## \[?$(printf '%s' "$VER" | sed 's/[.]/\\./g')\]?([[:space:]]|$)" CHANGELOG.md ||
  problem "CHANGELOG.md has no '## [$VER]' heading"
# The release notes are the '## [VER]' section alone (step 4), so whatever '## Unreleased' still holds would be left out
# of them. Blank lines and '###' subheadings do not count as content.
unreleased=$(awk '
  /^## / { h = tolower($2); gsub(/[][]/, "", h); if (h == "unreleased") { in_u = 1; next } if (in_u) exit }
  in_u && NF && !/^###/ { n++ }
  END { print n + 0 }
' CHANGELOG.md)
[ "$unreleased" = 0 ] ||
  problem "CHANGELOG.md '## Unreleased' still holds $unreleased line(s) the release notes would leave out; rename it to '## [$VER] - <date>' and start a fresh, empty '## Unreleased'"
if git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null || [ -n "$(git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null)" ]; then
  problem "tag $TAG already exists"
fi
for t in uv npm go gh; do
  have "$t" || problem "$(install_hint "$t")"
done
if [ "$problems" -gt 0 ] && [ "$DRY" = 0 ]; then
  die "$problems precondition(s) failed; nothing was built or published"
fi

# --- 1. the full check -------------------------------------------------------------------------------------------
SHA=$(git rev-parse HEAD)
if [ "$CHECK" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    bash "$ROOT/scripts/ci/check.sh" || die "the full check failed; see the summary above"
  else
    bash "$ROOT/scripts/ci/check.sh" --post || die "the full check failed; see the summary above"
  fi
else
  note "--no-check: the full check was skipped"
fi

# --- 2. personal-data scan ---------------------------------------------------------------------------------------
OUT="$ROOT/.k3dev/release/$VER"
ASSETS="$OUT/assets"
rm -rf "$OUT"
mkdir -p "$ASSETS" "$OUT/build"
echo "release: personal-data scan of the export of ${SHA:0:12}"
sh "$HERE/build_release.sh" -o "$OUT/export" HEAD

# --- 3. assets ---------------------------------------------------------------------------------------------------
echo "release: core wheel"
(cd core && uv build --quiet --wheel --out-dir "$OUT/build/wheel")
cp "$OUT"/build/wheel/*.whl "$ASSETS/"

echo "release: locked requirements"
# The runtime dependencies `k3code update` installs with --require-hashes before the wheel (--no-deps): the set from
# core/uv.lock that the check ran against. --locked fails on a lock that no longer matches pyproject.toml.
(cd core && uv export --quiet --locked --no-dev --no-emit-project --no-header --format requirements-txt \
  -o "$ASSETS/k3code-$VER-requirements.txt")

echo "release: TUI tarball"
(
  cd tui
  npm ci --no-audit --no-fund
  npm run build:ink
  npm run build
  tar -czf "$ASSETS/k3code-tui-$VER.tar.gz" dist
) >"$OUT/build/tui.log" 2>&1 || die "the TUI build failed; see $OUT/build/tui.log"

echo "release: k3 binaries"
for os in linux darwin; do
  for arch in amd64 arm64; do
    (cd panes && GOOS=$os GOARCH=$arch CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$ASSETS/k3-$os-$arch" ./cmd/k3)
  done
done

echo "release: SHA256SUMS"
(
  cd "$ASSETS"
  files=()
  for f in *; do [ "$f" = SHA256SUMS ] || files+=("$f"); done
  if have sha256sum; then sha256sum -- "${files[@]}"; else shasum -a 256 -- "${files[@]}"; fi >SHA256SUMS
)

# The assets must be exactly asset_names: k3code update finds its files by these names and installs only what
# SHA256SUMS lists.
expected=0
while IFS= read -r name; do
  expected=$((expected + 1))
  # shellcheck disable=SC2086 # the wheel name is a glob on purpose
  set -- "$ASSETS"/$name
  [ $# = 1 ] && [ -f "$1" ] || die "asset missing or ambiguous: $name"
done < <(asset_names "$VER")
actual=$(find "$ASSETS" -type f | wc -l | tr -d ' ')
[ "$actual" = "$expected" ] || die "$ASSETS holds $actual files, expected $expected: $(cd "$ASSETS" && echo *)"
[ "$(wc -l <"$ASSETS/SHA256SUMS" | tr -d ' ')" = $((expected - 1)) ] || die "SHA256SUMS does not list every asset"

# --- 4. notes ----------------------------------------------------------------------------------------------------
NOTES="$OUT/notes.md"
awk -v ver="$VER" '
  /^## / { if (found) exit; h = $2; gsub(/[][]/, "", h); if (h == ver) { found = 1; next } }
  found { print }
' CHANGELOG.md >"$NOTES"
if [ ! -s "$NOTES" ]; then
  [ "$DRY" = 1 ] || die "the CHANGELOG.md section of $VER is empty"
  echo "(dry run: CHANGELOG.md has no section for $VER)" >"$NOTES"
fi

flags=()
case $VER in *-*) flags+=(--prerelease) ;; esac

echo
echo "release: assets of $TAG (from ${SHA:0:12}) in ${ASSETS#"$ROOT"/}:"
(cd "$ASSETS" && for f in *; do printf '  %-40s %s\n' "$f" "$(wc -c <"$f" | tr -d ' ') bytes"; done)

if [ "$DRY" = 1 ]; then
  echo
  echo "release: --dry-run, would now run:"
  echo "  gh release create $TAG <the assets above> --draft --target ${SHA:0:12} --title $TAG --notes-file ${NOTES#"$ROOT"/} ${flags[*]-}"
  echo "  gh release edit $TAG --draft=false   (GitHub then creates the tag $TAG at ${SHA:0:12})"
  [ "$problems" = 0 ] || echo "release: $problems precondition(s) failed (above): a real release would have refused"
  exit 0
fi

# --- 5. publish --------------------------------------------------------------------------------------------------
# The tag must not exist before the release does: install.sh takes the newest v* tag at once, and release.sh refuses a
# tag that exists, so a tag left behind by a failed upload would be installed and block the rerun. The release is made
# as a draft (no tag) with every asset, checked, and only then published; GitHub creates the tag at $SHA when it does.
stale=$(gh release list --limit 100 --json tagName,isDraft \
  --jq ".[] | select(.isDraft and .tagName == \"$TAG\") | .tagName") || die "gh release list failed; is gh signed in (gh auth status)?"
if [ -n "$stale" ]; then
  note "replacing the draft release $TAG that an earlier run left behind"
  gh release delete "$TAG" --yes
fi
gh release create "$TAG" "$ASSETS"/* --draft --target "$SHA" --title "$TAG" --notes-file "$NOTES" ${flags[@]+"${flags[@]}"}
uploaded=$(gh release view "$TAG" --json assets --jq '.assets[].name' | sort) || die "gh release view $TAG failed"
local_assets=$(cd "$ASSETS" && printf '%s\n' * | sort)
[ "$uploaded" = "$local_assets" ] ||
  die "the draft release $TAG holds [$(tr '\n' ' ' <<<"$uploaded")], not [$(tr '\n' ' ' <<<"$local_assets")]; it stays a draft, no tag exists: rerun"
gh release edit "$TAG" --draft=false
echo "release: $TAG published"
