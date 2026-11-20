//! JSON-RPC 2.0 framing for the k3code gateway: one JSON object per line, the same shapes as
//! `core/src/k3code/gateway/protocol.py` (the Python gateway is the reference implementation).
//!
//! * request:      `{"jsonrpc":"2.0","id":<n|s>,"method":..,"params":{..}}`
//! * notification: the same without `id` (no response is sent)
//! * response:     `{"jsonrpc":"2.0","id":..,"result":..}` or `{"jsonrpc":"2.0","id":..,"error":{"code","message"}}`
//! * event:        a notification with `method == "event"` and `params == {"type","payload"[,"importance"]}`

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;
pub const INTERNAL_ERROR: i64 = -32603;
/// Server-defined: the method needs a connection that sent `gateway.auth` with the daemon token.
pub const UNAUTHORIZED: i64 = -32001;
/// Server-defined: the connection already has the maximum of concurrent long-running requests in flight.
pub const TOO_MANY_REQUESTS: i64 = -32002;

/// A frame longer than this many characters is shrunk before it is sent (the TUI builds one JS string per line).
pub const MAX_FRAME_CHARS: usize = 8 * 1024 * 1024;
/// Strings longer than this are cut (head and tail kept) when a frame is over [`MAX_FRAME_CHARS`].
const SHRUNK_STRING_CHARS: usize = 64 * 1024;

/// A request id: the TUI uses numbers, the gateway's own server-to-client requests use strings (`k3-N`).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Id {
    Num(i64),
    Str(String),
}

/// The `error` member of an error response.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

/// One decoded frame.
#[derive(Debug, Clone, PartialEq)]
pub enum Frame {
    Request {
        id: Id,
        method: String,
        params: Value,
    },
    Notification {
        method: String,
        params: Value,
    },
    Response {
        id: Id,
        result: Value,
    },
    /// `id` is `None` for an error the sender could not tie to a request (e.g. a parse error).
    Error {
        id: Option<Id>,
        error: RpcError,
    },
}

/// A frame that could not be decoded; `code` is the JSON-RPC error code to answer with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError {
    pub code: i64,
    pub message: String,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} ({})", self.message, self.code)
    }
}

impl std::error::Error for DecodeError {}

fn invalid(message: &str) -> DecodeError {
    DecodeError {
        code: INVALID_REQUEST,
        message: format!("invalid request: {message}"),
    }
}

fn parse_id(value: &Value) -> Result<Option<Id>, DecodeError> {
    match value {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(Id::Str(s.clone()))),
        Value::Number(n) => n
            .as_i64()
            .map(|n| Some(Id::Num(n)))
            .ok_or_else(|| invalid("id must be an integer or a string")),
        _ => Err(invalid("id must be an integer or a string")),
    }
}

/// Parse one newline-delimited frame (the trailing newline, if any, is ignored).
pub fn decode_frame(line: &str) -> Result<Frame, DecodeError> {
    let value: Value = serde_json::from_str(line.trim_end_matches(['\r', '\n'])).map_err(|e| DecodeError {
        code: PARSE_ERROR,
        message: format!("parse error: {e}"),
    })?;
    let Value::Object(mut obj) = value else {
        return Err(invalid("frame must be an object"));
    };
    if obj.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err(invalid("missing or bad jsonrpc version"));
    }
    let id = obj.remove("id");
    if let Some(method) = obj.remove("method") {
        let Value::String(method) = method else {
            return Err(invalid("method must be a string"));
        };
        let params = obj.remove("params").unwrap_or(Value::Null);
        return match id {
            None => Ok(Frame::Notification { method, params }),
            Some(id) => match parse_id(&id)? {
                Some(id) => Ok(Frame::Request { id, method, params }),
                None => Err(invalid("request id must not be null")),
            },
        };
    }
    let id = id.as_ref().map(parse_id).transpose()?.flatten();
    if let Some(error) = obj.remove("error") {
        let error: RpcError = serde_json::from_value(error).map_err(|e| invalid(&format!("bad error member: {e}")))?;
        return Ok(Frame::Error { id, error });
    }
    if let Some(result) = obj.remove("result") {
        return match id {
            Some(id) => Ok(Frame::Response { id, result }),
            None => Err(invalid("response without an id")),
        };
    }
    Err(invalid("frame has no method, result or error"))
}

/// `value` with every string longer than `limit` characters cut to its head and tail around a marker.
fn shrink(value: &Value, limit: usize) -> Value {
    match value {
        Value::String(s) => {
            let len = s.chars().count();
            if len <= limit {
                return value.clone();
            }
            let half = limit / 2;
            let cut = len - 2 * half;
            let head: String = s.chars().take(half).collect();
            let tail: String = s.chars().skip(len - half).collect();
            Value::String(format!(
                "{head}\n[... {cut} characters cut: too large for one frame ...]\n{tail}"
            ))
        }
        Value::Array(items) => Value::Array(items.iter().map(|v| shrink(v, limit)).collect()),
        Value::Object(map) => Value::Object(map.iter().map(|(k, v)| (k.clone(), shrink(v, limit))).collect()),
        other => other.clone(),
    }
}

fn dump(frame: &Value) -> String {
    frame.to_string()
}

/// The frame as one JSON line; over [`MAX_FRAME_CHARS`] the strings under `pointer` are cut so the line fits
/// (the shape stays). `pointer` is a JSON pointer such as `/result` or `/params/payload`.
fn dump_capped(mut frame: Value, pointer: &str) -> String {
    let line = dump(&frame);
    if line.chars().count() <= MAX_FRAME_CHARS {
        return line;
    }
    for limit in [SHRUNK_STRING_CHARS, 4096, 256] {
        if let Some(target) = frame.pointer_mut(pointer) {
            *target = shrink(target, limit);
        }
        let line = dump(&frame);
        if line.chars().count() <= MAX_FRAME_CHARS {
            return line;
        }
    }
    if let Some(target) = frame.pointer_mut(pointer) {
        *target = json!({"error": "frame too large to send"});
    }
    dump(&frame)
}

pub fn encode_request(id: &Id, method: &str, params: &Value) -> String {
    dump(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))
}

pub fn encode_notification(method: &str, params: &Value) -> String {
    dump(&json!({"jsonrpc": "2.0", "method": method, "params": params}))
}

pub fn encode_response(id: &Id, result: &Value) -> String {
    dump_capped(json!({"jsonrpc": "2.0", "id": id, "result": result}), "/result")
}

pub fn encode_error(id: Option<&Id>, code: i64, message: &str, data: Option<&Value>) -> String {
    let mut error = json!({"code": code, "message": message});
    if let Some(data) = data {
        error["data"] = data.clone();
    }
    dump(&json!({"jsonrpc": "2.0", "id": id, "error": error}))
}

/// A server-to-client event. An absent or empty `payload` becomes `{}`.
pub fn encode_event(event_type: &str, payload: Option<&Value>, importance: Option<&str>) -> String {
    let payload = match payload {
        Some(Value::Null) | None => json!({}),
        Some(p) => p.clone(),
    };
    let mut params = json!({"type": event_type, "payload": payload});
    if let Some(importance) = importance.filter(|s| !s.is_empty()) {
        params["importance"] = json!(importance); // the TUI's focusPolicy reads it beside type/payload
    }
    dump_capped(
        json!({"jsonrpc": "2.0", "method": "event", "params": params}),
        "/params/payload",
    )
}

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Unique id for server-to-client request frames: `k3-1`, `k3-2`, ...
pub fn next_request_id(prefix: &str) -> Id {
    Id::Str(format!("{prefix}-{}", REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed)))
}

impl Frame {
    /// The `(type, payload)` of an event notification, if this frame is one.
    pub fn as_event(&self) -> Option<(&str, &Value)> {
        match self {
            Frame::Notification { method, params } if method == "event" => {
                Some((params.get("type")?.as_str()?, params.get("payload")?))
            }
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rt(line: &str) -> Frame {
        decode_frame(line).unwrap_or_else(|e| panic!("{line}: {e}"))
    }

    #[test]
    fn request_with_numeric_and_string_ids() {
        let f = rt(r#"{"jsonrpc":"2.0","id":7,"method":"session.list","params":{"cwd":"/x"}}"#);
        assert_eq!(
            f,
            Frame::Request {
                id: Id::Num(7),
                method: "session.list".into(),
                params: json!({"cwd": "/x"})
            }
        );
        let f = rt(r#"{"jsonrpc":"2.0","id":"k3-1","method":"approval","params":{}}"#);
        assert!(matches!(f, Frame::Request { id: Id::Str(ref s), .. } if s == "k3-1"));
    }

    #[test]
    fn notification_has_no_id_and_missing_params_is_null() {
        assert_eq!(
            rt(r#"{"jsonrpc":"2.0","method":"ping"}"#),
            Frame::Notification {
                method: "ping".into(),
                params: Value::Null
            }
        );
    }

    #[test]
    fn response_and_error_frames() {
        assert_eq!(
            rt(r#"{"jsonrpc":"2.0","id":3,"result":{"ok":true}}"#),
            Frame::Response {
                id: Id::Num(3),
                result: json!({"ok": true})
            }
        );
        let f = rt(r#"{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}"#);
        let error = RpcError {
            code: PARSE_ERROR,
            message: "parse error".into(),
            data: None,
        };
        assert_eq!(f, Frame::Error { id: None, error });
    }

    #[test]
    fn event_notification_exposes_type_and_payload() {
        let line = encode_event("message.delta", Some(&json!({"text": "hi"})), Some("high"));
        let f = rt(&line);
        let (ty, payload) = f.as_event().expect("event");
        assert_eq!((ty, payload), ("message.delta", &json!({"text": "hi"})));
        assert_eq!(
            serde_json::from_str::<Value>(&line).unwrap()["params"]["importance"],
            "high"
        );
        let empty: Value = serde_json::from_str(&encode_event("x", None, Some(""))).unwrap();
        assert_eq!(empty["params"], json!({"type": "x", "payload": {}}));
    }

    #[test]
    fn bad_frames_report_the_json_rpc_error_code() {
        for (line, code) in [
            ("not json", PARSE_ERROR),
            ("[1,2]", INVALID_REQUEST),
            (r#"{"id":1,"method":"x"}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"1.0","id":1,"method":"x"}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","id":null,"method":"x"}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","id":1.5,"method":"x"}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","id":[],"method":"x"}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","id":1,"method":5}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","id":1}"#, INVALID_REQUEST),
            (r#"{"jsonrpc":"2.0","result":1}"#, INVALID_REQUEST),
        ] {
            let err = decode_frame(line).expect_err(line);
            assert_eq!(err.code, code, "{line}: {err}");
        }
    }

    #[test]
    fn trailing_newline_is_ignored() {
        assert!(matches!(
            rt("{\"jsonrpc\":\"2.0\",\"method\":\"a\"}\r\n"),
            Frame::Notification { .. }
        ));
    }

    #[test]
    fn encoders_round_trip_through_the_decoder() {
        let params = json!({"a": [1, 2, {"b": "ü"}]});
        assert_eq!(
            rt(&encode_request(&Id::Num(1), "m", &params)),
            Frame::Request {
                id: Id::Num(1),
                method: "m".into(),
                params: params.clone()
            }
        );
        assert_eq!(
            rt(&encode_notification("n", &params)),
            Frame::Notification {
                method: "n".into(),
                params: params.clone()
            }
        );
        let id = Id::Str("k3-2".into());
        assert_eq!(
            rt(&encode_response(&id, &params)),
            Frame::Response { id, result: params }
        );
        let data = json!({"m": "x"});
        let e = rt(&encode_error(
            Some(&Id::Num(4)),
            METHOD_NOT_FOUND,
            "no such method",
            Some(&data),
        ));
        let error = RpcError {
            code: METHOD_NOT_FOUND,
            message: "no such method".into(),
            data: Some(data),
        };
        assert_eq!(
            e,
            Frame::Error {
                id: Some(Id::Num(4)),
                error
            }
        );
        assert!(
            !encode_response(&Id::Num(1), &json!("ü")).contains("\\u"),
            "non-ASCII stays UTF-8"
        );
        assert!(
            !encode_request(&Id::Num(1), "m", &json!({"a": "x\ny"})).contains('\n'),
            "one frame is one line"
        );
    }

    #[test]
    fn error_without_id_serialises_a_null_id() {
        let v: Value = serde_json::from_str(&encode_error(None, PARSE_ERROR, "x", None)).unwrap();
        assert_eq!(v["id"], Value::Null);
        assert!(v["error"].get("data").is_none());
    }

    #[test]
    fn oversized_response_is_shrunk_to_fit_and_keeps_its_shape() {
        let huge = "x".repeat(MAX_FRAME_CHARS + 1000);
        let line = encode_response(&Id::Num(1), &json!({"out": huge, "ok": true}));
        assert!(line.chars().count() <= MAX_FRAME_CHARS);
        let Frame::Response { id, result } = rt(&line) else {
            panic!("not a response")
        };
        assert_eq!(id, Id::Num(1));
        assert_eq!(result["ok"], true);
        let out = result["out"].as_str().unwrap();
        assert!(out.contains("characters cut: too large for one frame"));
        assert!(out.starts_with("xxx") && out.ends_with("xxx"), "head and tail are kept");
    }

    #[test]
    fn oversized_event_is_shrunk_inside_its_payload() {
        let line = encode_event(
            "tool.result",
            Some(&json!({"text": "y".repeat(MAX_FRAME_CHARS + 10)})),
            None,
        );
        assert!(line.chars().count() <= MAX_FRAME_CHARS);
        let f = rt(&line);
        let (ty, payload) = f.as_event().unwrap();
        assert_eq!(ty, "tool.result");
        assert!(payload["text"].as_str().unwrap().contains("characters cut"));
    }

    #[test]
    fn many_small_strings_that_cannot_shrink_enough_are_replaced() {
        // 40k strings of 250 chars stay under every cut limit but together exceed the frame cap
        let items: Vec<Value> = (0..40_000).map(|_| Value::String("z".repeat(250))).collect();
        let v = json!({"items": items});
        assert!(v.to_string().chars().count() > MAX_FRAME_CHARS);
        let line = encode_response(&Id::Num(9), &v);
        assert!(line.chars().count() <= MAX_FRAME_CHARS);
        let Frame::Response { result, .. } = rt(&line) else {
            panic!("not a response")
        };
        assert_eq!(result, json!({"error": "frame too large to send"}));
    }

    #[test]
    fn shrink_counts_characters_not_bytes() {
        let s = "é".repeat(100);
        let out = shrink(&Value::String(s.clone()), 10);
        let out = out.as_str().unwrap();
        assert!(out.starts_with(&"é".repeat(5)) && out.ends_with(&"é".repeat(5)));
        assert!(out.contains("90 characters cut"));
        assert_eq!(shrink(&Value::String(s.clone()), 100), Value::String(s));
    }

    #[test]
    fn request_ids_are_unique_and_prefixed() {
        let (a, b) = (next_request_id("k3"), next_request_id("k3"));
        assert_ne!(a, b);
        assert!(matches!(a, Id::Str(ref s) if s.starts_with("k3-")));
    }
}
