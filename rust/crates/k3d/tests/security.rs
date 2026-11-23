//! Security tests over a real socket: the token, the frame cap and the permission checks of `gateway/auth.py`.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::thread;

use k3_rpc::{INVALID_REQUEST, UNAUTHORIZED};
use k3d::auth::{self, AUTH_METHOD, AUTH_REQUEST_ID};
use k3d::reader::MAX_FRAME_BYTES;
use k3d::server::Server;
use serde_json::{Value, json};

/// A fresh directory under the system temp dir (short: a socket path must stay under ~108 bytes), removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Self {
        static N: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!(
            "k3d-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        Scratch(dir)
    }

    fn sock(&self) -> PathBuf {
        let sock = self.0.join("k3d.sock");
        assert!(sock.as_os_str().len() < 100, "socket path too long: {}", sock.display());
        sock
    }

    /// A run directory k3d treats as its own private one (it never exists here before the test).
    fn run_dir(&self) -> PathBuf {
        self.0.join("run")
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).unwrap().mode() & 0o7777
}

/// A server on `sock`, serving on a background thread for the rest of the test process.
fn serve(sock: &Path, run_dir: &Path) -> Arc<Server> {
    let server = Arc::new(Server::bind(sock, run_dir).unwrap());
    let serving = Arc::clone(&server);
    thread::spawn(move || serving.serve());
    server
}

struct Client {
    writer: UnixStream,
    reader: BufReader<UnixStream>,
}

impl Client {
    fn connect(sock: &Path) -> Self {
        let writer = UnixStream::connect(sock).unwrap();
        let reader = BufReader::new(writer.try_clone().unwrap());
        Client { writer, reader }
    }

    fn send(&mut self, line: &str) -> Value {
        self.writer.write_all(line.as_bytes()).unwrap();
        if !line.ends_with('\n') {
            self.writer.write_all(b"\n").unwrap();
        }
        self.read()
    }

    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.reader.read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("{line:?}: {e}"))
    }

    fn auth(&mut self, token: Value) -> Value {
        let frame = json!({"jsonrpc": "2.0", "id": AUTH_REQUEST_ID, "method": AUTH_METHOD, "params": {"token": token}});
        self.send(&frame.to_string())
    }

    fn ping(&mut self) -> Value {
        self.send(r#"{"jsonrpc":"2.0","id":7,"method":"k3d.ping"}"#)
    }
}

#[test]
fn the_token_file_authenticates_a_connection() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let mut client = Client::connect(&sock);
    let reply = client.send(&auth::auth_frame(&sock).expect("a readable token"));
    assert_eq!(
        reply,
        json!({"jsonrpc": "2.0", "id": AUTH_REQUEST_ID, "result": {"ok": true}})
    );
    assert_eq!(client.ping()["result"], json!({"ok": true}));
    let version = client.send(r#"{"jsonrpc":"2.0","id":8,"method":"k3d.version"}"#);
    assert_eq!(version["result"]["name"], "k3d");
}

#[test]
fn a_wrong_token_is_rejected_and_the_connection_stays_locked() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let real = auth::read_token(&sock).unwrap();
    // same length, last character changed: only a full comparison tells them apart
    let mut wrong = real.as_str().to_owned();
    let last = if wrong.ends_with('0') { "1" } else { "0" };
    wrong.replace_range(wrong.len() - 1.., last);
    let mut client = Client::connect(&sock);
    for token in [json!(wrong), json!("short"), json!("")] {
        let reply = client.auth(token);
        assert_eq!(reply["error"]["code"], UNAUTHORIZED, "{reply}");
        assert_eq!(reply["error"]["message"], "wrong or missing gateway token");
        assert!(
            !reply.to_string().contains(real.as_str()),
            "the reply never carries the token"
        );
    }
    let ping = client.ping();
    assert_eq!(ping["error"]["code"], UNAUTHORIZED, "{ping}");
    assert!(ping.get("result").is_none());
}

#[test]
fn a_missing_or_non_string_token_is_rejected() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let mut client = Client::connect(&sock);
    for params in [json!({}), json!({"token": 5}), json!({"token": null}), json!(["x"])] {
        let frame = json!({"jsonrpc": "2.0", "id": 1, "method": AUTH_METHOD, "params": params});
        assert_eq!(
            client.send(&frame.to_string())["error"]["code"],
            UNAUTHORIZED,
            "{params}"
        );
    }
    let frame = json!({"jsonrpc": "2.0", "id": 1, "method": AUTH_METHOD});
    assert_eq!(client.send(&frame.to_string())["error"]["code"], UNAUTHORIZED);
    assert_eq!(client.ping()["error"]["code"], UNAUTHORIZED);
}

#[test]
fn a_missing_or_empty_token_file_gives_no_auth_frame() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    assert!(auth::read_token(&sock).is_none());
    assert!(auth::auth_frame(&sock).is_none());
    fs::write(auth::token_path(&sock), " \n").unwrap();
    assert!(auth::read_token(&sock).is_none());
    assert!(auth::auth_frame(&sock).is_none());
}

#[test]
fn every_method_needs_auth() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let mut client = Client::connect(&sock);
    for method in ["k3d.ping", "k3d.version"] {
        let reply = client.send(&json!({"jsonrpc": "2.0", "id": 1, "method": method}).to_string());
        assert_eq!(reply["error"]["code"], UNAUTHORIZED, "{method}");
    }
}

#[test]
fn an_oversized_frame_is_rejected_and_the_connection_stays_up() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let mut client = Client::connect(&sock);
    let mut huge = vec![b'x'; MAX_FRAME_BYTES + 1];
    huge.push(b'\n');
    client.writer.write_all(&huge).unwrap();
    let reply = client.read();
    assert_eq!(reply["id"], Value::Null);
    assert_eq!(reply["error"]["code"], INVALID_REQUEST);
    assert_eq!(reply["error"]["message"], "frame longer than 8 MiB: dropped");
    assert_eq!(
        client.send(&auth::auth_frame(&sock).unwrap())["result"],
        json!({"ok": true})
    );
    assert_eq!(client.ping()["result"], json!({"ok": true}));
}

#[test]
fn a_frame_at_the_cap_is_still_read() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let mut client = Client::connect(&sock);
    let body = r#"{"jsonrpc":"2.0","id":7,"method":"k3d.ping"}"#;
    let line = format!("{body}{}\n", " ".repeat(MAX_FRAME_BYTES - body.len()));
    assert_eq!(
        client.send(&line)["error"]["code"],
        UNAUTHORIZED,
        "decoded, then refused for want of auth"
    );
}

#[test]
fn the_token_and_the_socket_are_0600_and_the_run_dir_0700() {
    let scratch = Scratch::new();
    let run_dir = scratch.run_dir();
    fs::create_dir(&run_dir).unwrap();
    fs::set_permissions(&run_dir, fs::Permissions::from_mode(0o755)).unwrap();
    let sock = run_dir.join("k3d.sock");
    let _server = Server::bind(&sock, &run_dir).unwrap();
    assert_eq!(mode(&run_dir), 0o700, "the private run dir is tightened");
    assert_eq!(mode(&auth::token_path(&sock)), 0o600);
    assert_eq!(mode(&sock), 0o600);
}

#[test]
fn a_custom_dir_other_users_can_write_is_refused_unless_sticky() {
    let scratch = Scratch::new();
    let shared = scratch.0.join("shared");
    fs::create_dir(&shared).unwrap();
    fs::set_permissions(&shared, fs::Permissions::from_mode(0o777)).unwrap();
    let sock = shared.join("k3d.sock");
    let err = Server::bind(&sock, &scratch.run_dir()).err().expect("refused");
    assert!(err.to_string().contains("writable by other users"), "{err}");
    assert!(
        !auth::token_path(&sock).exists(),
        "no token is written into such a directory"
    );
    assert_eq!(mode(&shared), 0o777, "a custom directory is never chmod'ed");

    fs::set_permissions(&shared, fs::Permissions::from_mode(0o1777)).unwrap();
    let _server = Server::bind(&sock, &scratch.run_dir()).unwrap();
    assert!(auth::token_path(&sock).exists());
}

#[test]
fn a_missing_custom_dir_is_created_0700() {
    let scratch = Scratch::new();
    let dir = scratch.0.join("a").join("b");
    let _server = Server::bind(&dir.join("k3d.sock"), &scratch.run_dir()).unwrap();
    assert_eq!(mode(&dir), 0o700);
}

#[test]
fn a_symlink_at_the_token_path_is_replaced_not_written_through() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let target = scratch.0.join("target");
    fs::write(&target, "keep").unwrap();
    symlink(&target, auth::token_path(&sock)).unwrap();
    let token = auth::write_token(&sock).unwrap();
    assert_eq!(fs::read_to_string(&target).unwrap(), "keep");
    let meta = fs::symlink_metadata(auth::token_path(&sock)).unwrap();
    assert!(meta.file_type().is_file());
    assert_eq!(fs::read_to_string(auth::token_path(&sock)).unwrap(), token.as_str());
}

#[test]
fn a_live_socket_is_not_taken_over_and_a_stale_one_is_replaced() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    let _server = serve(&sock, &scratch.run_dir());
    let token = auth::read_token(&sock).unwrap();
    let err = Server::bind(&sock, &scratch.run_dir()).err().expect("refused");
    assert!(err.to_string().contains("served by another process"), "{err}");
    assert!(
        auth::read_token(&sock) == Some(token),
        "the live daemon keeps its token"
    );

    let stale = scratch.0.join("stale.sock");
    drop(std::os::unix::net::UnixListener::bind(&stale).unwrap());
    assert!(stale.exists());
    let _server = Server::bind(&stale, &scratch.run_dir()).unwrap();
    assert!(UnixStream::connect(&stale).is_ok());
}

#[test]
fn dropping_the_server_removes_its_socket_and_token_but_not_a_newer_token() {
    let scratch = Scratch::new();
    let sock = scratch.sock();
    drop(Server::bind(&sock, &scratch.run_dir()).unwrap());
    assert!(!sock.exists());
    assert!(!auth::token_path(&sock).exists());

    let server = Server::bind(&sock, &scratch.run_dir()).unwrap();
    auth::write_token(&sock).unwrap(); // a newer daemon's token
    drop(server);
    assert!(auth::read_token(&sock).is_some(), "a newer daemon's token stays");
}
