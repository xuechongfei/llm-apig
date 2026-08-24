// 本任务最小版（窗口 + splash），Task 3/4/5 逐步充实
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running llm-apig");
}
