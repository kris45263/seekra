# 开发指南

Seekra 使用 Python 3.13、uv 和同步 Python 模块。安装与用户命令见 [README](../README.md)，模块协作见 [架构](architecture.md)。

## 环境与运行

在仓库根目录执行：

```powershell
uv sync --locked
uv run seekra --help
uv run seekra --repl
```

首次运行按 README 配置 `.env`，不要覆盖已有配置。默认 DeepSeek 入口需要 DeepSeek 与 Tavily 配置；ChatGPT 订阅入口完成单独授权后只需 Tavily 配置。CLI 默认读取启动目录的 `.env`，其他路径通过 `--env-file PATH` 指定。帮助、版本和离线测试无需真实密钥。

`uv tool install --python 3.13 --editable .` 可安装本地源码命令。移动源码目录后需重新安装；项目虚拟环境可通过 `uv sync --locked` 重新绑定。

## 修改入口

|修改目标|主要模块|需要关注的行为|
|---|---|---|
|研究循环与任务结束|`agent.py`、`runtime.py`|请求历史、工具配对、取消、失败与完成状态|
|上下文与恢复|`context.py`、`checkpoint.py`、`compaction.py`、`provider_context.py`|实际请求、独立压缩提交、预算与续接缺失后的重建|
|模型协议与授权|`models.py`、`deepseek.py`、`chatgpt.py`、`chatgpt_auth.py`|流结束校验、工具组装、连接释放、身份与令牌刷新|
|工具与网络获取|`tools.py`、`retrieval.py`、`operations.py`|非法参数不联网、结果语义、失败原因与快照|
|正文与进展|`documents.py`、`delivery.py`、`research.py`、`progress.py`|稳定引用、实际交付范围、进展版本与局部更新|
|显式终稿|`answers.py`|原文校验、排版覆盖、依赖声明和恢复交付|
|终端与命令|`cli.py`、`display.py`、`input.py`、`windows_input.py`|配置来源、正文输出、运行中草稿与按键协议|
|持久会话|`session.py`|提交、重载、未知工具结果、文件锁与 reset 隔离|

以上模块位于 `src/seekra/`。正文、进展和历史视图都从已提交执行事实重建；判断上下文变更时，核对实际下一次模型请求，不能只看保存记录。

## 工程验证

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

测试使用假模型、MockTransport、本机测试服务及临时目录，不访问真实模型或搜索服务。安装入口测试会启动子进程。按修改范围选择测试与实测，具体见 [验证方法](validation.md)。

Windows 输入适配使用 prompt-toolkit 的内部键盘转换方法。升级依赖时检查该方法是否新增实例初始化状态，并运行 `test_windows_input.py`；剪贴板和物理组合键仍需实际 Windows Terminal 验证。

默认 CLI 保存会话；测试 CLI 时使用临时 `--session-dir` 或 `--no-session`。`Model.generate`、`ToolRegistry` 与 `execute_run` 是嵌入和离线模拟入口。恢复未完成任务的接口与兼容条件见架构说明。

## 能力检查与诊断

```powershell
uv run python -X utf8 scripts/run_live_case.py --list
uv run python -X utf8 scripts/check_documents.py --list
```

列举不调用服务。实际运行需要项目配置，会产生服务调用费用：

```powershell
uv run python -X utf8 scripts/run_live_case.py --case page --output evals/runs/page.json
uv run python -X utf8 scripts/check_documents.py --case long-section --output evals/runs/long-section.json
```

基础运行器保存答案与操作摘要；正文运行器记录实际公开请求、资料交付和执行记录，可使用固定资料或真实 Tavily。输出路径必须不存在。案例目标、固定夹具和精选证据见 [评估入口](../evals/README.md)。运行结果先作为过程产物使用，任务结束后仅保留有明确复查用途的内容。

`scripts/check_streaming.py` 单独诊断 DeepSeek 无工具流式文本、追问与输出故障，复用生产 SSE 接收器。它不覆盖完整研究循环或 ChatGPT 订阅入口；仅需要定位这一层时使用。

## 构建与分发

```powershell
uv build
```

`dist/` 内的包是构建时快照，不随源码变化；可编辑安装运行当前源码。分发前重新构建并验证安装入口。真实服务评估目前没有自动 CI 质量评分，离线测试通过不认证研究答案正确。
