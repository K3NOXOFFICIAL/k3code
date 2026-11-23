//! Who may drive k3d: a per-daemon token in the private run directory, the twin of
//! `core/src/k3code/gateway/auth.py` (same file layout, same frame, same refusals).
//!
//! The daemon writes a fresh random token next to its socket (`k3d.sock` -> `k3d.token`, 0600) before the socket
//! exists; a connection may call a method only after it sent `gateway.auth {"token": ..}` with that token. As in the
//! gateway, the token keeps out a peer that can reach the socket but not read the run directory (a confined
//! process); a process of the same user outside any sandbox can read the token and is not kept out.
//!
//! The token is never logged: [`Token`] has no `Debug` or `Display`, and a wrong token is answered without echoing it.

use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use k3_rpc::{Id, encode_request};
use serde_json::json;

/// Request id of the auth frame the bundled clients send first (a string no TUI request id uses).
pub const AUTH_REQUEST_ID: &str = "k3-gateway-auth";
/// The gateway's auth method: k3d takes the same frame, so `auth.authenticate()` in Python drives k3d unchanged.
pub const AUTH_METHOD: &str = "gateway.auth";
/// Random bytes in a token; hex-encoded it is 64 characters, as `secrets.token_hex(32)` writes.
const TOKEN_BYTES: usize = 32;

/// A daemon token. Deliberately neither `Debug` nor `Display`, so it cannot end up in a log line by accident.
#[derive(Clone, PartialEq, Eq)]
pub struct Token(String);

impl Token {
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Whether `given` is this token, in time that does not depend on where the two differ.
    pub fn matches(&self, given: &str) -> bool {
        tokens_match(given.as_bytes(), self.0.as_bytes())
    }
}

/// Constant-time equality of two tokens (`hmac.compare_digest`): every byte is compared, whatever the first
/// difference. Only the length can leak, as with `compare_digest`, and every token k3d writes has the same length.
pub fn tokens_match(given: &[u8], expected: &[u8]) -> bool {
    if given.len() != expected.len() {
        return false;
    }
    let diff = given.iter().zip(expected).fold(0u8, |acc, (a, b)| acc | (a ^ b));
    std::hint::black_box(diff) == 0
}

/// The token file of the daemon serving `sock`: next to it (`k3d.sock` -> `k3d.token`).
pub fn token_path(sock: &Path) -> PathBuf {
    sock.with_extension("token")
}

/// The single-instance lock of the daemon serving `sock`: next to it, named after it (`k3d.sock` -> `k3d.lock`).
pub fn lock_path(sock: &Path) -> PathBuf {
    sock.with_extension("lock")
}

/// Take an exclusive, non-blocking flock on [`lock_path`] (`daemon.acquire_instance_lock`); keep the file to hold it.
///
/// Without it a second k3d on `sock` would pass the stale-socket check whenever the first one's socket is not
/// connectable (before its bind, or after the file was unlinked) and overwrite the first one's token.
pub fn acquire_instance_lock(sock: &Path) -> io::Result<File> {
    let path = lock_path(sock);
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&path)?;
    match file.try_lock() {
        Ok(()) => {}
        Err(fs::TryLockError::WouldBlock) => {
            return Err(io::Error::new(
                io::ErrorKind::AddrInUse,
                format!("another k3d holds {}; not serving {}", path.display(), sock.display()),
            ));
        }
        Err(fs::TryLockError::Error(e)) => return Err(e),
    }
    file.set_len(0)?;
    writeln!(file, "{}", std::process::id())?;
    Ok(file)
}

fn refused(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

/// The effective uid of this process: the owner of `/proc/self` (std has no `getuid`, and `unsafe` is forbidden).
pub fn current_uid() -> io::Result<u32> {
    Ok(fs::metadata("/proc/self")?.uid())
}

/// Create `path` as 0700, or tighten an existing one this user owns to 0700; refuse one another user owns
/// (`k3code.paths.ensure_private_dir`). Missing ancestors are created 0700 too; existing ones are left alone.
pub fn ensure_private_dir(path: &Path) -> io::Result<()> {
    let missing: Vec<&Path> = path.ancestors().take_while(|p| !p.exists()).collect();
    for dir in missing.into_iter().rev() {
        match DirBuilder::new().mode(0o700).create(dir) {
            Err(e) if e.kind() != io::ErrorKind::AlreadyExists => return Err(e),
            _ => fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?,
        }
    }
    let meta = fs::metadata(path)?;
    if meta.uid() != current_uid()? {
        return Err(refused(format!(
            "{} is owned by another user; k3code keeps private state there",
            path.display()
        )));
    }
    if meta.mode() & 0o777 != 0o700 {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Make `sock`'s directory safe for the socket and the token (`auth.prepare_socket_dir`).
///
/// `private_run_dir` (`<K3CODE_HOME>/run`) is ours: it is created or tightened to 0700. Any other directory is
/// never chmod'ed (it may be `$HOME` or `/tmp`): it is created 0700 when missing and refused when other users could
/// replace the socket or the token in it (group- or other-writable without the sticky bit).
pub fn prepare_socket_dir(sock: &Path, private_run_dir: &Path) -> io::Result<PathBuf> {
    let parent = sock.parent().unwrap_or(Path::new("."));
    if parent == private_run_dir {
        ensure_private_dir(parent)?;
        return Ok(parent.to_path_buf());
    }
    DirBuilder::new().mode(0o700).recursive(true).create(parent)?;
    let mode = fs::metadata(parent)?.mode();
    if mode & 0o022 != 0 && mode & 0o1000 == 0 {
        return Err(refused(format!(
            "{} is writable by other users; put the k3d socket in a directory only you can write to",
            parent.display()
        )));
    }
    Ok(parent.to_path_buf())
}

fn random_hex() -> io::Result<String> {
    let mut bytes = [0u8; TOKEN_BYTES];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// A fresh random token, written 0600 next to `sock` (replacing a stale one); returns it.
///
/// The old file is unlinked first and the new one is created exclusively (`O_CREAT|O_EXCL`), which also refuses to
/// follow a symlink planted at the path: the token is never written through an existing file or link.
pub fn write_token(sock: &Path) -> io::Result<Token> {
    let path = token_path(sock);
    let token = random_hex()?;
    match fs::remove_file(&path) {
        Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
        _ => {}
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)?;
    file.write_all(token.as_bytes())?;
    Ok(Token(token))
}

/// Remove the token file if it still holds `token` (a newer daemon's file stays).
pub fn remove_token(sock: &Path, token: &Token) {
    let path = token_path(sock);
    if read_token_file(&path).is_some_and(|t| token.matches(t.as_str())) {
        let _ = fs::remove_file(path);
    }
}

fn read_token_file(path: &Path) -> Option<Token> {
    let text = fs::read_to_string(path).ok()?;
    let text = text.trim();
    (!text.is_empty()).then(|| Token(text.to_owned()))
}

/// The token of the daemon serving `sock`, or `None` when it cannot be read or is empty.
pub fn read_token(sock: &Path) -> Option<Token> {
    read_token_file(&token_path(sock))
}

/// The `gateway.auth` request line a client sends first, or `None` without a readable token.
pub fn auth_frame(sock: &Path) -> Option<String> {
    let token = read_token(sock)?;
    let id = Id::Str(AUTH_REQUEST_ID.into());
    Some(encode_request(&id, AUTH_METHOD, &json!({"token": token.as_str()})) + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_match_only_on_equal_bytes() {
        assert!(tokens_match(b"abc", b"abc"));
        assert!(!tokens_match(b"abc", b"abd"));
        assert!(!tokens_match(b"xbc", b"abc"));
        assert!(!tokens_match(b"ab", b"abc"));
        assert!(!tokens_match(b"", b"abc"));
        assert!(tokens_match(b"", b""));
    }

    #[test]
    fn token_path_sits_next_to_the_socket() {
        assert_eq!(token_path(Path::new("/r/k3d.sock")), PathBuf::from("/r/k3d.token"));
        assert_eq!(token_path(Path::new("/r/k3d")), PathBuf::from("/r/k3d.token"));
    }

    #[test]
    fn lock_path_sits_next_to_the_socket() {
        assert_eq!(lock_path(Path::new("/r/k3d.sock")), PathBuf::from("/r/k3d.lock"));
        assert_eq!(lock_path(Path::new("/r/k3d")), PathBuf::from("/r/k3d.lock"));
    }

    #[test]
    fn tokens_are_64_hex_characters_and_differ() {
        let (a, b) = (random_hex().unwrap(), random_hex().unwrap());
        assert_eq!(a.len(), 2 * TOKEN_BYTES);
        assert!(a.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_ne!(a, b);
    }

    #[test]
    fn current_uid_is_the_owner_of_files_this_process_creates() {
        let dir = std::env::temp_dir().join(format!("k3d-uid-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let uid = fs::metadata(&dir).unwrap().uid();
        fs::remove_dir(&dir).unwrap();
        assert_eq!(current_uid().unwrap(), uid);
    }
}
