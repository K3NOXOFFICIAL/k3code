//! `k3d [--socket PATH]`: serve the sidecar socket (default `$K3CODE_HOME/run/k3d.sock`, `K3CODE_HOME` defaulting
//! to `~/.k3code` as in `k3code.paths.home`). The token is written next to the socket as `k3d.token`.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use k3d::server::Server;

fn k3code_home() -> Option<PathBuf> {
    let home = env::var_os("HOME").map(PathBuf::from);
    match env::var_os("K3CODE_HOME").map(PathBuf::from) {
        Some(dir) => match dir.strip_prefix("~") {
            Ok(rest) => Some(home?.join(rest)),
            Err(_) => Some(dir),
        },
        None => Some(home?.join(".k3code")),
    }
}

fn main() -> ExitCode {
    let Some(run_dir) = k3code_home().map(|home| home.join("run")) else {
        eprintln!("k3d: neither K3CODE_HOME nor HOME is set");
        return ExitCode::FAILURE;
    };
    let mut args = env::args().skip(1);
    let sock = match (args.next().as_deref(), args.next(), args.next()) {
        (None, _, _) => run_dir.join("k3d.sock"),
        (Some("--socket"), Some(path), None) => PathBuf::from(path),
        _ => {
            eprintln!("usage: k3d [--socket PATH]");
            return ExitCode::from(2);
        }
    };
    let server = match Server::bind(&sock, &run_dir) {
        Ok(server) => server,
        Err(e) => {
            eprintln!("k3d: cannot serve {}: {e}", sock.display());
            return ExitCode::FAILURE;
        }
    };
    eprintln!("k3d: listening on {}", server.socket_path().display());
    server.serve();
    ExitCode::SUCCESS
}
