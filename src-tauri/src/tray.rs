//! 系统托盘：菜单四项（对等旧 pystray）+ 关窗隐藏 + 托盘退出。

use tauri::{
    menu::{CheckMenuItem, MenuBuilder, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};
use tauri_plugin_autostart::ManagerExt;

use crate::daemon;

pub fn setup_tray<R: Runtime>(app: &AppHandle<R>) -> Result<(), Box<dyn std::error::Error>> {
    let show: MenuItem<R> = MenuItem::with_id(app, "show", "打开主界面", true, None::<&str>)?;
    let autostart: CheckMenuItem<R> = CheckMenuItem::with_id(
        app,
        "autostart",
        "开机自启",
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let check_update: MenuItem<R> =
        MenuItem::with_id(app, "check_update", "检查更新", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit: MenuItem<R> = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;

    let menu = MenuBuilder::new(app)
        .item(&show)
        .item(&autostart)
        .item(&check_update)
        .item(&sep)
        .item(&quit)
        .build()?;

    TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("llm-apig API 网关")
        .icon(app.default_window_icon().cloned().unwrap())
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "autostart" => {
                let mgr = app.autolaunch();
                let enabled = mgr.is_enabled().unwrap_or(false);
                let result = if enabled { mgr.disable() } else { mgr.enable() };
                if let Err(e) = result {
                    daemon::log_to_desktop(&format!("autostart 切换失败: {}", e));
                }
            }
            "check_update" => {
                // Task 5 Step 3 会把这里替换为 updater_cmds::tray_check_update(app);
                // 本任务先最小实现：打开主窗口（横幅在页内显示更新状态）
                show_main(app);
            }
            "quit" => {
                let state = app.state::<crate::AppState>();
                match state.daemon.lock() {
                    Ok(mut guard) => {
                        if let Some(dh) = guard.take() {
                            dh.graceful_stop();
                            // drop(dh) 触发 Drop：kill + Job 清理
                        }
                    }
                    Err(_) => {
                        // 锁中毒：直接退出，daemon 由 Job Object 兜底回收
                        daemon::log_to_desktop("AppState 锁中毒，退出时跳过优雅停机");
                    }
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;

    // 关窗 = 隐藏（服务继续），托盘退出才是真退出
    if let Some(window) = app.get_webview_window("main") {
        let w = window.clone();
        window.on_window_event(move |event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = w.hide();
            }
        });
    }
    Ok(())
}

fn show_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}
