//! The k3d socket: one JSON-RPC connection per client, each on its own thread.
//!
//! Dispatch follows the Python gateway (`_handle_line` / `_authenticate` in `gateway/server.py`): a frame that does
//! not decode is answered with a null-id error, a notification gets no reply, `gateway.auth` is answered first, an
//! unknown method is `METHOD_NOT_FOUND`, and every other method needs an authenticated connection. k3d has no open
//! methods: unlike the gateway's read-only `OPEN_METHODS`, nothing here is answered before `gateway.auth`.

use std::fs;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;

use k3_rpc::{Frame, INVALID_REQUEST, Id, METHOD_NOT_FOUND, UNAUTHORIZED, decode_frame, encode_error, encode_response};
use serde_json::{Value, json};

use crate::auth::{self, AUTH_METHOD, Token};
use crate::reader::{Line, MAX_FRAME_BYTES, read_line};

/// The methods k3d serves in this stage.
const METHODS: [&str; 2] = ["k3d.ping", "k3d.version"];

fn call(method: &str) -> Value {
    match method {
        "k3d.version" => json!({"name": "k3d", "version": env!("CARGO_PKG_VERSION")}),
        _ => json!({"ok": true}),
    }
}

/// What one connection knows about its peer.
pub struct Connection<'a> {
    token: &'a Token,
    authenticated: bool,
}

impl<'a> Connection<'a> {
    pub fn new(token: &'a Token) -> Self {
        Connection {
            token,
            authenticated: false,
        }
    }

    pub fn authenticated(&self) -> bool {
        self.authenticated
    }

    /// The reply line to one frame, or `None` when it needs none.
    pub fn handle(&mut self, text: &str) -> Option<String> {
        let (id, method, params) = match decode_frame(text) {
            Err(e) => return Some(encode_error(None, e.code, &e.message, None)),
            Ok(Frame::Request { id, method, params }) => (id, method, params),
            // a notification, or an answer to a server request k3d never sends
            Ok(_) => return None,
        };
        if method == AUTH_METHOD {
            return Some(self.authenticate(&id, &params));
        }
        if !METHODS.contains(&method.as_str()) {
            return Some(encode_error(
                Some(&id),
                METHOD_NOT_FOUND,
                &format!("Method not found: {method}"),
                None,
            ));
        }
        if !self.authenticated {
            let msg =
                format!("{method} needs an authenticated connection (send {AUTH_METHOD} with the daemon token first)");
            return Some(encode_error(Some(&id), UNAUTHORIZED, &msg, None));
        }
        Some(encode_response(&id, &call(&method)))
    }

    /// `gateway.auth {token}`: authenticate the connection when the token is this daemon's.
    fn authenticate(&mut self, id: &Id, params: &Value) -> String {
        let given = params.get("token").and_then(Value::as_str);
        if given.is_some_and(|given| self.token.matches(given)) {
            self.authenticated = true;
            return encode_response(id, &json!({"ok": true}));
        }
        eprintln!("k3d: a client sent a wrong or missing gateway token");
        encode_error(Some(id), UNAUTHORIZED, "wrong or missing gateway token", None)
    }
}

/// How a frame cap reads in an error message, as the gateway words it.
fn limit_text(limit: usize) -> String {
    if limit >= 1 << 20 {
        format!("{} MiB", limit >> 20)
    } else {
        format!("{limit} bytes")
    }
}

/// Serve one connection until EOF: frames from `reader`, replies to `writer`, lines over `limit` bytes dropped.
pub fn serve_connection<R: BufRead, W: Write>(
    mut reader: R,
    mut writer: W,
    token: &Token,
    limit: usize,
) -> io::Result<()> {
    let mut conn = Connection::new(token);
    loop {
        let reply = match read_line(&mut reader, limit)? {
            Line::Eof => return Ok(()),
            Line::TooLong => Some(encode_error(
                None,
                INVALID_REQUEST,
                &format!("frame longer than {}: dropped", limit_text(limit)),
                None,
            )),
            Line::Frame(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let text = text.trim();
                if text.is_empty() {
                    continue;
                }
                conn.handle(text)
            }
        };
        if let Some(reply) = reply {
            writer.write_all(reply.as_bytes())?;
            writer.write_all(b"\n")?;
            writer.flush()?;
        }
    }
}

/// A bound k3d socket with its token. Dropping it removes the socket (if it is still this one) and the token file
/// (if it still holds this token), as the gateway's `stop_socket` does.
pub struct Server {
    listener: UnixListener,
    sock: PathBuf,
    ino: u64,
    token: Arc<Token>,
    /// The flock on `<socket>.lock`, held for the server's lifetime. Fields drop after `Drop::drop`, so the lock is
    /// released only once the socket and token are cleaned up; the lock file stays, as the Python daemon's does.
    _lock: fs::File,
}

impl Server {
    /// Prepare the directory, take the instance lock, write the token, then bind `sock` 0600. While another k3d
    /// holds the lock this fails without touching its socket or token. A socket another process still serves is
    /// never taken over; a stale one (nothing answers) is replaced.
    pub fn bind(sock: &Path, private_run_dir: &Path) -> io::Result<Server> {
        auth::prepare_socket_dir(sock, private_run_dir)?;
        // one k3d per socket: the lock comes before anything that could touch another instance's socket or token
        let lock = auth::acquire_instance_lock(sock)?;
        if fs::symlink_metadata(sock).is_ok() {
            if UnixStream::connect(sock).is_ok() {
                return Err(io::Error::new(
                    io::ErrorKind::AddrInUse,
                    format!("{} is served by another process; not taking it over", sock.display()),
                ));
            }
            fs::remove_file(sock)?;
        }
        // The token exists before the socket does: a client that sees the socket can authenticate.
        let token = auth::write_token(sock)?;
        let listener = UnixListener::bind(sock)?;
        // std cannot set the umask without `unsafe`; the default run dir is 0700, so nobody else can reach the
        // socket in the moment before this chmod
        fs::set_permissions(sock, fs::Permissions::from_mode(0o600))?;
        let ino = fs::metadata(sock)?.ino();
        Ok(Server {
            listener,
            sock: sock.to_path_buf(),
            ino,
            token: Arc::new(token),
            _lock: lock,
        })
    }

    pub fn socket_path(&self) -> &Path {
        &self.sock
    }

    /// Accept connections forever, one thread each.
    pub fn serve(&self) {
        for stream in self.listener.incoming() {
            let stream = match stream {
                Ok(stream) => stream,
                Err(e) => {
                    eprintln!("k3d: accept failed: {e}");
                    continue;
                }
            };
            let token = Arc::clone(&self.token);
            thread::spawn(move || {
                let reader = match stream.try_clone() {
                    Ok(read_half) => BufReader::new(read_half),
                    Err(e) => return eprintln!("k3d: cannot read the connection: {e}"),
                };
                if let Err(e) = serve_connection(reader, &stream, &token, MAX_FRAME_BYTES) {
                    eprintln!("k3d: connection closed: {e}");
                }
            });
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if fs::metadata(&self.sock).is_ok_and(|m| m.ino() == self.ino) {
            let _ = fs::remove_file(&self.sock);
        }
        auth::remove_token(&self.sock, &self.token);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn token() -> Token {
        let dir = std::env::temp_dir().join(format!("k3d-srv-{}-{:?}", std::process::id(), thread::current().id()));
        fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("s.sock");
        let token = auth::write_token(&sock).unwrap();
        fs::remove_file(auth::token_path(&sock)).unwrap();
        fs::remove_dir(&dir).unwrap();
        token
    }

    fn reply(conn: &mut Connection, line: &str) -> Value {
        serde_json::from_str(&conn.handle(line).expect("a reply")).unwrap()
    }

    #[test]
    fn unknown_methods_are_not_found_before_any_auth_check() {
        let token = token();
        let mut conn = Connection::new(&token);
        let r = reply(&mut conn, r#"{"jsonrpc":"2.0","id":1,"method":"jobs.list"}"#);
        assert_eq!(r["error"]["code"], METHOD_NOT_FOUND);
        assert_eq!(r["id"], 1);
    }

    #[test]
    fn notifications_and_stray_answers_get_no_reply() {
        let token = token();
        let mut conn = Connection::new(&token);
        assert_eq!(conn.handle(r#"{"jsonrpc":"2.0","method":"k3d.ping"}"#), None);
        assert_eq!(conn.handle(r#"{"jsonrpc":"2.0","id":"k3-1","result":{}}"#), None);
    }

    #[test]
    fn bad_frames_get_a_null_id_error_with_the_decoder_code() {
        let token = token();
        let mut conn = Connection::new(&token);
        let r = reply(&mut conn, "not json");
        assert_eq!(
            (r["id"].clone(), r["error"]["code"].clone()),
            (Value::Null, json!(-32700))
        );
        let r = reply(&mut conn, r#"{"jsonrpc":"1.0","id":1,"method":"k3d.ping"}"#);
        assert_eq!(r["error"]["code"], INVALID_REQUEST);
    }

    #[test]
    fn the_right_token_unlocks_ping_and_version() {
        let token = token();
        let mut conn = Connection::new(&token);
        let auth = json!({"jsonrpc": "2.0", "id": "a", "method": AUTH_METHOD, "params": {"token": token.as_str()}});
        assert_eq!(reply(&mut conn, &auth.to_string())["result"], json!({"ok": true}));
        assert!(conn.authenticated());
        assert_eq!(
            reply(&mut conn, r#"{"jsonrpc":"2.0","id":2,"method":"k3d.ping"}"#)["result"],
            json!({"ok": true})
        );
        let v = reply(&mut conn, r#"{"jsonrpc":"2.0","id":3,"method":"k3d.version"}"#);
        assert_eq!(v["result"]["name"], "k3d");
        assert_eq!(v["result"]["version"], env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn blank_lines_are_skipped_and_replies_are_one_line_each() {
        let token = token();
        let input = "\n  \n{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"k3d.ping\"}\n";
        let mut out = Vec::new();
        serve_connection(Cursor::new(input), &mut out, &token, 1024).unwrap();
        let out = String::from_utf8(out).unwrap();
        assert_eq!(out.lines().count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn limit_text_matches_the_gateway_wording() {
        assert_eq!(limit_text(8 << 20), "8 MiB");
        assert_eq!(limit_text(100), "100 bytes");
    }
}
