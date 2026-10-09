# shellcheck shell=bash
# Helpers shared by scripts/ci/check.sh, scripts/ci/merge-pr.sh and scripts/release/release.sh. Source it; it runs
# nothing on its own.

# The commit status that branch protection on Main requires (it replaces the old GitHub Actions checks).
K3CI_CONTEXT=local-ci

die() {
  printf '%s: %s\n' "${K3CI_NAME:-k3code-ci}" "$*" >&2
  exit 2
}

note() {
  printf '%s: %s\n' "${K3CI_NAME:-k3code-ci}" "$*" >&2
}

have() {
  command -v "$1" >/dev/null 2>&1
}

# install_hint TOOL: one line on how to get TOOL.
install_hint() {
  case $1 in
    uv) echo "install uv: curl -LsSf https://astral.sh/uv/install.sh | sh (or see https://docs.astral.sh/uv/)" ;;
    node | npm | npx) echo "install Node 22+ with npm: https://nodejs.org/ (or dnf install nodejs / apt install nodejs npm / brew install node)" ;;
    go) echo "install Go (the version in panes/go.mod): https://go.dev/dl/ (or dnf install golang / brew install go)" ;;
    gitleaks) echo "install gitleaks 8.30.1 or newer: https://github.com/gitleaks/gitleaks/releases (or brew install gitleaks)" ;;
    shellcheck) echo "install shellcheck: dnf install ShellCheck / apt install shellcheck / brew install shellcheck" ;;
    bwrap) echo "install bubblewrap (the sandbox the core tests run under): dnf install bubblewrap / apt install bubblewrap" ;;
    gh) echo "install the GitHub CLI and sign in: https://cli.github.com/ then gh auth login" ;;
    python3) echo "install Python 3.12+ (python3 on PATH)" ;;
    cc | gcc) echo "install a C compiler for go test -race: dnf install gcc / apt install gcc / xcode-select --install" ;;
    *) echo "install $1 and put it on PATH" ;;
  esac
}

# repo_slug: owner/name of the GitHub repository this checkout belongs to.
repo_slug() {
  gh repo view --json nameWithOwner -q .nameWithOwner
}

# sha_on_origin SHA: true when a branch on origin contains SHA (after the last fetch).
sha_on_origin() {
  [ -n "$(git branch -r --contains "$1" 2>/dev/null | grep -E '^ *origin/' || true)" ]
}

# tree_clean: no staged, unstaged or untracked (not ignored) changes.
tree_clean() {
  [ -z "$(git status --porcelain --untracked-files=normal)" ]
}

# post_status SHA STATE DESCRIPTION: set the local-ci commit status on SHA. GitHub caps the description at 140
# characters.
post_status() {
  local sha=$1 state=$2 desc=$3 slug
  have gh || die "--post needs gh: $(install_hint gh)"
  slug=$(repo_slug) || die "gh repo view failed: is gh signed in (gh auth status)?"
  desc=${desc:0:140}
  gh api --silent "repos/$slug/statuses/$sha" -f state="$state" -f context="$K3CI_CONTEXT" -f description="$desc"
  note "posted $K3CI_CONTEXT=$state on ${sha:0:12} ($slug): $desc"
}

# strip_git_env: drop every GIT_* variable. A git hook exports GIT_DIR and friends; a test suite that runs git in a
# temporary repository would otherwise write into this one.
strip_git_env() {
  local v
  for v in $(compgen -e); do
    case $v in GIT_*) unset "$v" ;; esac
  done
}

# fmt_secs N: 75 -> 1m15s
fmt_secs() {
  if [ "$1" -ge 60 ]; then printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)); else printf '%ds' "$1"; fi
}
