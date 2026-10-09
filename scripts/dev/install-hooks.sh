#!/bin/sh
# Use the repository's git hooks (.githooks/: pre-commit, pre-push): sets core.hooksPath in this repository's own
# config, never the global one. Safe to run again. --uninstall removes the setting.
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

if [ "${1:-}" = "--uninstall" ]; then
  if git config --local --get core.hooksPath >/dev/null 2>&1; then
    git config --local --unset core.hooksPath
    echo "install-hooks: core.hooksPath removed; git uses .git/hooks again"
  else
    echo "install-hooks: core.hooksPath was not set"
  fi
  exit 0
fi
[ $# -eq 0 ] || {
  echo "usage: scripts/dev/install-hooks.sh [--uninstall]" >&2
  exit 2
}

[ -d .githooks ] || {
  echo "install-hooks: no .githooks directory in $root" >&2
  exit 1
}
chmod +x .githooks/*
current=$(git config --local --get core.hooksPath || true)
if [ "$current" = ".githooks" ]; then
  echo "install-hooks: already installed (core.hooksPath=.githooks)"
else
  git config --local core.hooksPath .githooks
  echo "install-hooks: core.hooksPath=.githooks (was: ${current:-unset})"
fi
echo "pre-commit: ruff format / prettier / gitleaks on staged files; pre-push: scripts/ci/check.sh (--quick, full for Main and tags)"
echo "skip once with K3CODE_SKIP_HOOKS=1"
