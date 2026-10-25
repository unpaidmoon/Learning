use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

fn run(input: &[u8], arguments: &[&str]) -> std::process::Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_learning-fsrs-helper"));
    command
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn binary_accepts_single_utf8_json_and_exits_without_queues() {
    let input = serde_json::to_vec(&json!({"protocol_version":1,"request_id":"process-check","operation":{"type":"eligibility","now_ms":1_760_000_000_000_i64,"due_ms":1_759_000_000_000_i64,"suspended":true,"removed":false}})).unwrap();
    let first = run(&input, &[]);
    let second = run(&input, &[]);
    assert!(first.status.success());
    assert!(first.stderr.is_empty());
    assert_eq!(first.stdout, second.stdout);
    let value: Value = serde_json::from_slice(&first.stdout).unwrap();
    assert_eq!(value["result"]["eligible"], false);
    assert_eq!(value["result"]["reason"], "suspended");
}

#[test]
fn binary_rejects_oversize_and_two_documents_with_structured_errors() {
    for (input, expected) in [
        (vec![b' '; 16_385], "FSRS_INPUT_TOO_LARGE"),
        (b"{}\n{}".to_vec(), "FSRS_INVALID_JSON"),
    ] {
        let output = run(&input, &[]);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stderr.is_empty());
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["error"]["code"], expected);
        assert!(output.stdout.len() <= 65_536);
    }
}

#[test]
fn binary_does_not_accept_file_or_shell_arguments() {
    let output = run(&[], &["--file", "not-a-file"]);
    assert_eq!(output.status.code(), Some(2));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["error"]["code"], "FSRS_ARGUMENTS_NOT_ALLOWED");
}
