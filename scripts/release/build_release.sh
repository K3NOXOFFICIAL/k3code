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
# that the repository tracks by mistake (since 556cfe8); it is left out until it is untracked.
EXCLUDED="scripts/dev scripts/exit docs/reports GOAL.md panes/k3"
# The names and hosts to keep out of an export are NOT listed in this file: a list would publish what it guards. They
# are read from the owner's machine, outside the repository (default folder ~/.config/k3code-release, or
# $K3_RELEASE_RULES):
#   personal.re   extended regexes, one per line, matched case-insensitively after a non-letter
#   names.re      extended regexes, one per line, matched case-sensitively (a first name that is also a word)
# Blank lines and # comments are ignored. Without the files the scan still checks every tailnet (100.x) address and
# every absolute home path, and it says that the name check did not run.
RULES_DIR=${K3_RELEASE_RULES:-${HOME:-/nonexistent}/.config/k3code-release}

die() {
  printf '%s\n' "build_release: $*" >&2
  exit 1
}

# bad_regex RE: true when RE is not a clean extended regular expression. grep exits 2 on a syntax error, and some
# mistakes (`(?i)`, a Perl-ism) only draw a warning on stderr and then match something other than what was meant. A
# rule that never matches is a scan that fails open, so both are refused.
bad_regex() {
  _re_err=$(printf '' | grep -E -e "$1" 2>&1 >/dev/null) || [ $? -eq 1 ] || return 0
  [ -n "$_re_err" ]
}
join_rules() { # join_rules FILE: the file's regexes as a|b|c, or nothing; a rule that is not a valid regex aborts
  [ -s "$1" ] || return 0
  _rules=$(tr -d '\r' <"$1") || die "cannot read $1" # CRLF line endings would end every rule in a carriage return
  _n=0
  _joined=
  while IFS= read -r _line; do
    _n=$((_n + 1))
    _trim=${_line#"${_line%%[![:space:]]*}"}
    case $_trim in '' | '#'*) continue ;; esac
    # the rule is not echoed: the rule files name what must not be published
    ! bad_regex "$_line" || die "$1 line $_n: not a valid extended regular expression; the scan cannot run with it"
    _joined=${_joined:+$_joined|}$_line
  done <<EOF
$_rules
EOF
  printf '%s\n' "$_joined"
}
personal=$(join_rules "$RULES_DIR/personal.re")
names=$(join_rules "$RULES_DIR/names.re")
PERSONAL_RE=
NAME_RE=
[ -z "$personal" ] || PERSONAL_RE="(^|[^A-Za-z])($personal)"
[ -z "$names" ] || NAME_RE="(^|[^A-Za-z])($names)([^A-Za-z]|\$)"
# the rules joined into one expression must still be a valid one
[ -z "$PERSONAL_RE" ] || ! bad_regex "$PERSONAL_RE" || die "$RULES_DIR/personal.re: the rules together are not a valid extended regular expression"
[ -z "$NAME_RE" ] || ! bad_regex "$NAME_RE" || die "$RULES_DIR/names.re: the rules together are not a valid extended regular expression"
TAILNET_RE='(^|[^0-9.])100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
# Absolute home paths: any /home/<name> fails unless <name> is a generic placeholder used in upstream fixtures.
HOME_RE='(^|[^A-Za-z0-9_.~])/home/[A-Za-z0-9_-]+'
HOME_ALLOW='/home/(user|u|me|x|you|someone|ubuntu|ada|dana|dev|d|g|guest|fuzz|al|alex|linuxbrew)([^A-Za-z0-9_.-]|$)'

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
  failed="$work/grep-failed"
  if [ -z "$PERSONAL_RE$NAME_RE" ]; then
    printf '%s\n' "build_release: warning: no name rules in $RULES_DIR; only tailnet addresses and home paths were checked" >&2
  fi
  # grep exits 1 for "no match", which is fine, and 2 for an error (a bad regex, an unreadable file), which is not: a
  # scan that could not read everything has found nothing, so it must not pass. The pipelines below hide a left-hand
  # status, so an error is recorded in a file.
  grep_ok() { "$@" || [ $? -eq 1 ] || : >"$failed"; }
  (
    cd "$1" || exit 2
    {
      if [ -n "$PERSONAL_RE" ]; then grep_ok grep -rlaiE --exclude-dir=.git -e "$PERSONAL_RE" .; fi
      if [ -n "$NAME_RE" ]; then grep_ok grep -rlaE --exclude-dir=.git -e "$NAME_RE" .; fi
      grep_ok grep -rlaE --exclude-dir=.git -e "$TAILNET_RE" .
      grep_ok grep -raoHE --exclude-dir=.git -e "$HOME_RE" . | grep_ok grep -vaE -e "$HOME_ALLOW" | cut -d: -f1
      if [ -n "$PERSONAL_RE" ]; then find . -name .git -prune -o -print | grep_ok grep -iE -e "$PERSONAL_RE"; fi
    } | sort -u
  ) >"$hits" || die "cannot scan $1"
  [ ! -e "$failed" ] || die "the personal-data scan did not run to the end (grep failed: an unreadable file or a bad rule); it cannot pass"
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
# A trap that only cleans up would let the script carry on after the signal (and exit 0): on a signal, exit with the
# conventional 128+n status, which runs the EXIT trap.
trap 'rm -rf "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$' ||
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
