//! 自动更新命令：admin 页横幅经 window.__TAURI__.core.invoke 调用。
//!
//! 两阶段交互（审查 Important B）：
//! 1. `download_update`：check -> 有新版则 download 到内存 -> 存 AppState ->
//!    返回 UpdateInfo（无新版 Ok(None)）；
//! 2. `install_update`：取出的更新 install()。tauri-plugin-updater 2.10.1 在
//!    Windows 上 install() 会 ShellExecuteW 启动 NSIS（默认 installMode 带
//!    /P /R，装完自动重启应用）后无条件 `std::process::exit(0)` -- 命令成功
//!    路径永不返回，故不再需要单独的 restart_app 命令（NSIS /R 负责重启）。

use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_updater::UpdaterExt;

use crate::daemon;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    pub version: String,
    pub notes: String,
}

/// 检查更新：None = 已是最新。错误一律返回 Err(String) 由前端 catch 显示。
#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;
    Ok(update.map(|u| UpdateInfo {
        version: u.version.clone(),
        notes: u.body.clone().unwrap_or_default(),
    }))
}

/// exit(0) 前的最后机会：停 daemon（幂等，install_update 已显式停过则为 no-op），
/// 再补插件默认钩子的 cleanup_before_exit（清托盘图标/隐藏窗口/清资源表）。
/// 经 UpdaterBuilder::on_before_exit 挂载 -- 它会整体替换插件默认钩子，故此处
/// 必须补调 cleanup_before_exit；Update 携带该闭包，install() 在
/// ShellExecuteW 之后、std::process::exit(0) 之前执行它。
fn stop_daemon_before_exit(app: AppHandle) -> impl Fn() + Send + Sync + 'static {
    move || {
        let state = app.state::<crate::AppState>();
        if let Ok(mut guard) = state.daemon.lock() {
            // take + drop：graceful_stop 优雅停，Drop 补 kill + 等待退出
            //（daemon exe 在安装目录内，NSIS 覆写前进程必须死透）
            drop(guard.take());
        }
        app.cleanup_before_exit();
    }
}

/// 阶段一：下载更新到内存并存入 AppState。
/// None = 已是最新（前端静默）；Err = 检查/网络/下载失败（daemon 不受影响，
/// admin 页继续可用）。
#[tauri::command]
pub async fn download_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let updater = app
        .updater_builder()
        .on_before_exit(stop_daemon_before_exit(app.clone()))
        .build()
        .map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?;
    let Some(update) = update else {
        return Ok(None);
    };
    let info = UpdateInfo {
        version: update.version.clone(),
        notes: update.body.clone().unwrap_or_default(),
    };
    // tauri-plugin-updater 2.x: download() 返回 Result<Vec<u8>>（含签名校验）
    let bytes: Vec<u8> = update
        .download(|_chunk: usize, _total: Option<u64>| {}, || {})
        .await
        .map_err(|e| format!("下载失败: {}", e))?;
    let state = app.state::<crate::AppState>();
    let mut guard = state
        .pending_update
        .lock()
        .map_err(|_| "更新状态锁中毒".to_string())?;
    *guard = Some((update, bytes));
    Ok(Some(info))
}

/// 阶段二：安装已下载的更新（同步命令）。
/// 成功路径：install() 内部 exit(0) 进程直接退出（NSIS /R 自动重启应用），
/// 本函数不返回；失败路径（未下载/签名/解包失败等）返回 Err 由前端回落手动下载。
#[tauri::command]
pub fn install_update(app: AppHandle) -> Result<(), String> {
    let state = app.state::<crate::AppState>();
    let (update, bytes) = {
        let mut guard = state
            .pending_update
            .lock()
            .map_err(|_| "更新状态锁中毒".to_string())?;
        guard
            .take()
            .ok_or("尚未下载更新，请先点击“立即更新”完成下载")?
    };
    // daemon 先停干净：NSIS 要覆写安装目录里的 daemon exe，运行中的 exe 无法
    // 覆写。graceful_stop 优雅停 + Drop 兜底 kill/等待；install() 内部的
    // on_before_exit 钩子与此幂等共存（届时 daemon 已被 take 走）。
    if let Ok(mut guard) = state.daemon.lock() {
        drop(guard.take());
    }
    update
        .install(bytes)
        .map_err(|e| format!("安装失败: {}", e))
}

/// 托盘"检查更新"共用入口（tray.rs Task 4 的 check_update 分支调这里）。
pub fn tray_check_update(app: &AppHandle) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match check_update(handle.clone()).await {
            Ok(Some(info)) => {
                let _ = handle.dialog()
                    .message(format!("新版本 {} 可用，请打开主界面点击横幅更新",
                                     info.version))
                    .kind(MessageDialogKind::Info)
                    .title("llm-apig 更新")
                    .show(|_| {});
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            Ok(None) => {
                let _ = handle.dialog().message("已是最新版本")
                    .kind(MessageDialogKind::Info)
                    .title("llm-apig")
                    .show(|_| {});
            }
            Err(e) => {
                daemon::log_to_desktop(&format!("托盘检查更新失败: {}", e));
                let _ = handle.dialog().message(format!("检查更新失败：{}", e))
                    .kind(MessageDialogKind::Error)
                    .title("llm-apig")
                    .show(|_| {});
            }
        }
    });
}
