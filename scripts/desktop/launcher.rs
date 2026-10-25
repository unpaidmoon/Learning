#![windows_subsystem = "windows"]

use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};

#[link(name = "user32")]
unsafe extern "system" {
    fn MessageBoxW(window: *mut core::ffi::c_void, text: *const u16, title: *const u16, flags: u32) -> i32;
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

fn main() {
    let result = (|| -> std::io::Result<()> {
        let executable = std::env::current_exe()?;
        let root = executable.parent().ok_or_else(|| std::io::Error::other("DESKTOP_ROOT_MISSING"))?;
        let python = root.join("runtime/python/pythonw.exe");
        let entry = root.join("scripts/desktop/portable.py");
        if !python.is_file() || !entry.is_file() || !root.join("desktop-release.json").is_file() {
            return Err(std::io::Error::other("DESKTOP_RELEASE_INCOMPLETE"));
        }
        Command::new(python)
            .arg("-I").arg("-X").arg("utf8").arg(entry).args(std::env::args_os().skip(1))
            .current_dir(root).creation_flags(0x08000000)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn()?;
        Ok(())
    })();
    if let Err(error) = result {
        let message = wide(&format!("DESKTOP_START_FAILED：无法启动学习软件。请确认已完整解压运行包。\n{error}"));
        let title = wide("AI 辅助学习");
        unsafe { MessageBoxW(std::ptr::null_mut(), message.as_ptr(), title.as_ptr(), 0x10); }
    }
}
