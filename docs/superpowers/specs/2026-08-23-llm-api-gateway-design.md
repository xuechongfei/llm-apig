# LLM API 网关（llm-apig）设计文档

日期：2026-08-23
状态：已确认（设计评审通过）

## 1. 背景与目标

个人用户本机部署的 LLM API 网关（PC 桌面应用形态：本地服务 + 浏览器管理界面）。
核心痛点：手上有多个云产商的大模型服务，某个服务余额耗尽或故障时，需要网关**自动切换到其它可用渠道**。

核心需求：

1. 有 Web 配置界面
2. 渠道支持优先级
3. 能力感知切换：渠道不支持图片/视频输入时，智能切换到支持的渠道
4. 兼容 Anthropic 协议（**优先支持**）与 OpenAI 协议，支持跨协议双向转换
5. uv 管理依赖，脚本启动

## 2. 技术选型

- **方案**：自研轻量网关（方案 A）。协议转换手写，实现时参考 LiteLLM 的转换逻辑，但不引入 LiteLLM 作为依赖（其以 OpenAI 协议为中心，Anthropic 流式特性支持不全，与本项目"Anthropic 优先"诉求相反）。
- **栈**：Python + FastAPI + uvicorn + httpx（流式转发）+ SQLite（WAL）
- **管理界面**：服务端渲染 + HTMX/Alpine（CDN 引入），无前端构建链
- **启动**：`start.bat` 双击 → uv run 启动服务 + 自动打开浏览器；只绑 `127.0.0.1`

## 3. 总体架构

```
客户端(Anthropic 协议) ──▶ /v1/messages ──┐
                                          ├─▶ 路由层 ─▶ 渠道选择器 ─▶ [协议转换层] ─▶ 转发执行器 ─▶ 上游渠道
客户端(OpenAI 协议)    ──▶ /v1/chat/…  ──┘   (能力/协议/优先级/冷却)    (A⇄O)      (httpx+重试)
浏览器 ──▶ /admin (管理 API + 页面)
存储：SQLite（配置 + 请求日志）
```

单进程 FastAPI，三类端点：

- `/v1/messages`、`/v1/messages/count_tokens`（Anthropic 协议）
- `/v1/chat/completions`、`/v1/models`（OpenAI 协议）
- `/admin/*` 管理 API + 静态页面

**请求数据流**：入口协议解析 → 统一模型名查渠道组 → 选择器产出候选列表 → 逐个尝试（协议不一致则过转换层）→ httpx 流式转发 → 失败记录原因并重试下一候选 → 全部失败返回最后错误。每次尝试写日志子记录。

**关键取舍**：重试只在「尚未向客户端发出任何字节」时进行。流式响应中途失败不重试（避免客户端收到重复内容），记日志并按入口协议规范发 error 事件。

## 4. 数据模型（SQLite）

### channel（渠道）
| 字段 | 说明 |
|---|---|
| id / name | 如「官方 Claude」「DeepSeek」 |
| protocol | `anthropic` / `openai` |
| base_url / api_key | api_key 界面脱敏显示 |
| enabled | 启用开关 |

### model_group（统一模型组）
| 字段 | 说明 |
|---|---|
| id / name | 统一模型名，如 `claude-sonnet`，客户端请求填它 |

### model_mapping（组内映射，多对多）
| 字段 | 说明 |
|---|---|
| group_id / channel_id | 关联 |
| actual_model | 该渠道真实模型名 |
| priority | 数字，越小越优先 |
| supports_image / supports_video | 能力标记（布尔） |

### request_log（请求日志）+ attempt（尝试子表）
- 主记录：id、created_at、client_protocol、group_name、path、stream、status、total_duration_ms、input_tokens、output_tokens
- 子记录（每次尝试一条）：渠道、actual_model、是否协议转换、HTTP 状态、错误摘要、是否因能力跳过、原始错误体（供 v2 Agent 使用）

首次启动自动建表。配置全部走 Web 界面，无手写配置文件。

## 5. 渠道选择与故障切换

### 候选排序
1. **能力过滤（硬性）**：请求含图片/视频内容时，过滤掉能力标记不满足的映射（日志记"因能力跳过"）
2. **冷却过滤**：冷却中的渠道排到最后（不排除，避免全灭）
3. **排序**：同协议优先 → priority 升序

### 错误分类与动作
| 类别 | 典型表现 | 动作 |
|---|---|---|
| 余额不足 | 402/403，文本含 `insufficient balance/quota`、`余额不足` 等 | 重试 + 冷却 10 分钟（可配） |
| 能力不支持 | 400，文本含 `does not support image/video` 等 | 重试 + 日志提示检查能力标记 |
| 限流 | 429 | 重试 + 冷却 1 分钟 |
| 鉴权失败 | 401/403 | 重试 + 冷却 30 分钟，界面标红 |
| 服务器错误 | 5xx、超时、网络错误 | 重试 + 短冷却 |
| 客户端错误 | 400 参数错误（非能力类）、404 | 不重试，原样返回 |
| 未识别 | 其它 | 打标 `unclassified`，保留原始错误体（v2 Agent 数据源） |

错误文本特征库集中定义、可在界面维护（v1 提供查看/简单编辑）。

### 边界规则
- 单请求最多尝试 `min(组内渠道数, 5)` 次
- 全部失败 → 返回最后一次错误，日志保留完整尝试链

## 6. 协议转换层（A⇄O）

纯函数模块（无网络、无状态，可单测）。

### 非流式映射
| Anthropic | OpenAI |
|---|---|
| 顶层 `system` | `messages[role=system]` |
| content blocks: `text`/`image` | content parts: `text`/`image_url` |
| `tools` (input_schema) | `tools` (parameters) |
| `tool_result` | `role=tool` 消息 |
| `tool_use` | `tool_calls` |
| `max_tokens`/`temperature`/`top_p`/`stop_sequences` | 直传/改名 |

反向映射同理（OpenAI → Anthropic）。

### 流式转换（增量状态机）
- Anthropic 流：`message_start` → `content_block_start/delta/stop` → `message_delta` → `message_stop`
- OpenAI 流：`chat.completion.chunk`（delta.content / tool_calls / finish_reason）
- 状态机跟踪：当前 block 索引、tool_call JSON 累积、usage 尾部事件
- `ping`、`thinking`、`cache_control`：同协议透传保留；跨协议时 `cache_control` 丢弃、`thinking` 尽力映射（不支持则丢弃并记日志）

### 范围控制（v1 不转换）
`computer_use`、文件 block、citations、batch API。未知 block：同协议透传，跨协议报"该内容类型不支持跨协议转换"。

### count_tokens
Anthropic 渠道原样转发；OpenAI 渠道返回近似估算 + `x-estimated: true` 头。

## 7. 管理界面

| 页面 | 功能 |
|---|---|
| 仪表盘 | 渠道健康状态（正常/冷却中/失败原因）、最近请求 |
| 渠道管理 | CRUD、启用开关、连通性测试按钮 |
| 模型组管理 | 组 CRUD、映射编辑（拖拽优先级、能力勾选） |
| 请求日志 | 列表 + 详情（尝试链、错误原文、耗时、token）、按状态/渠道/模型过滤 |
| 设置 | 冷却时长、错误特征库、监听端口、访问令牌（可选本地 API key） |

## 8. 项目结构与测试

```
llm-apig/
├── pyproject.toml          # uv 管理
├── start.bat               # 双击启动
├── app/
│   ├── main.py             # FastAPI 装配、启动入口
│   ├── db.py               # SQLite、建表迁移
│   ├── router_anthropic.py # /v1/messages 等
│   ├── router_openai.py    # /v1/chat/completions 等
│   ├── selector.py         # 渠道选择器
│   ├── executor.py         # httpx 转发、重试循环、流式处理
│   ├── errors.py           # 错误分类器 + 特征库
│   ├── convert/            # a2o.py / o2a.py / stream_a2o.py / stream_o2a.py
│   ├── admin/              # 管理 API + 模板 + 静态资源
│   └── logging_.py         # 请求日志写入
└── tests/                  # pytest
```

测试策略：
- 转换层：黄金样本测试（真实请求/响应 fixture，含流式 chunk 序列）
- 选择器：能力过滤、协议优先、冷却、优先级排序单测
- 执行器：httpx MockTransport 模拟 402/429/5xx/流中断，验证重试链与日志
- 端到端：「Anthropic 请求 → OpenAI 渠道」全链路

## 9. v2 扩展（本次不实现，预留接口）

**内置智能 Agent**：遇到 `unclassified` 错误时，由 Agent 辅助处理——自动修正渠道能力标记、维护错误文本特征库。v1 已为此预留：`unclassified` 打标 + 原始错误体完整保留。
