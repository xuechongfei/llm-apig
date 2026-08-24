# llm-apig — 个人 LLM API 网关

本机部署的大模型 API 网关：多渠道优先级、余额耗尽/限流/能力不匹配时自动切换，
兼容 Anthropic（优先）与 OpenAI 协议，支持双向协议转换。

## 快速开始

1. 安装 [uv](https://docs.astral.sh/uv/)
2. 双击 `start.bat`（或 `uv run python -m app.main`）
3. 浏览器自动打开管理界面 http://127.0.0.1:8317/admin

## 使用

1. **渠道**：添加云产商接入点（协议/base_url/api_key），可用「测试」按钮验证连通性
2. **模型组**：建统一模型名（如 `my-claude`），把渠道加进组里，设真实模型名、
   优先级（越小越优先）、图片/视频能力标记
3. **客户端指向网关**：
   - Anthropic 协议：`ANTHROPIC_BASE_URL=http://127.0.0.1:8317`，模型填统一模型名
   - OpenAI 协议：`base_url=http://127.0.0.1:8317/v1`
   - 若在「设置」里配置了访问令牌，客户端 api key 填该令牌；否则任意

## 切换规则

能力过滤（图片/视频）→ 同协议优先 → 优先级 → 冷却排最后。
余额不足(402/特征匹配)冷却 10 分钟、限流 60s、鉴权失败 30 分钟、5xx 60s，
均可在设置页调整。流式响应一旦开始则不再重试（避免重复内容）。

## 开发

`uv run pytest tests/ -v`

## 桌面版构建与分发（Windows）

### 构建

1. 安装依赖与 Inno Setup（一次性）：
   `winget install -e --id JRSoftware.InnoSetup`
2. 一键构建：`uv run python desktop/build.py`
   （自动执行：测试 → PyInstaller → 安装包 → 冒烟自检，产物在 `dist/`）
3. 发布前编辑 `version.json` 填写 `notes` 与 `url`（发布页地址），
   把 setup.exe 上传到 GitLab Release，并提交 version.json

### 最终用户安装说明（随安装包附带）

1. 双击 `llm-apig-setup-x.x.x.exe` 安装
2. 首次运行若出现蓝色「Windows 已保护你的电脑」提示：
   点「更多信息」→「仍要运行」（无签名软件的正常提示）
3. 关闭窗口后程序驻留系统托盘，网关继续服务；托盘右键可退出
4. 客户端配置同上（地址 `http://127.0.0.1:8317`，若端口被占程序自动换用
   8318-8321，实际端口见管理界面左下角）

### 更新检查

设置页「版本信息地址」填 version.json 直链（如 GitLab raw 地址）后，
程序每小时后台检查一次，有新版在界面顶部横幅提示。
