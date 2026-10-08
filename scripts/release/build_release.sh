#!/bin/sh
# Build the k3code release export: git archive of a ref, internal paths removed, a personal-data scan,
# then k3code-<version>.tar.gz and SHA256SUMS in the output directory.
#
# Usage: build_release.sh [-o DIR] [REF]      build the export (REF defaults to HEAD; DIR defaults to
#                                             $CLAUDE_JOB_DIR, else dist/)
#        build_release.sh --scan DIR          scan an unpacked export and report; builds nothing
#
# The scan prints file names only, never the text it matched.
set -eu

# Internal paths: they stay in the repository and never reach an export. panes/k3 is a 36 MB Linux build output
# that the repository tracks by mistake (since 30f66ef); it is left out until it is untracked.
EXCLUDED="scripts/dev scripts/exit docs/reports GOAL.md panes/k3"
# Personal names and hosts, matched as whole words and case-insensitively, and the owner's first name matched
# case-sensitively. The literals are split with '' so that this file does not match its own scan.
# Every tailnet (100.x) address is matched too.
PERSONAL_RE='(^|[^A-Za-z])(ke''no|fenn''ec|nb''wg|nyew''cloud|han''na|k3nox\.com|kokad''ock|k3ser''ver|se''tsu)'
NAME_RE='(^|[^A-Za-z])Ni''ls([^A-Za-z]|$)'
TAILNET_RE='(^|[^0-9.])100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
# Absolute home paths: any /home/<name> fails unless <name> is a generic placeholder used in upstream fixtures.
HOME_RE='(^|[^A-Za-z0-9_.~])/home/[A-Za-z0-9_-]+'
HOME_ALLOW='/home/(user|u|me|x|you|someone|ubuntu|ada|dana|dev|d|g|guest|fuzz|al|alex|linuxbrew)([^A-Za-z0-9_.-]|$)'

die() {
  printf '%s\n' "build_release: $*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
usage: build_release.sh [-o DIR] [REF]
       build_release.sh --scan DIR

Builds k3code-<version>.tar.gz and SHA256SUMS in DIR (default: $CLAUDE_JOB_DIR, else dist/)
from the git ref REF (default HEAD). --scan only runs the personal-data scan on DIR.
USAGE
}

# scan_dir DIR: print the file names that match, return 1 if any match.
scan_dir() {
  hits="$work/hits"
  (
    cd "$1" || exit 2
    {
      grep -rlaiE --exclude-dir=.git -e "$PERSONAL_RE" . || true
      grep -rlaE --exclude-dir=.git -e "$NAME_RE" . || true
      grep -rlaE --exclude-dir=.git -e "$TAILNET_RE" . || true
      grep -raoHE --exclude-dir=.git -e "$HOME_RE" . | grep -vaE -e "$HOME_ALLOW" | cut -d: -f1 || true
      find . -name .git -prune -o -print | grep -iE -e "$PERSONAL_RE" || true
    } | sort -u
  ) > "$hits"
  if [ -s "$hits" ]; then
    sed 's|^\./||' "$hits" >&2
    return 1
  fi
  return 0
}

out=${CLAUDE_JOB_DIR:-dist}
ref=HEAD
scan_only=
while [ $# -gt 0 ]; do
  case $1 in
    -o)
      [ $# -ge 2 ] || die "-o needs a directory"
      out=$2
      shift 2
      ;;
    --scan)
      [ $# -ge 2 ] || die "--scan needs a directory"
      scan_only=$2
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*) die "unknown option: $1" ;;
    *)
      ref=$1
      shift
      ;;
  esac
done

work=$(mktemp -d "${TMPDIR:-/tmp}/k3code-release.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

if [ -n "$scan_only" ]; then
  [ -d "$scan_only" ] || die "not a directory: $scan_only"
  if scan_dir "$scan_only"; then
    echo "scan passed: $scan_only"
    exit 0
  fi
  die "personal-data scan failed; the files above match (matched text not shown)"
fi

script_dir=$(cd "$(dirname -- "$0")" && pwd)
repo=$(cd "$script_dir/../.." && pwd)
git -C "$repo" rev-parse --verify --quiet "$ref^{commit}" >/dev/null || die "not a commit: $ref"

stage="$work/src"
mkdir "$stage"
git -C "$repo" archive --format=tar "$ref" | tar -x -C "$stage"
[ -f "$stage/VERSION" ] || die "no VERSION file at $ref"
version=$(tr -d ' \t\r\n' < "$stage/VERSION")
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$' ||
  die "VERSION is not a version number"

for p in $EXCLUDED; do
  rm -rf "${stage:?}/$p"
done

name="k3code-$version"
mv "$stage" "$work/$name"

if ! scan_dir "$work/$name"; then
  die "personal-data scan failed; fix the files above before building a release"
fi
count=$(find "$work/$name" -type f | wc -l | tr -d ' ')

mkdir -p "$out"
out=$(cd "$out" && pwd)
tar -C "$work" -cf "$work/$name.tar" "$name"
gzip -n -9 -c "$work/$name.tar" > "$out/$name.tar.gz"
(
  cd "$out"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$name.tar.gz"
  else
    shasum -a 256 "$name.tar.gz"
  fi
) > "$out/SHA256SUMS"

echo "wrote $out/$name.tar.gz ($count files; personal-data scan passed)"
echo "wrote $out/SHA256SUMS"
