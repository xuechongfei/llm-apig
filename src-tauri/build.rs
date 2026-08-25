fn main() {
    // 应用自定义命令的 ACL 权限声明（审查 Critical A）：
    // tauri-build 2.6.3 不会从 generate_handler! 自动生成应用命令权限
    // （实测仅往 capabilities 加 allow-* 条目会报 Permission not found），
    // 必须经 AppManifest 显式声明，才会生成 allow-/deny-<command> 权限，
    // capabilities/default.json 才能引用。
    // 生产形态主窗口被导航到 http://127.0.0.1:<port>/admin（remote origin），
    // tauri 2.11.5 对 remote origin 的自定义命令同样强制过 ACL，缺权限即拒。
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "check_update",
                "download_update",
                "install_update",
            ]),
        ),
    )
    .expect("tauri-build failed");
}
