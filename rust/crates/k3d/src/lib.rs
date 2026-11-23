//! k3d, the k3code sidecar daemon: newline-delimited JSON-RPC 2.0 ([`k3_rpc`]) on a unix socket, guarded by the
//! same token-file scheme as the Python gateway (`core/src/k3code/gateway/auth.py`).
//!
//! * [`auth`]: the token next to the socket, the socket directory checks and the constant-time token compare
//! * [`reader`]: a line reader that drops a frame longer than the k3-rpc frame cap instead of buffering it
//! * [`server`]: the socket, one thread per connection, and the methods (`k3d.ping`, `k3d.version` in this stage)

pub mod auth;
pub mod reader;
pub mod server;
