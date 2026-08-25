mod daemon;
mod tray;
mod updater_cmds;

use std::sync::Mutex;
use tauri::Manager;

struct AppState {
    daemon: Mutex<Option<daemon::DaemonHandle>>,
    /// 两阶段更新的传递槽：download_update 存、install_update 取。
    /// Update 实现 tauri::Resource（Send + Sync + 'static），可跨命令保存。
    pending_update: Mutex<Option<(tauri_plugin_updater::Update, Vec<u8>)>>,
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 第二实例启动：前置已有实例的主窗口
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_dialog::init())
        // 注意：tauri-plugin-updater 2.10.1 的插件级 Builder 没有 on_before_exit
        // 钩子（那是 UpdaterBuilder 的 API）。exit(0) 前停 daemon 的钩子挂在
        // updater_cmds::download_update 构造的 Updater 上，Update 对象会携带
        // 它进入 install()（见 updater_cmds::stop_daemon_before_exit）。
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(AppState {
            daemon: Mutex::new(None),
            pending_update: Mutex::new(None),
        })
        .setup(|app| {
            // 托盘 + 关窗隐藏不依赖 daemon，dev/打包两种形态都注册
            // （托盘 API 内部异步，放 async runtime）
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = tray::setup_tray(&app_handle) {
                    daemon::log_to_desktop(&format!("托盘初始化失败: {}", e));
                }
            });
            // dev 模式（tauri dev，devUrl 指向开发 uvicorn）不起 sidecar
            if cfg!(dev) {
                return Ok(());
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                match daemon::DaemonHandle::spawn(&handle.clone()) {
                    Ok(dh) => {
                        let port = dh.port();
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.eval(&format!(
                                "window.location.replace('http://127.0.0.1:{}/admin')",
                                port));
                        }
                        let state = handle.state::<AppState>();
                        let mut guard = match state.daemon.lock() {
                            Ok(g) => g,
                            Err(_) => {
                                // 锁中毒：丢弃 dh（Drop 会停掉刚拉起的 daemon），
                                // 记日志后放弃注册，交给用户重试
                                daemon::log_to_desktop(
                                    "AppState 锁中毒，初次 daemon handle 注册失败");
                                return;
                            }
                        };
                        *guard = Some(dh);
                    }
                    Err(e) => {
                        daemon::log_to_desktop(&format!("daemon 启动失败: {}", e));
                        if let Some(window) = handle.get_webview_window("main") {
                            let detail = format!(
                                "{}\n\n日志目录：{}\\logs",
                                e,
                                daemon::data_dir().display());
                            let _ = window.eval(&format!(
                                "window.__shellError('llm-apig 启动失败', {})",
                                serde_json::to_string(&detail).unwrap()));
                        }
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            updater_cmds::check_update,
            updater_cmds::download_update,
            updater_cmds::install_update,
        ])
        .run(tauri::generate_context!())
        .expect("error while running llm-apig");
}
