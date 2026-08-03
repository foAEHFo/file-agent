# 文件助理 Agent

文件助理可以在指定工作区内搜索、读取、创建、覆盖和移动文件。项目同时提供
浏览器界面和命令行工具。

## 准备环境

需要 Python 3.12，以及一个支持 Responses API 的 LLM模型。

```bash
make setup
cp .env.example .env
```

CLI 至少需要：

```env
OPENAI_API_KEY="你的 API Key"
OPENAI_MODEL="模型名称"
```

运行 Web 时还需要：

```env
DEMO_USERNAME="demo"
DEMO_PASSWORD="本地登录密码"
SESSION_SECRET="至少 32 字节的随机字符串"
```

可以生成一个随机 `SESSION_SECRET`：

```bash
openssl rand -hex 32
```

其余配置可保留默认值：

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Responses API 地址 |
| `WORKSPACE_SEED_PATH` | `./workspace` | Web 工作区的目标目录 |
| `RUNTIME_ROOT` | `/tmp/file-agent` | Web 临时副本和 trace 的存放目录 |
| `SESSION_TTL_SECONDS` | `3600` | 闲置工作区的保留时间 |
| `MAX_LLM_CALLS` | `30` | 每次 Run 最多模型调用次数 |
| `MAX_TOOL_CALLS` | `100` | 每次 Run 最多工具调用次数 |
| `RUN_TIMEOUT_SECONDS` | `1200` | 每次 Run 的活动时间上限 |
| `MAX_RUN_FILE_CONTENT_BYTES` | `262144` | 每次 Run 可读文件正文总量 |

不要把 `.env` 提交到 Git。

## 本地启动 Web

加载 `.env` 并启动服务：

```bash
set -a; source .env; set +a; .venv/bin/uvicorn file_agent.web:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>，使用 `DEMO_USERNAME` 和
`DEMO_PASSWORD` 登录。

Web 的基本使用方式：

1. 在任务框中用自然语言描述要查找、整理或修改的文件。
2. 在中间查看推理摘要和回答，在右侧查看工具过程、审批和 Token 用量。
3. 遇到创建、覆盖或移动操作时，核对参数后逐次批准或拒绝。
4. 在左侧刷新文件树并点击文件预览内容。
5. 点击 trace 链接下载当前 Run 的 `trace.jsonl`。
6. 没有 Run 正在执行时，可用 **Reset Workspace** 恢复种子文件并清空模型上下文。

Web 操作的是 `WORKSPACE_SEED_PATH` 的临时副本，不会修改原始种子目录。状态只在
当前服务进程中有效；停止服务后，聊天上下文不会恢复。按 `Ctrl+C` 停止服务。

## 使用 CLI

先加载环境变量：

```bash
set -a; source .env; set +a
```

执行一个只读任务：

```bash
.venv/bin/python -m file_agent.cli \
  --workspace ./workspace \
  --task "帮我整理一下 Project Falcon 的时间线，并按月份分组。" \
  --trace ./trace.jsonl
```

参数说明：

| 参数 | 是否必填 | 说明 |
| --- | --- | --- |
| `--workspace` | 是 | Agent 可以访问的工作区目录 |
| `--task` | 是 | 要执行的自然语言任务 |
| `--trace` | 否 | trace 输出位置，默认 `./trace.jsonl` |
| `--yes` | 否 | 自动批准每个写操作 |

CLI 会流式显示 `[推理]`、`[工具]`、`[结果]`、`[回答]` 和 `[用量]`。默认情况下，
每个写操作都会在 TTY 中单独询问；输入 `y` 或 `yes` 批准，其他输入拒绝。标准输入
不是 TTY 时，写操作默认拒绝。

仅在受控测试或自动化环境中使用 `--yes`：

```bash
.venv/bin/python -m file_agent.cli \
  --workspace ./workspace \
  --task "把内容状态明确为 obsolete 的草稿移动到 archive 目录。" \
  --trace ./trace.jsonl \
  --yes
```

`--yes` 只跳过人工确认，不会绕过工作区隔离、目标冲突或文件 SHA-256 校验。

查看完整 CLI 帮助：

```bash
.venv/bin/python -m file_agent.cli --help
```

## 运行检查

测试默认使用假的 Responses 客户端，不会调用真实 API：

```bash
make check
```
