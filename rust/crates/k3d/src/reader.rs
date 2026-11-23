//! A bounded line reader: one frame per line, never more than the frame cap held in memory.
//!
//! The cap is [`k3_rpc::MAX_FRAME_CHARS`], which is also the Python gateway's read limit (`MAX_FRAME_BYTES = 8 << 20`
//! in `gateway/server.py`), counted here in bytes as asyncio counts it. A longer line is reported as
//! [`Line::TooLong`] after its tail has been skipped, so the connection can answer with an error and stay up.

use std::io::{self, BufRead, Read};

/// Bytes a frame may have, without its newline.
pub const MAX_FRAME_BYTES: usize = k3_rpc::MAX_FRAME_CHARS;

#[derive(Debug, PartialEq, Eq)]
pub enum Line {
    /// One line, its newline (if any: the last line before EOF may lack one) included.
    Frame(Vec<u8>),
    /// A line longer than the limit: it was read to its end and dropped.
    TooLong,
    Eof,
}

/// The next line of `reader`, at most `limit` bytes plus the newline.
pub fn read_line<R: BufRead>(reader: &mut R, limit: usize) -> io::Result<Line> {
    let mut buf = Vec::new();
    let max = u64::try_from(limit).unwrap_or(u64::MAX).saturating_add(1);
    if reader.by_ref().take(max).read_until(b'\n', &mut buf)? == 0 {
        return Ok(Line::Eof);
    }
    if buf.ends_with(b"\n") || buf.len() <= limit {
        return Ok(Line::Frame(buf));
    }
    drop(buf);
    skip_line(reader)?;
    Ok(Line::TooLong)
}

/// Consume input up to and including the next newline (or to EOF) without keeping it.
fn skip_line<R: BufRead>(reader: &mut R) -> io::Result<()> {
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            return Ok(());
        }
        if let Some(pos) = chunk.iter().position(|&b| b == b'\n') {
            reader.consume(pos + 1);
            return Ok(());
        }
        let len = chunk.len();
        reader.consume(len);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn lines(input: &[u8], limit: usize) -> Vec<Line> {
        let mut reader = Cursor::new(input.to_vec());
        let mut out = Vec::new();
        loop {
            let line = read_line(&mut reader, limit).unwrap();
            let eof = line == Line::Eof;
            out.push(line);
            if eof {
                return out;
            }
        }
    }

    fn frame(s: &str) -> Line {
        Line::Frame(s.as_bytes().to_vec())
    }

    #[test]
    fn lines_up_to_the_limit_pass_and_keep_their_newline() {
        assert_eq!(lines(b"abcd\nab\n", 4), [frame("abcd\n"), frame("ab\n"), Line::Eof]);
    }

    #[test]
    fn a_longer_line_is_dropped_whole_and_the_next_one_still_reads() {
        assert_eq!(lines(b"abcde\nok\n", 4), [Line::TooLong, frame("ok\n"), Line::Eof]);
        let long = [b"x".repeat(10_000), b"\n{}\n".to_vec()].concat();
        assert_eq!(lines(&long, 16), [Line::TooLong, frame("{}\n"), Line::Eof]);
    }

    #[test]
    fn a_last_line_without_newline_is_a_frame_unless_too_long() {
        assert_eq!(lines(b"abc", 4), [frame("abc"), Line::Eof]);
        assert_eq!(lines(b"abcde", 4), [Line::TooLong, Line::Eof]);
    }

    #[test]
    fn the_cap_is_the_k3_rpc_frame_limit() {
        assert_eq!(MAX_FRAME_BYTES, 8 << 20);
    }
}
