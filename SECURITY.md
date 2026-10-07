# Security policy

k3code runs commands and edits files on your machine, as your user. A security problem in it can mean that someone else's input makes it do that. Please report problems privately, and give us a chance to fix them before they are public.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

1. Open the repository on GitHub and choose the **Security** tab.
2. Choose **Report a vulnerability** and describe the problem.

Include the k3code version or commit, your operating system, the permission mode you were in, the steps to reproduce the problem, and what you expected to happen. Remove real API keys, tokens and private file contents from your report. A small fake example is enough.

If you cannot see the **Report a vulnerability** option, do not put the details in a public issue. Open an issue that says only that you have a security report and asks for a private channel. Nothing sensitive goes in that issue.

We are a small team. We will acknowledge a report as soon as we can, keep you informed while we work on it, and credit you in the changelog if you want that.

## Supported versions

k3code is alpha software. Only the latest commit on the development line receives fixes. There are no backports.

## What is in scope

- The core (`core/`): the gateway, the agent loop, the tools, the permission engine and its hardline list, the bash sandbox, the daemon and its socket, the webhook listener, redaction, bundle import and export, setup, and the updater.
- The terminal UI (`tui/`) where it handles data from the core, from a model or from a file.
- The `k3` multi-window terminal (`panes/`).
- The installer and the systemd unit (`install/`).

Examples: a way to run a denied command from a model's output or a file's contents; a secret that is written to a log, an export or a session file; a way to bypass the sandbox in a mode that claims to use it; a remote request that can reach the daemon or the webhook without the configured token.

## What is out of scope

- Anything that requires the attacker to already control your account or your shell.
- The documented behaviour of `yolo` mode. In `yolo`, tools run without asking, by design. It is not a security control; see the [Safety section of the README](README.md#safety).
- Wrong or harmful model output that does not get past the permission checks.
- Bugs in model providers, MCP servers or other upstream projects. Please report those to their maintainers.
- A command the hardline list does not match. The list is a set of patterns, not a sandbox, and the README says so. A new spelling that gets past it is useful to report as a normal bug. Please say that it is a hardline bypass.
- Findings from automated scanners with no demonstrated effect.

## Warning: yolo mode

In `yolo` mode, k3code runs tools without asking. Only the hardline list and explicit deny rules for non-shell tools still apply. Do not use `yolo` on a machine or in a project you cannot restore from backup, and do not give it to a model or a repository you do not trust.
