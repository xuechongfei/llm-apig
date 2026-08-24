mod daemon;

use std::sync::Mutex;
use tauri::Manager;

struct AppState {
    daemon: Mutex<Option<daemon::DaemonHandle>>,
}

pub fn run() {
    tauri::Builder::default()
        .manage(AppState { daemon: Mutex::new(None) })
        .setup(|app| {
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
                        *state.daemon.lock().unwrap() = Some(dh);
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
        .run(tauri::generate_context!())
        .expect("error while running llm-apig");
}
