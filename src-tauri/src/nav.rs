//! 导航守卫：webview 只允许停留在「壳自身页面」，其余 URL 转交系统浏览器。
//!
//! 允许的导航目标：
//! - Tauri 内置 scheme / tauri.localhost（打包态启动页 desktop-ui/index.html）
//! - 回环地址（127.0.0.1 / localhost / ::1）且路径为 /admin 或 /admin/...
//!   （daemon 端口由 pick_free_port 动态挑选，故不限定端口）
//! - about:blank（webview 内部占位导航，转外部打开无意义）
//!
//! 其余（同源的 /health、GitHub 等外链）一律由 on_navigation 拦截：
//! 取消 webview 导航、用 tauri-plugin-opener 在系统默认浏览器打开。
//! webview 没有浏览器后退栏，页内跳走就回不去主页（健康检查链接 bug）。

use tauri::Url;

pub(crate) fn navigation_allowed(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "about" => return true,
        "http" | "https" => {}
        _ => return false,
    }
    let host = match url.host_str() {
        Some(h) => h,
        None => return false,
    };
    if host.eq_ignore_ascii_case("tauri.localhost") {
        return true;
    }
    let loopback = host == "127.0.0.1"
        || host == "[::1]"
        || host.eq_ignore_ascii_case("localhost");
    if !loopback {
        return false;
    }
    let path = url.path();
    path == "/admin" || path.starts_with("/admin/")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    // ── 放行：壳自身页面 ──────────────────────────────────

    #[test]
    fn allows_tauri_localhost_splash() {
        // 打包态初始页（Windows 为 http://tauri.localhost，macOS/Linux 为 tauri://localhost）
        assert!(navigation_allowed(&u("http://tauri.localhost/")));
        assert!(navigation_allowed(&u("https://tauri.localhost/index.html")));
        assert!(navigation_allowed(&u("tauri://localhost/")));
    }

    #[test]
    fn allows_loopback_admin_any_port() {
        // 初始 devUrl，及 daemon 重启后 location.replace 到的新端口
        assert!(navigation_allowed(&u("http://127.0.0.1:58317/admin")));
        assert!(navigation_allowed(&u("http://127.0.0.1:58318/admin/channels")));
        assert!(navigation_allowed(
            &u("http://localhost:58317/admin/settings?x=1#frag")
        ));
        assert!(navigation_allowed(&u("http://[::1]:58317/admin/logs")));
    }

    #[test]
    fn allows_about_blank() {
        assert!(navigation_allowed(&u("about:blank")));
    }

    // ── 拦截：转系统浏览器 ────────────────────────────────

    #[test]
    fn denies_loopback_non_admin_path() {
        // 本 bug：健康检查是同源但非 /admin 页面（裸 JSON，无返回入口）
        assert!(!navigation_allowed(&u("http://127.0.0.1:58317/health")));
        assert!(!navigation_allowed(&u("http://127.0.0.1:58317/")));
        assert!(!navigation_allowed(&u("http://localhost:58317/openapi.json")));
    }

    #[test]
    fn denies_admin_lookalike_prefix() {
        // 前缀匹配陷阱：/administrator 不能算 /admin
        assert!(!navigation_allowed(&u("http://127.0.0.1:58317/administrator")));
        assert!(!navigation_allowed(&u("http://127.0.0.1:58317/adminx")));
    }

    #[test]
    fn denies_external_origins() {
        // 更新失败回落的 GitHub releases 页
        assert!(!navigation_allowed(
            &u("https://github.com/xuechongfei/llm-apig/releases")
        ));
        assert!(!navigation_allowed(&u("https://example.com/admin")));
    }
}
