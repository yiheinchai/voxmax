//! Hotspot Guard desktop shell.
//!
//! The firewall work happens in the bundled Python engine (hotspot_guard.py). This file finds a
//! Python 3.8+ interpreter, runs the engine as the user for reads, asks the OS for administrator
//! rights for anything that changes the firewall, and exposes a small set of commands to the UI.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::OnceLock;
#[cfg(windows)]
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager};

/// Returned when the user dismisses the administrator prompt. The UI treats it as a quiet cancel.
const DENIED: &str = "Administrator permission was not granted.";

static PYTHON: OnceLock<String> = OnceLock::new();

/// Where the engine lives and what it needs to run.
struct Engine {
    python: String,
    script: PathBuf,
    config: PathBuf,
}

impl Engine {
    fn load(app: &AppHandle) -> Result<Engine, String> {
        let dir = engine_dir(app)?;
        let config_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
        fs::create_dir_all(&config_dir).map_err(|e| format!("cannot create {}: {e}", config_dir.display()))?;
        let config = config_dir.join("hotspot-guard.ini");
        if !config.exists() {
            fs::copy(dir.join("hotspot-guard.ini"), &config)
                .map_err(|e| format!("cannot create the allowlist: {e}"))?;
        }
        Ok(Engine {
            python: find_python()?,
            script: dir.join("hotspot_guard.py"),
            config,
        })
    }

    /// `python -B hotspot_guard.py --config <file> <args>` as argv entries. No shell is involved.
    fn argv(&self, args: &[&str]) -> Vec<String> {
        let mut argv = vec![
            self.python.clone(),
            "-B".into(),
            path_str(&self.script),
            "--config".into(),
            path_str(&self.config),
        ];
        argv.extend(args.iter().map(|arg| arg.to_string()));
        argv
    }
}

/// The bundled engine, or the source tree when running under `cargo tauri dev`.
fn engine_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let bundled = app.path().resource_dir().map_err(|e| e.to_string())?.join("engine");
    if bundled.join("hotspot_guard.py").exists() {
        return Ok(bundled);
    }
    Ok(Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

/// Finds a Python 3.8+ interpreter and returns its absolute path. The answer is cached after the first success.
fn find_python() -> Result<String, String> {
    if let Some(path) = PYTHON.get() {
        return Ok(path.clone());
    }
    let candidates: Vec<Vec<&str>> = if cfg!(target_os = "windows") {
        vec![vec!["py", "-3"], vec!["python"]]
    } else if cfg!(target_os = "macos") {
        vec![vec!["/usr/bin/python3"], vec!["python3"]]
    } else {
        vec![vec!["python3"]]
    };
    for candidate in candidates {
        let mut command = Command::new(candidate[0]);
        command
            .args(&candidate[1..])
            .args(["-c", "import sys; print(sys.executable if sys.version_info >= (3, 8) else '')"]);
        hide_console(&mut command);
        if let Ok(output) = command.output() {
            let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if output.status.success() && !path.is_empty() {
                let _ = PYTHON.set(path.clone());
                return Ok(path);
            }
        }
    }
    Err("Python 3.8 or newer was not found. Install it from python.org (Windows) or your package manager, \
         then reopen Hotspot Guard."
        .into())
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

fn path_str(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// Runs the engine as the current user. Used for reads and for editing the allowlist file.
fn run_engine(engine: &Engine, args: &[&str]) -> Result<String, String> {
    let argv = engine.argv(args);
    let mut command = Command::new(&argv[0]);
    command.args(&argv[1..]);
    hide_console(&mut command);
    let output = command.output().map_err(|e| e.to_string())?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        Err(failure_text(&output))
    }
}

fn failure_text(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let text = if stderr.trim().is_empty() { stdout } else { stderr };
    let text = text.trim();
    if text.is_empty() {
        "The engine failed without a message.".into()
    } else {
        text.to_string()
    }
}

fn parse_json(text: &str) -> Result<Value, String> {
    serde_json::from_str(text).map_err(|e| format!("unexpected engine output: {e}"))
}

#[cfg(target_os = "macos")]
fn shell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

#[cfg(target_os = "macos")]
fn shell_join(argv: &[String]) -> String {
    argv.iter().map(|arg| shell_quote(arg)).collect::<Vec<_>>().join(" ")
}

/// Runs an engine command as root and returns its output once it finishes.
fn run_elevated(engine: &Engine, args: &[&str]) -> Result<String, String> {
    elevated_once(engine.argv(args))
}

/// Starts `watch` as a detached root process that logs to `log`. Returns as soon as it has started.
fn start_watcher(engine: &Engine, log: &str) -> Result<(), String> {
    elevated_background(engine.argv(&["watch"]), log)
}

// --- macOS: the standard administrator password prompt, through osascript --------------------

#[cfg(target_os = "macos")]
fn osascript_admin(shell: &str) -> Result<String, String> {
    let escaped = shell.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!("do shell script \"{escaped}\" with administrator privileges");
    let output = Command::new("/usr/bin/osascript")
        .arg("-e")
        .arg(script)
        .output()
        .map_err(|e| e.to_string())?;
    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).into_owned());
    }
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    if stderr.contains("-128") {
        return Err(DENIED.into()); // the user cancelled the prompt
    }
    Err(stderr.trim().to_string())
}

#[cfg(target_os = "macos")]
fn elevated_once(argv: Vec<String>) -> Result<String, String> {
    osascript_admin(&format!("{} 2>&1", shell_join(&argv)))
}

#[cfg(target_os = "macos")]
fn elevated_background(argv: Vec<String>, log: &str) -> Result<(), String> {
    let dir = Path::new(log).parent().map(path_str).unwrap_or_default();
    let command = format!(
        "mkdir -p {dir} && nohup env PYTHONUNBUFFERED=1 {argv} >> {log} 2>&1 </dev/null & echo $!",
        dir = shell_quote(&dir),
        argv = shell_join(&argv),
        log = shell_quote(log),
    );
    osascript_admin(&command).map(|_| ())
}

// --- Linux: polkit through pkexec ----------------------------------------------------------------

#[cfg(target_os = "linux")]
fn elevated_once(argv: Vec<String>) -> Result<String, String> {
    let output = Command::new("pkexec")
        .args(&argv)
        .output()
        .map_err(|e| format!("pkexec (polkit) is needed for administrator rights: {e}"))?;
    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).into_owned());
    }
    if output.status.code() == Some(126) {
        return Err(DENIED.into()); // pkexec: authorisation was dismissed or refused
    }
    Err(failure_text(&output))
}

#[cfg(target_os = "linux")]
fn elevated_background(argv: Vec<String>, log: &str) -> Result<(), String> {
    // $1 is the log file and the rest is the engine command. Arguments are passed as separate
    // words, so no path is ever interpolated into the shell text.
    let script = r#"mkdir -p "$(dirname "$1")"; log="$1"; shift; nohup env PYTHONUNBUFFERED=1 "$@" >>"$log" 2>&1 </dev/null & echo $!"#;
    let output = Command::new("pkexec")
        .arg("sh")
        .arg("-c")
        .arg(script)
        .arg("sh")
        .arg(log)
        .args(&argv)
        .output()
        .map_err(|e| format!("pkexec (polkit) is needed for administrator rights: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    if output.status.code() == Some(126) {
        return Err(DENIED.into());
    }
    Err(failure_text(&output))
}

// --- Windows: UAC through PowerShell -------------------------------------------------------------

#[cfg(windows)]
fn cmd_quote(text: &str) -> String {
    format!("\"{text}\"")
}

#[cfg(windows)]
fn cmd_join(argv: &[String]) -> String {
    argv.iter().map(|arg| cmd_quote(arg)).collect::<Vec<_>>().join(" ")
}

#[cfg(windows)]
fn ps_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "''"))
}

#[cfg(windows)]
fn scratch_dir() -> Result<PathBuf, String> {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("hotspot-guard-{nanos}"));
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

/// Runs a PowerShell command, mapping a cancelled UAC prompt to DENIED.
#[cfg(windows)]
fn run_powershell(script: &str) -> Result<(), String> {
    let mut command = Command::new("powershell.exe");
    command.args(["-NoProfile", "-NonInteractive", "-Command", script]);
    hide_console(&mut command);
    let output = command.output().map_err(|e| e.to_string())?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    if stderr.contains("canceled") || stderr.contains("cancelled") {
        return Err(DENIED.into());
    }
    Err(failure_text(&output))
}

#[cfg(windows)]
fn elevated_once(argv: Vec<String>) -> Result<String, String> {
    let dir = scratch_dir()?;
    let script = dir.join("run.cmd");
    let out = dir.join("out.txt");
    let code = dir.join("code.txt");
    let body = format!(
        "@echo off\r\n{} > {} 2>&1\r\necho %errorlevel%> {}\r\n",
        cmd_join(&argv),
        cmd_quote(&path_str(&out)),
        cmd_quote(&path_str(&code)),
    );
    fs::write(&script, body).map_err(|e| e.to_string())?;
    run_powershell(&format!(
        "Start-Process -FilePath {} -Verb RunAs -Wait -WindowStyle Hidden",
        ps_quote(&path_str(&script))
    ))?;
    let exit_code = fs::read_to_string(&code).unwrap_or_default();
    let text = fs::read_to_string(&out).unwrap_or_default();
    let _ = fs::remove_dir_all(&dir);
    if exit_code.trim() == "0" {
        Ok(text)
    } else {
        Err(if text.trim().is_empty() { "The engine failed without a message.".into() } else { text.trim().to_string() })
    }
}

#[cfg(windows)]
fn elevated_background(argv: Vec<String>, log: &str) -> Result<(), String> {
    let dir = scratch_dir()?;
    let script = dir.join("start.cmd");
    let log_dir = Path::new(log).parent().map(path_str).unwrap_or_default();
    let body = format!(
        "@echo off\r\nif not exist {d} mkdir {d}\r\nset PYTHONUNBUFFERED=1\r\n{} >> {} 2>&1\r\n",
        cmd_join(&argv),
        cmd_quote(log),
        d = cmd_quote(&log_dir),
    );
    fs::write(&script, body).map_err(|e| e.to_string())?;
    run_powershell(&format!(
        "Start-Process -FilePath {} -Verb RunAs -WindowStyle Hidden",
        ps_quote(&path_str(&script))
    ))
}

// --- Commands called from the web UI ------------------------------------------------------------

/// "macos", "windows" or "linux", so the UI can match the native chrome of each platform.
#[tauri::command]
fn platform() -> &'static str {
    std::env::consts::OS
}

#[tauri::command]
async fn engine_status(app: AppHandle) -> Result<Value, String> {
    let engine = Engine::load(&app)?;
    parse_json(&run_engine(&engine, &["status", "--json"])?)
}

#[tauri::command]
async fn engine_groups(app: AppHandle) -> Result<Value, String> {
    let engine = Engine::load(&app)?;
    parse_json(&run_engine(&engine, &["groups", "--json"])?)
}

#[tauri::command]
async fn engine_plan(app: AppHandle) -> Result<Value, String> {
    let engine = Engine::load(&app)?;
    parse_json(&run_engine(&engine, &["plan", "--json"])?)
}

#[tauri::command]
async fn set_group(app: AppHandle, name: String, enabled: bool) -> Result<(), String> {
    let engine = Engine::load(&app)?;
    let state = if enabled { "on" } else { "off" };
    run_engine(&engine, &["set-group", name.as_str(), state]).map(|_| ())
}

#[tauri::command]
async fn start_blocking(app: AppHandle) -> Result<(), String> {
    let engine = Engine::load(&app)?;
    let status = parse_json(&run_engine(&engine, &["status", "--json"])?)?;
    let log = status["log_file"]
        .as_str()
        .ok_or("the engine did not report a log file")?
        .to_string();
    start_watcher(&engine, &log)
}

#[tauri::command]
async fn stop_blocking(app: AppHandle) -> Result<String, String> {
    let engine = Engine::load(&app)?;
    run_elevated(&engine, &["disable"])
}

#[tauri::command]
async fn apply_changes(app: AppHandle) -> Result<String, String> {
    let engine = Engine::load(&app)?;
    run_elevated(&engine, &["refresh"])
}

/// The last `lines` lines of the watcher log, or nothing if there is no log yet.
#[tauri::command]
async fn read_log(app: AppHandle, lines: usize) -> Result<Vec<String>, String> {
    let engine = Engine::load(&app)?;
    let status = parse_json(&run_engine(&engine, &["status", "--json"])?)?;
    let Some(log) = status["log_file"].as_str() else {
        return Ok(Vec::new());
    };
    let text = fs::read_to_string(log).unwrap_or_default();
    let all: Vec<String> = text.lines().map(String::from).collect();
    Ok(all[all.len().saturating_sub(lines)..].to_vec())
}

/// Opens the allowlist in the user's text editor.
#[tauri::command]
async fn open_allowlist(app: AppHandle) -> Result<(), String> {
    let engine = Engine::load(&app)?;
    let result = if cfg!(target_os = "macos") {
        Command::new("open").arg("-t").arg(&engine.config).status()
    } else if cfg!(target_os = "windows") {
        Command::new("cmd").args(["/C", "start", ""]).arg(&engine.config).status()
    } else {
        Command::new("xdg-open").arg(&engine.config).status()
    };
    result.map(|_| ()).map_err(|e| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            platform,
            engine_status,
            engine_groups,
            engine_plan,
            set_group,
            start_blocking,
            stop_blocking,
            apply_changes,
            read_log,
            open_allowlist,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Hotspot Guard");
}
