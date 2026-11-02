# 开发指南

## 环境与入口

项目通过 `.python-version` 选择 Python 3.13，使用 uv 管理依赖。运行以下命令准备环境并查看产品命令：

```powershell
uv sync --locked
uv run seekra --help
```

配置和会话操作见[使用说明](usage.md)，执行与状态关系见[系统架构](architecture.md)。

## 目录和文件用途

|位置|作用与维护方式|
|---|---|
|`src/seekra/`|产品源码；按应用、SDK、搜索、终端职责组织|
|`tests/`|快速、确定的工程回归；使用假模型和固定检索检查真实 SDK 接入|
|`evals/`|可复用任务、固定资料和人工判据；运行方式见[评估说明](../evals/README.md)|
|`scripts/`|开发辅助命令，目前保留统一评估入口 `run_live_case.py`；不参与产品启动|
|`docs/`|使用说明、系统架构与开发指南|
|`dist/`|`uv build` 生成的 wheel 和源码分发包；不是源码或运行依赖，用完可删除|
|`.venv/`|项目 Python 环境，由 `uv sync --locked` 准备；日常保留|
|`.uv-cache/`|uv 的下载与构建缓存，路径由 `pyproject.toml` 配置；日常保留以避免重复下载|
|`__pycache__/`|Python 自动生成的字节码缓存，可再生成|
|`.seekra/`|本地会话、来源和工作文件；可能还包含凭据等历史数据，不能按缓存清理|
|`.git/`|版本历史与仓库状态，不作为整理对象|
|`pyproject.toml`、`uv.lock`、`.python-version`|包定义、依赖锁定与解释器选择；保持一致|
|`.env`、`seekra.toml`|本地凭据与个人配置；示例文件用于说明可配置内容|
|`AGENTS.md`|本地协作准则|

## 按修改选择验证

|改动|重点入口与风险|
|---|---|
|配置和模型接入|`config.py`、`sdk/models.py`；档案校验、工具绑定、推理字段往返和序列化|
|搜索和来源|`search/retrieval.py`、`tools.py`、`sources.py`；失败反馈、并发发布、幂等和损坏检测|
|会话和恢复|`app.py`、`sdk/sessions.py`；锁、待恢复状态、配置指纹和重启后的消息|
|流和终端|`sdk/events.py`、`terminal/`；摘要分类、最终输出、多行粘贴、草稿和取消|
|评估入口|`scripts/run_live_case.py`；输入隔离、诊断记录、外部停止条件|

```powershell
uv run python -X utf8 -m unittest discover -s tests -q
```

产品、SDK 集成和 CLI 检查分别集中在 `test_sdk_products.py`、`test_sdk_integration.py`、`test_sdk_cli.py`；输入检查在 `test_input_editor.py` 和 `test_windows_input.py`。Windows 实机按键项需要真实控制台，跳过不能算作实机通过。

升级 SDK 时检查工具 schema、文件权限、摘要、SQLite 恢复和事件分类。v3 事件与 profile API 的变化可能影响接入，依赖锁定不是未来兼容保证。模型协议问题优先检查实际集成的转换与请求。

工程测试检查行为契约；真实模型运行和答案判读使用 evals。根据改动选择验证，不为纯说明调整默认调用付费模型。评估调用上限与超时仅属于运行器。

## 构建

```powershell
uv build
```

默认在 `dist/` 生成 `.whl` 和 `.tar.gz`。wheel 用于安装包，sdist 用于从源码构建；本地开发通常直接 `uv run`，不需要持续保留构建产物。需要临时检查时可用 `uv build --out-dir <临时目录>`。

分发前检查实际包内容、安装入口及依赖。Git 忽略规则与构建包含规则各自生效，不能用“已忽略”证明密钥、会话、缓存或本地文档不会进入安装包。

## 记录检查结果

在变更说明中记录相关检查的命令、结果和未覆盖范围。真实模型评估还需说明模型、案例版本和输入条件，并分别记录执行结果与答案判读。题面改变后，新结果不能直接延续旧题成绩。

一次性探针放系统临时目录；仅在确实需要复查特定现场时保留原始输出。任务结束后判断材料用途，清理不再需要的文件。`evals/runs/` 是可选输出位置，不是永久归档约定。
