use learning_fsrs_helper::{failure, handle_json, MAX_INPUT_BYTES};
use std::io::{Read, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    let result = if std::env::args_os().len() != 1 {
        failure(
            "FSRS_ARGUMENTS_NOT_ALLOWED",
            "调度helper仅接受标准输入JSON。",
            2,
        )
    } else {
        let mut input = Vec::new();
        match std::io::stdin()
            .lock()
            .take((MAX_INPUT_BYTES + 1) as u64)
            .read_to_end(&mut input)
        {
            Ok(_) => handle_json(&input),
            Err(_) => failure("FSRS_INPUT_IO_FAILED", "无法读取调度标准输入。", 3),
        }
    };
    let mut stdout = std::io::stdout().lock();
    if stdout
        .write_all(&result.1)
        .and_then(|_| stdout.flush())
        .is_err()
    {
        return ExitCode::from(3);
    }
    ExitCode::from(result.0)
}
