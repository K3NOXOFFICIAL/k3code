# Contributing to k3code

Thank you for looking at k3code. It is an alpha project run by a small team, so please open an issue before you start on anything large. This file explains how to build, test and submit a change.

## Setup

k3code has three parts. You only need the toolchain for the part you change.

```sh
# core: Python 3.12+ with uv
cd core && uv sync
uv run pytest -q
uv run ruff check src tests

# TUI: Node 22+
cd tui && npm ci
npm run build:ink && npm run build
npx vitest run                    # tui/.ci-test-excludes is empty, so CI runs every test

# panes (the k3 multi-window terminal): Go, only the packages we depend on
cd panes && go build ./cmd/k3
go test ./internal/k3keys/... ./internal/harness/... ./internal/input/ ./internal/app/
```

Do not run `go test ./...` in `panes/`. Its upstream remote-sync tests recurse without bound.

License bookkeeping is checked with `python3 scripts/vendor_check.py`.

CI runs the core, TUI, panes and vendor checks on every pull request (`.github/workflows/ci.yml`). A secret scan runs too (`.github/workflows/gitleaks.yml`). The CI core job runs `uv run ruff check .` and `uv run pytest -q` in `core/`, so `core/scripts` is linted too.

## Before you open a pull request

- The core test suite and `ruff check src tests` must pass.
- A behaviour change needs a test. A bug fix needs a test that fails without the fix.
- Security-sensitive code (permissions, the hardline list, the sandbox, the daemon socket, redaction, bundle import and export, the updater) needs a test and a line in `CHANGELOG.md`.
- Do not commit secrets, not even in tests. Use obviously fake values, and build them at run time when they would match a secret pattern. The secret scan fails on new matches.

## The TUI is a frozen fork

`tui/` (and `tui/shared/`) is a heavily modified fork of the Hermes Agent terminal UI. It is not synced with upstream. Do not copy upstream files over it or merge upstream into it.

To take an upstream fix, apply the change by hand to the matching file under `tui/`, following the steps in [`docs/UPSTREAM.md`](docs/UPSTREAM.md). Name the upstream commit in your commit message. Leave the base commit recorded in `VENDOR.toml` unchanged.

`panes/` is a TUIOS fork with its own merge procedure, also in `docs/UPSTREAM.md`.

## Licensing

k3code is MIT licensed. Accept only permissive licenses (MIT or Apache-2.0) for code and dependencies you add. If you copy or adapt a file from another project, add it to [`VENDOR.toml`](VENDOR.toml), keep its notice, and add the notice to [`NOTICE`](NOTICE) and [`LICENSES/`](LICENSES). Never add proprietary or leaked code, or closed binaries.

## Commit messages

Start the subject with the area the change touches, then a colon and a short summary, for example `core: keep the hardline list in config`. Use the imperative or the past tense, and keep the subject under about 72 characters. Put the reason for the change in the body. Older commits do not all follow this form, so match the area prefix and keep the style consistent within your branch.

Keep commits focused. A commit that mixes a refactor with a behaviour change is hard to review.

## Design documents

- [`docs/PLAN.md`](docs/PLAN.md): the design plan and the milestones (M0 to M6).
- [`docs/tui-contract.md`](docs/tui-contract.md): the protocol between the core and the TUI.
- [`docs/UPSTREAM.md`](docs/UPSTREAM.md): how vendored code is tracked and how upstream changes are taken in.
- [`docs/reports/`](docs/reports): one report per milestone or merge, and the exit-status table.
- [`panes/docs/`](panes/docs): documentation for the `k3` multi-window terminal.

If your change alters a design decision, update the relevant document in the same pull request.

## Reporting bugs and security issues

Bugs go in the issue tracker. Security problems must not: follow [`SECURITY.md`](SECURITY.md).
