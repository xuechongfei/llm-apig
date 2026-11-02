@echo off
rem 桌面壳开发：先起 Python 网关（uvicorn），再起 Tauri 壳（devUrl 指向它）
rem 壳侧 devUrl=http://127.0.0.1:8317/admin，dev 模式不起 sidecar daemon。
start "llm-apig dev server" cmd /c "uv run python -m app.main"
cd /d "%~dp0src-tauri" && cargo tauri dev