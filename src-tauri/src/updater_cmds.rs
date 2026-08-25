//! 自动更新命令：admin 页横幅经 window.__TAURI__.core.invoke 调用。

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

/// 下载并安装（不自动重启）。顺序：下载完成 -> 优雅停 daemon -> 安装。
/// 下载失败时 daemon 仍在跑，admin 页面不中断。
#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or("已是最新版本")?;
    // tauri-plugin-updater 2.x: download() 返回 Result<Vec<u8>>
    let bytes: Vec<u8> = update
        .download(|_chunk: usize, _total: Option<u64>| {}, || {})
        .await
        .map_err(|e| format!("下载失败: {}", e))?;
    // daemon 先优雅停（admin 页面即将随重启断开，无所谓）
    if let Some(dh) = app.state::<crate::AppState>().daemon.lock().unwrap().take() {
        dh.graceful_stop();
    }
    update.install(bytes)
        .map_err(|e| format!("安装失败: {}", e))?;
    Ok(())
}

/// 重启壳（安装完成后前端确认调用）。
#[tauri::command]
pub fn restart_app(app: AppHandle) {
    if let Some(dh) = app.state::<crate::AppState>().daemon.lock().unwrap().take() {
        dh.graceful_stop();
    }
    app.restart();
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
