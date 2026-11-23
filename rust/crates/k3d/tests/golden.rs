//! Golden frames: lines the Python gateway encodes or decodes (`fixtures/protocol_frames.jsonl`, written by
//! `fixtures/gen_protocol_frames.py`), checked against k3-rpc and k3d.
//!
//! Python's `json.dumps` puts a space after `:` and `,` and serde does not, so encoded frames are compared as JSON
//! values, not bytes. A decoded line must get the same verdict from both decoders, except the lines marked
//! `rust_differs` (Rust is stricter there), whose difference is pinned too.

use std::fs;

use k3_rpc::{Frame, Id, decode_frame, encode_error, encode_event, encode_request, encode_response};
use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/protocol_frames.jsonl");

fn records() -> Vec<Value> {
    let records: Vec<Value> = FIXTURE.lines().map(|l| serde_json::from_str(l).unwrap()).collect();
    assert!(records.len() >= 15, "the fixture lost its records");
    records
}

fn id(value: &Value) -> Option<Id> {
    match value {
        Value::Null => None,
        Value::String(s) => Some(Id::Str(s.clone())),
        other => Some(Id::Num(other.as_i64().expect("an integer id"))),
    }
}

fn opt(value: &Value) -> Option<&Value> {
    (!value.is_null()).then_some(value)
}

/// The line the Rust side produces for an encoder record.
fn rust_line(encoder: &str, args: &Value) -> String {
    match encoder {
        "response" => encode_response(&id(&args["id"]).unwrap(), &args["result"]),
        "error" => encode_error(
            id(&args["id"]).as_ref(),
            args["code"].as_i64().unwrap(),
            args["message"].as_str().unwrap(),
            opt(&args["data"]),
        ),
        "event" => encode_event(
            args["type"].as_str().unwrap(),
            opt(&args["payload"]),
            args["importance"].as_str(),
        ),
        "server_request" => encode_request(
            &id(&args["id"]).unwrap(),
            args["method"].as_str().unwrap(),
            &args["params"],
        ),
        "auth_frame" => auth_frame_for(args["token"].as_str().unwrap()),
        other => panic!("unknown encoder {other}"),
    }
}

/// k3d's auth frame for a token file holding `token`.
fn auth_frame_for(token: &str) -> String {
    let dir = std::env::temp_dir().join(format!("k3d-golden-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("k3d.sock");
    fs::write(k3d::auth::token_path(&sock), token).unwrap();
    let frame = k3d::auth::auth_frame(&sock);
    fs::remove_dir_all(&dir).unwrap();
    frame.expect("a readable token")
}

#[test]
fn rust_encodes_the_frames_python_encodes() {
    let mut checked = 0;
    for record in records() {
        let Some(encoder) = record["encoder"].as_str() else {
            continue;
        };
        let (name, py_line) = (&record["name"], record["line"].as_str().unwrap());
        let rs_line = rust_line(encoder, &record["args"]);
        let py: Value = serde_json::from_str(py_line).unwrap();
        let rs: Value = serde_json::from_str(&rs_line).unwrap();
        assert_eq!(rs, py, "{name}");
        assert_eq!(
            rs_line.ends_with('\n'),
            py_line.ends_with('\n'),
            "{name}: trailing newline"
        );
        assert!(!rs_line.trim_end().contains('\n'), "{name}: one frame is one line");
        checked += 1;
    }
    assert!(checked >= 9, "only {checked} encoder records");
}

#[test]
fn rust_decodes_python_lines_with_the_same_verdict() {
    for record in records() {
        let (name, line) = (&record["name"], record["line"].as_str().unwrap());
        let python_ok = record["python_ok"].as_bool().unwrap();
        let rust = decode_frame(line);
        match record["rust_differs"].as_str() {
            Some(why) => {
                assert!(python_ok, "{name}: a divergence is a line Python accepts");
                assert!(
                    rust.is_err(),
                    "{name}: Rust no longer rejects it ({why}); update the fixture"
                );
            }
            None => assert_eq!(rust.is_ok(), python_ok, "{name}: {rust:?}"),
        }
    }
}

#[test]
fn python_encoded_frames_decode_to_their_shape() {
    for record in records() {
        let Some(encoder) = record["encoder"].as_str() else {
            continue;
        };
        let (name, args) = (&record["name"], &record["args"]);
        let frame = decode_frame(record["line"].as_str().unwrap()).unwrap_or_else(|e| panic!("{name}: {e}"));
        match (encoder, frame) {
            ("response", Frame::Response { id: got, result }) => {
                assert_eq!((Some(got), result), (id(&args["id"]), args["result"].clone()), "{name}");
            }
            ("error", Frame::Error { id: got, error }) => {
                assert_eq!(got, id(&args["id"]), "{name}");
                assert_eq!(
                    (error.code, error.message.as_str()),
                    (args["code"].as_i64().unwrap(), args["message"].as_str().unwrap())
                );
                assert_eq!(error.data.as_ref(), opt(&args["data"]), "{name}");
            }
            ("event", frame @ Frame::Notification { .. }) => {
                let (ty, _) = frame.as_event().expect("an event");
                assert_eq!(ty, args["type"].as_str().unwrap(), "{name}");
            }
            (
                "server_request",
                Frame::Request {
                    id: got,
                    method,
                    params,
                },
            ) => {
                assert_eq!(Some(got), id(&args["id"]), "{name}");
                assert_eq!(
                    (method.as_str(), &params),
                    (args["method"].as_str().unwrap(), &args["params"])
                );
            }
            (
                "auth_frame",
                Frame::Request {
                    id: got,
                    method,
                    params,
                },
            ) => {
                assert_eq!(got, Id::Str(k3d::auth::AUTH_REQUEST_ID.into()));
                assert_eq!(method, k3d::auth::AUTH_METHOD);
                assert_eq!(params["token"], args["token"]);
            }
            (encoder, frame) => panic!("{name}: {encoder} decoded as {frame:?}"),
        }
    }
}
