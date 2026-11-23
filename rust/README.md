# k3code Rust workspace

Staged migration of k3code (plan: jobs and voice first, then the TUI, then the core). New Rust code talks to the
Python gateway over the same newline-delimited JSON-RPC 2.0 contract (`docs/tui-contract.md`).

| crate | purpose |
|---|---|
| `k3-rpc` | JSON-RPC framing, the twin of `core/src/k3code/gateway/protocol.py` |
| `k3d` | sidecar daemon: JSON-RPC on a unix socket with the gateway's token-file auth (`k3d.ping`, `k3d.version` so far) |

Build and check: `cd rust && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test`
(`scripts/ci/check.sh --only rust`). Rust is installed with rustup; `~/.cargo/bin` must be on PATH.
