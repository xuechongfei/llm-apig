# 日志耗时、出参与缓存 token 展示设计

日期：2026-11-02
状态：已批准

## 背景

请求日志有四处影响排障的短板：

1. **耗时单位**：`request_log.total_duration_ms` 存毫秒，列表/仪表盘/详情直接渲染成 `1234ms`，长请求不直观。
2. **没有出参**：`request_log` 只有 `request_body`（v2 迁移加入），响应完全没落库。流式分支边转发边丢弃（`app/executor.py:295`），事后无法确认上游到底回了什么。
3. **报文不格式化**：`_sanitize_body` 存的是压缩 JSON，页面 `<pre>` 原样输出，长报文难以阅读。
4. **tokens 常缺失**：只有非流式从 `body.usage` 取值（`app/executor.py:220`）；流式 `finish_log` 写死 `None`（`app/executor.py:340`）。Anthropic 的 `cache_read_input_tokens` / `cache_creation_input_tokens` 既没落库也没展示，缓存命中情况不可见。

用户要求先解决 Anthropic 协议。范围判定为**Anthropic 入口协议**（`/v1/messages`）：包含上游 Anthropic 直通和上游 OpenAI 协议转换两种链路；OpenAI 入口（`/v1/chat/completions`）只补非流式出参与现有 token，流式留待下一轮。

## 需求

- 界面耗时以秒为单位、保留两位小数。
- 日志详情能看到响应报文；流式请求展示重建后的最终 JSON。
- 请求/响应报文在界面格式化 JSON。
- 记录并展示 Anthropic 缓存命中（读取）与缓存写入 token，流式非流式都要有。

## 方案

对比过的备选：

- 流式出参存原始 SSE：能看到逐事件细节，但体积大、无格式化意义，且与「重建 JSON」诉求重复。弃用。
- 同时存重建 JSON + 原始 SSE：信息最全，但需要额外字段与折叠 UI，收益有限。弃用。
- **采用：流式旁路解析重建最终 JSON**。存储小、可读，且顺手解决流式 usage 采集。

原始 SSE 仍原样转发给客户端，不改动客户端可见事件。

## 设计

### 一、数据模型（迁移 v2 → v3）

`MIGRATIONS` 追加一条（`app/db.py`）：

```sql
ALTER TABLE request_log ADD COLUMN response_body TEXT;
ALTER TABLE request_log ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE request_log ADD COLUMN cache_creation_tokens INTEGER;
```

旧数据保留，新列默认 NULL。耗时继续以毫秒整数落库（`total_duration_ms` 不改名、不换算），只在展示层转秒，避免迁移与精度问题。

`app/logging_.py`：

- `finish_log()` 增加可选参数 `response_body=None`、`cache_read_tokens=None`、`cache_creation_tokens=None`，更新语句一并写入。现有调用无需改动。

### 二、用量采集（新模块 `app/usage.py`）

统一归一化为：

```python
{"input_tokens": int | None, "output_tokens": int | None,
 "cache_read_tokens": int | None, "cache_creation_tokens": int | None}
```

提供两个入口：

- `from_anthropic_usage(usage: dict | None) -> dict`：读 `input_tokens`、`output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`。
- `from_openai_usage(usage: dict | None) -> dict`：读 `prompt_tokens`、`completion_tokens`；缓存读取取 `prompt_tokens_details.cached_tokens`，回退 `prompt_cache_hit_tokens`；缓存写入无对应字段，置 None。

**语义说明**：Anthropic 的 `input_tokens` 不含缓存部分，三个计数并列；OpenAI 的 `prompt_tokens` 含缓存部分。两者都按上游原样记录，不做加减换算，避免误导。

### 三、执行器改动（app/executor.py）

所有采集均基于**上游原始响应**，在协议转换之前提取，保证拿到真实的 usage。

#### 非流式

- `_try_non_stream` 在 `resp.json()` 之后、`cand.needs_conversion` 转换之前，从原始 body 提取 usage。
- `response_body` 存**返回给客户端**的脱敏 JSON（转换后的 body），与 `request_body` 一样经 `_sanitize_body` 处理（图片 base64 替换、100KB 截断）。只记录成功响应；错误响应已由 attempt 的 `raw_error` 承载。
- `finish_log` 写入四项计数与 `response_body`。

#### 流式直通（上游 Anthropic → 客户端 Anthropic）

- 转发循环不变，逐行 `yield` 原始块。
- 旁路解析：`event: message_start` → 从 `message.usage` 取输入与缓存；`event: message_delta` → 取 `usage.output_tokens`；`content_block_start/delta/stop` → 累加到重建器。
- 生成器 `finally` 里用新连接写日志（现状已如此）：写入 usage、`response_body=重建 JSON`、状态。
- 解析失败（非 SSE、JSON 坏块）静默跳过，不能影响转发。

#### 流式转换（客户端 Anthropic → 上游 OpenAI）

- 复用现有 `A2OStreamConverter` 链路。转换器产出的已是 Anthropic 事件，直接喂给重建器即可。
- 给转换器补充 `usage() -> dict`：累计 `prompt_tokens` / `completion_tokens`（来自上游 `include_usage` 尾包，现有代码已读取），并按 `from_openai_usage` 映射缓存读取。
- 注意：上游 OpenAI 的 usage 在流末尾才到达，转换器发出的 `message_start` 里输入 token 是 0。因此重建出的 JSON 在流结束后用转换器的最终 usage **覆盖** `usage` 字段，保证日志显示真实值。

#### 流式出参重建器（新模块 `app/reconstruct.py`）

`AnthropicStreamReconstructor`，输入上游事件 dict，输出最终 Anthropic message JSON：

- `message_start`：初始化骨架（id/type/role/model/content/stop_reason/usage）。
- `content_block_start`：按 `content_block.type` 建块（`text`、`thinking`、`tool_use` 记 id/name、其它类型原样保留）。
- `content_block_delta`：`text_delta` 追加文本；`thinking_delta` 追加思考；`signature_delta` 追加签名；`input_json_delta` 追加 partial_json。
- `content_block_stop`：`tool_use` 块的 partial_json 解析为 `input`（解析失败保留空 dict，原文另存于内部，不阻塞）。
- `message_delta`：写 `stop_reason` / `stop_sequence` / 输出 token。
- 进程被客户端断开（`GeneratorExit`）时 `finally` 仍写日志，保留已重建的部分内容；状态沿用现有判定（无上游异常即 success），不新增 message_stop 判据。

#### 与 OpenAI 入口的边界

- OpenAI 入口非流式：同样提取 usage（走 `from_openai_usage` 映射缓存读取）并落 `response_body`。
- OpenAI 入口流式：本轮**不**重建出参、不采集 usage，保持现状（`None`）。

### 四、界面

`app/admin/routes.py` 新增 Jinja 过滤器：

- `dur`：毫秒 → `f"{ms/1000:.2f} s"`；None → `—`。
- `pretty`：`json.loads` 后 `indent=2, ensure_ascii=False` 输出；解析失败回退原文。

模板改动：

- `logs.html`
  - 耗时列：`{{ l["total_duration_ms"] | dur }}`。
  - tokens 列拆为「输入 / 输出 / 缓存读 / 缓存写」四个独立列，缺失值 `—`。
- `dashboard.html`：最近请求耗时改秒。
- `log_detail.html`
  - `meta-grid` 增加：输入 tokens、输出 tokens、缓存读取、缓存写入（连同现有协议/模型/路径/耗时，共 8 项，4 列网格正好两行）。
  - 「请求报文」改用 `| pretty`。
  - 新增「响应报文」区块，`| pretty`；流式为重建后的 JSON。
  - 尝试链的 `raw_error` 也用 `| pretty`（非 JSON 自动回退）。

### 五、测试

- `tests/test_db.py`：v3 迁移后新列存在，旧数据保留。
- `tests/test_usage.py`（新）：Anthropic/OpenAI usage 两种映射，缓存读取的两种字段名回退，缺失字段返回 None。
- `tests/test_reconstruct.py`（新）：文本、thinking、tool_use（含 partial_json 拼接）、stop_reason、usage 的完整重建；坏 JSON delta 不抛异常。
- `tests/test_executor.py`
  - 非流式 Anthropic：`cache_read_tokens`、`cache_creation_tokens`、`response_body` 落库且 JSON 合法。
  - 流式直通（含缓存字段）：四项计数正确，`response_body` 可解析且 content 文本等于流式拼接结果。
  - 流式转换（上游 OpenAI 带 `include_usage` 尾包）：token 映射正确，重建 JSON 合法。
- `tests/test_admin_logs_settings.py`：插入带 `response_body`/缓存计数的日志，断言列表页耗时形如 `1.20 s`、详情页出现格式化后的 JSON 关键字段与缓存数字。

### 六、明确不做

- 不改动客户端可见的流式 usage 事件内容。
- 不存储原始 SSE 文本。
- 不做 OpenAI 入口流式出参重建与 usage 采集（下一轮）。
- 不改耗时列名、不做毫秒→秒的数据迁移。

## 影响面

- 修改：`app/db.py`、`app/logging_.py`、`app/executor.py`、`app/convert/stream_a2o.py`、`app/admin/routes.py`、`app/admin/templates/logs.html`、`dashboard.html`、`log_detail.html`。
- 新增：`app/usage.py`、`app/reconstruct.py`、`tests/test_usage.py`、`tests/test_reconstruct.py`。
- 无 API 契约变化、无桌面端（Tauri）改动。
