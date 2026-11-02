# Seekra

Seekra 是一个终端研究助手，可以搜索网页、读取来源、核对资料并继续追问。它使用 Deep Agents 执行研究任务，通过 Tavily 搜索和提取网页，将来源快照与会话保存在本地。

## 快速开始

需要 Python 3.13、[uv](https://docs.astral.sh/uv/)、模型 API key 和 Tavily API key。

```powershell
uv sync --locked
Copy-Item seekra.example.toml seekra.toml
Copy-Item .env.example .env
```

已有配置时跳过复制，避免覆盖。把密钥填入 `.env`，在 `seekra.toml` 中填写账户可用的模型名称和端点，然后启动：

```powershell
uv run seekra --repl
```

也可以直接提交一个问题：

```powershell
uv run seekra "查找 Python 3.13 的主要改进，并提供官方来源"
```

## 模型与会话

提供 DeepSeek、Kimi 和 GLM 的命名配置档案，通过 `--model` 为新会话选择模型：

```powershell
uv run seekra --model kimi-research --repl
uv run seekra --sessions
uv run seekra --session 会话ID --repl
uv run seekra --help
```

交互模式中，Enter 发送，Shift+Enter 或 Ctrl+J 换行，`/new` 新建会话，`/open ID` 打开会话，`/continue` 继续未完成任务。运行时按 Ctrl+C 取消。

模型示例不是型号推荐。真实执行检查目前以 DeepSeek 为主，Kimi 和 GLM 已检查配置构造，尚未完成真实服务验证。来源提取可能不完整，回答中的重要判断仍需核对引用及其适用条件。

## 文档

- [使用说明](docs/usage.md)：配置、命令、会话和终端操作。
- [系统架构](docs/architecture.md)：执行过程、模块职责和数据存储。
- [开发指南](docs/development.md)：目录用途、开发检查和构建。
- [评估说明](evals/README.md)：可复用案例、运行方式和判读方法。
