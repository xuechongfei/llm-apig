//! daemon 生命周期管理：spawn / Job Object 防孤儿 / 健康检查 / 日志转发 / 优雅停机。

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;

use tauri::Manager;

const HEALTH_TIMEOUT_S: u32 = 30;
const HEALTH_POLL_MS: u64 = 300;

#[cfg(windows)]
struct JobHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for JobHandle {}

#[cfg(windows)]
impl Drop for JobHandle {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

/// 把子进程挂进 KILL_ON_JOB_CLOSE Job：壳以任何方式终止（崩溃/强杀）→
/// OS 销毁 Job → daemon 整棵进程树被杀，杜绝孤儿 daemon 占端口。
#[cfg(windows)]
fn assign_kill_on_close(child: &Child) -> Result<JobHandle, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(format!("CreateJobObjectW: {}", std::io::Error::last_os_error()));
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            let e = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("SetInformationJobObject: {}", e));
        }
        if AssignProcessToJobObject(job, child.as_raw_handle()) == 0 {
            let e = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("AssignProcessToJobObject: {}", e));
        }
        Ok(JobHandle(job))
    }
}

/// TCP connect 探测端口是否被监听（不需要 HTTP 成功）。
fn port_in_use(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

/// 8317 起找第一个空闲端口（最多 +19）。被占大概率是上次残留的孤儿
/// daemon —— 换端口而不是杀进程，不误杀。
fn pick_free_port(requested: u16) -> u16 {
    for port in requested..requested.saturating_add(20) {
        if !port_in_use(port) {
            return port;
        }
    }
    requested
}

/// 数据目录：环境变量优先（冒烟自检用），缺省 %APPDATA%\llm-apig。
pub(crate) fn data_dir() -> PathBuf {
    if let Ok(d) = std::env::var("LLMAPIG_DATA_DIR") {
        return PathBuf::from(d);
    }
    let appdata = std::env::var("APPDATA").unwrap_or_default();
    PathBuf::from(appdata).join("llm-apig")
}

pub struct DaemonHandle {
    child: Child,
    port: u16,
    shutdown_token: String,
    /// desktop.log 共享句柄（Task 4/5 托盘/更新流程复用）
    #[allow(dead_code)]
    pub log: Option<fs::File>,
    #[cfg(windows)]
    _job: Option<JobHandle>,
}

fn ensure_log_dir(dir: &std::path::Path) -> PathBuf {
    let d = dir.join("logs");
    let _ = fs::create_dir_all(&d);
    d
}

fn open_log(log_dir: &std::path::Path, name: &str) -> Option<fs::File> {
    fs::File::create(log_dir.join(name)).ok()
}

pub(crate) fn append_log(file: &Option<fs::File>, level: &str, msg: &str) {
    if let Some(mut f) = file.as_ref() {
        let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
        let _ = writeln!(f, "[{}] [{}] {}", ts, level, msg);
    }
}

/// 便捷日志：没有 DaemonHandle（spawn 失败/托盘等）时写 desktop.log。
/// 注意 open_log 是 truncate 语义 —— daemon 已就绪后调用会截断 desktop.log，
/// 可接受（desktop.log 只是启动诊断用，daemon.log 才是运行日志）。
pub(crate) fn log_to_desktop(msg: &str) {
    let dir = ensure_log_dir(&data_dir());
    let f = open_log(&dir, "desktop.log");
    append_log(&f, "INFO", msg);
}

/// 递归搜索目录找 daemon exe（MSI/NSIS resource 落点不稳定）。
fn find_daemon_exe(dir: &std::path::Path, depth: u32) -> Option<PathBuf> {
    if depth > 4 {
        return None;
    }
    let p = dir.join("llm-apig-daemon.exe");
    if p.exists() {
        return Some(p);
    }
    if let Ok(entries) = fs::read_dir(dir) {
        for e in entries.flatten() {
            if e.path().is_dir() {
                if let Some(found) = find_daemon_exe(&e.path(), depth + 1) {
                    return Some(found);
                }
            }
        }
    }
    None
}

/// daemon 提前退出的错误信息：带上退出码语义（daemon.py 约定：
/// 0 正常、2 缺 LLMAPIG_DATA_DIR、3 端口绑定失败/uvicorn STARTUP_FAILURE）。
fn exit_status_detail(status: &std::process::ExitStatus) -> String {
    match status.code() {
        Some(2) => "daemon 意外退出: exit code 2（缺少 LLMAPIG_DATA_DIR 环境变量）"
            .to_string(),
        Some(3) => "daemon 意外退出: exit code 3（端口绑定失败，uvicorn STARTUP_FAILURE）"
            .to_string(),
        other => format!("daemon 意外退出: exit code {}", other.unwrap_or(-1)),
    }
}

/// pid 对应进程是否存活。Windows 下 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
/// 成功且退出码仍为 STILL_ACTIVE（259）视为存活；枚举失败时保守返回 true
/// （不阻塞启动，交给健康检查兜底）。
#[cfg(windows)]
fn pid_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            // OpenProcess 对已退出的 pid 可能短暂成功（句柄未释放）或因
            // 权限失败 —— 后者不能误判为已死，保守当活着，交给健康检查兜底。
            return true;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(h, &mut code);
        CloseHandle(h);
        if ok == 0 {
            return true;
        }
        // 259（STILL_ACTIVE）视为活着；其余值是真实退出码
        // （0 也算已退出，runtime.json 属残留）。
        code == STILL_ACTIVE as u32
    }
}

#[cfg(not(windows))]
fn pid_alive(_pid: u32) -> bool {
    // 非 Windows 不做 pid 校验，交给健康检查兜底
    true
}

impl DaemonHandle {
    pub fn spawn(app: &tauri::AppHandle) -> Result<Self, String> {
        let requested: u16 = std::env::var("LLMAPIG_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(8317);

        let exe_dir = std::env::current_exe()
            .unwrap_or_default()
            .parent()
            .map(PathBuf::from)
            .unwrap_or_default();
        let project_root = exe_dir
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .map(PathBuf::from)
            .unwrap_or_default();

        let data = data_dir();
        let _ = fs::create_dir_all(&data);
        let log_dir = ensure_log_dir(&data);
        let desktop_log = open_log(&log_dir, "desktop.log");
        append_log(&desktop_log, "INFO", &format!("exe_dir: {}", exe_dir.display()));

        let port = pick_free_port(requested);
        if port != requested {
            append_log(&desktop_log, "WARN", &format!(
                "端口 {} 被占用（疑似残留 daemon），改用 {}", requested, port));
        }

        // 残留 runtime.json 里的 pid 还活着 → 说明有别的 daemon 正在运行
        // （我们刚挑了空闲端口，那个 daemon 占的是别的端口）。不杀它，
        // 但其 runtime.json 会被本实例覆盖，旧实例停机 token 随之丢失 ——
        // 记日志备查（Job Object 保证壳死时它也会被连带回收，仅当它也是
        // 本壳拉起的 daemon 时；独立启动的 daemon 不受影响）。
        if let Ok(stale) = fs::read_to_string(data.join("runtime.json")) {
            let stale_pid = serde_json::from_str::<serde_json::Value>(&stale)
                .ok()
                .and_then(|v| v.get("pid").and_then(|p| p.as_u64()));
            if let Some(pid) = stale_pid {
                if pid > 0 && pid_alive(pid as u32) {
                    append_log(&desktop_log, "WARN", &format!(
                        "残留 runtime.json 指向存活进程 pid={}(端口可能被其占用)，已忽略文件继续启动", pid));
                } else {
                    append_log(&desktop_log, "INFO", "runtime.json 为残留文件（pid 已退出），忽略");
                }
            }
        }

        let daemon_exe = find_daemon_exe(&exe_dir, 0);
        let mut cmd = if let Some(ref exe) = daemon_exe {
            Command::new(exe)
        } else {
            // dev 兜底：打包 daemon 不在时回落源码运行
            let python = std::env::var("LLMAPIG_PYTHON")
                .unwrap_or_else(|_| "python".to_string());
            let mut c = Command::new(&python);
            c.args(["-m", "desktop.daemon"]).current_dir(&project_root);
            c
        };
        cmd.env("LLMAPIG_DATA_DIR", &data)
            .env("LLMAPIG_PORT", port.to_string())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        // daemon 无窗口：隐藏控制台，避免 Windows Terminal 弹黑窗
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        append_log(&desktop_log, "INFO", &format!(
            "spawning daemon (bundled={}, port={})",
            daemon_exe.is_some(), port));

        let mut child = cmd.spawn().map_err(|e| {
            append_log(&desktop_log, "ERROR", &format!("spawn failed: {}", e));
            format!("无法启动 daemon: {}", e)
        })?;
        append_log(&desktop_log, "INFO", &format!("daemon pid: {}", child.id()));

        #[cfg(windows)]
        let job = match assign_kill_on_close(&child) {
            Ok(j) => {
                append_log(&desktop_log, "INFO", "daemon assigned to kill-on-close job");
                Some(j)
            }
            Err(e) => {
                append_log(&desktop_log, "WARN",
                    &format!("job assign failed（无孤儿保护）: {}", e));
                None
            }
        };

        // daemon stdout/stderr → logs/daemon.log / daemon.err.log
        if let Some(stdout) = child.stdout.take() {
            let f = open_log(&log_dir, "daemon.log");
            thread::spawn(move || {
                for line in BufReader::new(stdout).lines().flatten() {
                    append_log(&f, "OUT", &line);
                }
            });
        }
        if let Some(stderr) = child.stderr.take() {
            let f = open_log(&log_dir, "daemon.err.log");
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().flatten() {
                    append_log(&f, "ERR", &line);
                }
            });
        }

        // 健康检查轮询
        let health_url = format!("http://127.0.0.1:{}/health", port);
        let start = std::time::Instant::now();
        loop {
            let elapsed = start.elapsed().as_secs();
            if elapsed > HEALTH_TIMEOUT_S as u64 {
                let _ = child.kill();
                append_log(&desktop_log, "ERROR",
                    &format!("timeout after {}s", HEALTH_TIMEOUT_S));
                return Err(format!("daemon 在 {}s 内未就绪", HEALTH_TIMEOUT_S));
            }
            match child.try_wait() {
                Ok(Some(status)) => {
                    let msg = exit_status_detail(&status);
                    append_log(&desktop_log, "ERROR", &msg);
                    return Err(msg);
                }
                Ok(None) => {}
                Err(e) => return Err(format!("检查 daemon 状态失败: {}", e)),
            }
            if let Ok(resp) = ureq::get(&health_url).timeout(Duration::from_secs(3)).call() {
                if resp.status() == 200 {
                    append_log(&desktop_log, "INFO",
                        &format!("daemon ready ({}s)", elapsed));
                    break;
                }
            }
            thread::sleep(Duration::from_millis(HEALTH_POLL_MS));
        }

        // 健康检查通过后从 runtime.json 读 shutdown token（文件为准）。
        // pid 校验：文件必须是本子进程写的 —— 防止残留 runtime.json 冒充
        // （健康检查命中的是别家进程 / 文件是上一次启动残留）。
        let runtime = fs::read_to_string(data.join("runtime.json"))
            .map_err(|e| format!("读 runtime.json 失败: {}", e))?;
        let v: serde_json::Value = serde_json::from_str(&runtime)
            .map_err(|e| format!("解析 runtime.json 失败: {}", e))?;
        let file_pid = v.get("pid").and_then(|p| p.as_u64()).unwrap_or(0);
        if file_pid != child.id() as u64 {
            return Err(format!(
                "runtime.json pid 不符（文件 {}，子进程 {}）——疑似残留文件或健康检查命中旧 daemon",
                file_pid, child.id()));
        }
        let file_port = v.get("port").and_then(|p| p.as_u64()).unwrap_or(0);
        if file_port != port as u64 {
            return Err(format!(
                "runtime.json port 不符（文件 {}，壳选 {}）——疑似残留文件",
                file_port, port));
        }
        let shutdown_token = v
            .get("token")
            .and_then(|t| t.as_str())
            .unwrap_or("")
            .to_string();

        let log_for_handle = desktop_log.as_ref().and_then(|f| f.try_clone().ok());
        let handle = DaemonHandle {
            child,
            port,
            shutdown_token,
            log: log_for_handle,
            #[cfg(windows)]
            _job: job,
        };
        spawn_crash_watcher(app.clone(), handle.child.id());
        Ok(handle)
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// 崩溃监控线程用：比对 state 里的 handle 是否仍是本进程。
    pub fn child_id(&self) -> u32 {
        self.child.id()
    }

    /// 崩溃监控线程用：非阻塞查询子进程状态（child 是私有字段）。
    /// 注意 std::process::Child::try_wait 需要 &mut self，故此处为 &mut。
    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }

    /// 优雅停机：POST /shutdown（3s 超时）。失败由 Drop 的 kill + Job 兜底。
    pub fn graceful_stop(&self) {
        if self.shutdown_token.is_empty() {
            return;
        }
        let url = format!("http://127.0.0.1:{}/shutdown", self.port);
        let _ = ureq::post(&url)
            .set("X-Shutdown-Token", &self.shutdown_token)
            .timeout(Duration::from_secs(3))
            .call();
    }
}

/// 崩溃监控：daemon 异常退出 → 重启一次；再挂则停（由用户手动处理）。
/// 在 DaemonHandle::spawn() 末尾、返回 handle 前调用。
///
/// 注意注册窗口期：watcher 在 spawn() 返回前启动，而调用方（lib.rs 初次
/// 接线 / 本函数的重启路径）要在 spawn() 返回后才把 handle 存进 state ——
/// 期间 state.daemon 为 None。若把 None 一律当"已退出"会误退役 watcher
/// （首个 daemon 将无人监控、崩溃不重启）。故 None 先等（上限 30s），
/// 只有匹配过（matched）之后的 None 才是真正的退出流程。
fn spawn_crash_watcher(
    handle: tauri::AppHandle,
    child_id: u32,
) {
    std::thread::spawn(move || {
        let register_deadline =
            std::time::Instant::now() + Duration::from_secs(HEALTH_TIMEOUT_S as u64);
        let mut matched = false;
        loop {
            let state = handle.state::<crate::AppState>();
            let mut guard = state.daemon.lock().unwrap();
            if let Some(dh) = guard.as_mut() {
                if dh.child_id() != child_id {
                    return; // handle 已被替换（重启过/退出流程），本 watcher 退役
                }
                matched = true; // 已注册，进入监控
                match dh.try_wait() {
                    Ok(Some(status)) => {
                        drop(guard);
                        log_to_desktop(&format!(
                            "daemon 崩溃({})，自动重启一次", exit_status_detail(&status)));
                        // take 出旧 handle（Drop 会 kill——进程已死，无害）
                        let old = state.daemon.lock().unwrap().take();
                        drop(old);
                        match DaemonHandle::spawn(&handle) {
                            Ok(dh) => {
                                let new_port = dh.port();
                                *state.daemon.lock().unwrap() = Some(dh);
                                if let Some(w) = handle.get_webview_window("main") {
                                    let _ = w.eval(&format!(
                                        "window.location.replace('http://127.0.0.1:{}/admin')",
                                        new_port));
                                }
                            }
                            Err(e) => log_to_desktop(&format!("重启失败: {}", e)),
                        }
                        return; // 只重启一次，新实例由新监控线程负责（spawn 递归装新 watcher）
                    }
                    Ok(None) => {} // 还活着
                    Err(_) => return,
                }
            } else if matched {
                return; // 正常退出流程已 take 走 handle
            } else if std::time::Instant::now() > register_deadline {
                return; // 调用方未在窗口期内注册（异常路径），退役
            }
            drop(guard);
            std::thread::sleep(std::time::Duration::from_secs(2));
        }
    });
}

impl Drop for DaemonHandle {
    fn drop(&mut self) {
        self.graceful_stop();
        let _ = self.child.kill();
        for _ in 0..50 {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                _ => std::thread::sleep(Duration::from_millis(100)),
            }
        }
    }
}
