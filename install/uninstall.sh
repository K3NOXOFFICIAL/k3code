#!/bin/sh
# Remove a k3code install: its versions, the private Node and Go, the k3code/k3 links, the systemd unit, and what
# presetup added under the install root (the Chromium location $DATA/browsers and the $DATA/presetup markers).
# Your data (~/.k3code, ~/.config/k3code) is kept unless you pass --purge, which asks you to type yes on a terminal
# (--yes answers it; without a terminal and without --yes, --purge is refused). uv is shared, so it stays.
# A k3code daemon, TUI or gateway still running from the install (one you started by hand; the systemd units are
# stopped first) is listed, and stopped once you type yes, or with --yes; without a terminal and without --yes the
# uninstall stops there. It would go on running from a deleted tree, and a purge would be undone by it.
#   sh uninstall.sh [--prefix DIR] [--purge] [--yes]
set -eu

usage() {
  echo "usage: uninstall.sh [--prefix DIR] [--purge] [--yes]   (--purge also deletes ~/.k3code and ~/.config/k3code;"
  echo "       --yes answers yes to the questions, including stopping a k3code still running from the install)"
  exit 0
}

# Prints the pids of this user's processes that run from the install root $1: their executable, or the program or script
# they were started with (argv[0] and argv[1]: a venv's python and `node tui/dist/entry.js` show it there), lies under
# it. Not this script or the processes that started it. Needs /proc (Linux); elsewhere it finds nothing.
running_from() {
  [ -d "/proc/$$" ] || return 0
  real=$(cd "$1" 2>/dev/null && pwd -P) || real=$1
  skip=" "
  p=$$
  while [ -n "$p" ] && [ "$p" -gt 1 ]; do
    skip="$skip$p "
    p=$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$p/status" 2>/dev/null) || p=""
  done
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    case "$skip" in *" $pid "*) continue ;; esac
    kill -0 "$pid" 2>/dev/null || continue # not one of yours
    hit=$(
      {
        readlink "$d/exe"
        tr '\0' '\n' <"$d/cmdline" | sed -n '1,2p'
      } 2>/dev/null | while IFS= read -r q; do
        case "$q" in "$1"/* | "$real"/*)
          echo "$pid"
          break
          ;;
        esac
      done
    ) || hit=""
    [ -z "$hit" ] || echo "$hit"
  done
}

pid_alive() { [ -d "/proc/$1" ] && ! grep -q '^State:[[:space:]]*Z' "/proc/$1/status" 2>/dev/null; }

alive_of() { # which of the pids given are still running (a zombie is gone: only its parent has yet to collect it)
  for p in "$@"; do pid_alive "$p" && echo "$p"; done
  return 0
}

# TERM, then KILL for what is still running after K3_UNINSTALL_STOP_WAIT seconds (default 10). Prints what is left.
stop_pids() {
  kill "$@" 2>/dev/null || true
  waited=0
  while [ -n "$(alive_of "$@")" ] && [ "$waited" -lt "${K3_UNINSTALL_STOP_WAIT:-10}" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  left=$(alive_of "$@")
  if [ -n "$left" ]; then
    # shellcheck disable=SC2086 # a list of pids
    kill -9 $left 2>/dev/null || true
    sleep 1
  fi
  alive_of "$@"
}

main() {
  PREFIX="$HOME/.local"
  PURGE=0
  YES=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge) PURGE=1 ;;
      --yes | -y) YES=1 ;;
      --prefix)
        [ $# -ge 2 ] || {
          echo "--prefix needs a directory" >&2
          exit 1
        }
        PREFIX=$2
        shift
        ;;
      -h | --help) usage ;;
      *)
        echo "unknown option: $1" >&2
        exit 1
        ;;
    esac
    shift
  done
  case "$PREFIX" in /*) ;; *) PREFIX="$(pwd)/$PREFIX" ;; esac
  DATA="${K3CODE_DATA:-$PREFIX/share/k3code}"
  BIN="${K3_BIN_DIR:-$PREFIX/bin}"
  user_data="${K3CODE_HOME:-$HOME/.k3code} and ${XDG_CONFIG_HOME:-$HOME/.config}/k3code"
  # Decided before anything is removed: the user data holds keys and sessions that no reinstall brings back.
  if [ "$PURGE" = 1 ] && [ "$YES" != 1 ]; then
    if ! (: </dev/tty) 2>/dev/null; then
      echo "--purge deletes $user_data (keys, sessions) and needs confirmation: pass --yes, or run it on a terminal" >&2
      exit 1
    fi
    printf '%s' "--purge deletes $user_data (keys, sessions). Type yes to continue: " >/dev/tty
    read -r ans </dev/tty || ans=""
    if [ "$ans" != yes ]; then
      echo "nothing removed" >&2
      exit 1
    fi
  fi

  units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" # where `k3code service install` writes them
  unit="$units/k3code.service"
  recover="$units/k3code-recover.service"
  had_units=0
  if [ -f "$unit" ] || [ -f "$recover" ]; then
    had_units=1
    # The probe's output is dropped: a broken install prints a Python traceback that says nothing useful here.
    if [ -x "$BIN/k3code" ] && "$BIN/k3code" --version >/dev/null 2>&1; then
      runnable=1
    else
      runnable=0
      echo "k3code is not runnable; removing units directly" >&2
    fi
    if [ "$runnable" = 1 ] && "$BIN/k3code" service uninstall; then
      :
    else # k3code is broken or gone: do what `k3code service uninstall` does
      if [ "$runnable" = 1 ]; then echo "k3code service uninstall failed; removing units directly" >&2; fi
      systemctl --user disable --now k3code.service k3code-recover.service 2>/dev/null || true
      rm -f "$unit" "$recover"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    for u in "$unit" "$recover"; do
      if [ -f "$u" ]; then echo "could not remove the systemd unit; remove $u by hand" >&2; fi
    done
  fi
  # After the units, so the daemon systemd runs is stopped by systemd and is not restarted.
  busy=""
  if [ -d "$DATA/versions" ] || [ -L "$DATA/current" ]; then busy=$(running_from "$DATA"); fi
  if [ -n "$busy" ]; then
    echo "k3code is still running from $DATA:" >&2
    for p in $busy; do
      printf '  %s  %s\n' "$p" "$(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | cut -c1-200)" >&2
    done
    if [ "$YES" != 1 ]; then
      if ! (: </dev/tty) 2>/dev/null; then
        echo "stop it first, or pass --yes to have uninstall.sh stop it; the install is left in place" >&2
        exit 1
      fi
      printf '%s' "Stop it and uninstall? Type yes to continue: " >/dev/tty
      read -r ans </dev/tty || ans=""
      if [ "$ans" != yes ]; then
        echo "nothing removed" >&2
        exit 1
      fi
    fi
    # shellcheck disable=SC2086 # a list of pids
    left=$(stop_pids $busy)
    if [ -n "$left" ]; then
      echo "could not stop the process(es) $(echo "$left" | tr '\n' ' '); stop them by hand and run uninstall.sh again" >&2
      exit 1
    fi
    echo "stopped $(echo "$busy" | tr '\n' ' ')" >&2
  fi
  for l in k3code k3; do
    t=$(readlink "$BIN/$l" 2>/dev/null || true)
    case "$t" in "$DATA"/*) rm -f "$BIN/$l" ;; esac
  done
  # Only delete a directory that really is a k3code install root.
  if [ -d "$DATA/versions" ] || [ -L "$DATA/current" ]; then
    rm -rf "$DATA"
    echo "removed $DATA"
  elif [ -e "$DATA" ]; then
    echo "$DATA does not look like a k3code install; left in place" >&2
  fi

  if [ "$PURGE" = 1 ]; then
    rm -rf "${K3CODE_HOME:-$HOME/.k3code}" "${XDG_CONFIG_HOME:-$HOME/.config}/k3code"
    echo "purged user data"
  else
    echo "kept $user_data (use --purge to delete)"
  fi
  echo "k3code uninstalled (system packages such as bubblewrap were left alone)"
  # `k3code service install` advises `loginctl enable-linger`; nothing here can know whether something else needs it
  me=$(id -un)
  if [ "$had_units" = 1 ] && command -v loginctl >/dev/null 2>&1 &&
    [ "$(loginctl show-user "$me" -p Linger 2>/dev/null)" = "Linger=yes" ]; then
    echo "lingering is still on for $me, as 'k3code service install' advised; if nothing else needs it: loginctl disable-linger $me" >&2
  fi
}

main "$@"
