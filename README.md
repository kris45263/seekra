# Seekra

在终端中搜索、读取来源、继续追问的轻量研究助手。

Seekra 根据已有信息决定下一步：搜索网络、读取网页、整理研究笔记，或给出带来源的回答。你可以看到它搜索了什么、找到了哪些来源，以及正在读取哪个页面。

采用普通 Python，使用 DeepSeek 和 Tavily，无需额外 Agent 框架。适合轻量资料研究，也便于观察、理解和扩展迭代搜索过程。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.13，以及有效的 DeepSeek 和 Tavily API key。uv 可在本机缺少所需 Python 时下载对应版本。

```powershell
git clone https://github.com/kris45263/seekra.git
cd seekra
uv sync --locked
Copy-Item .env.example .env
```

已有 `.env` 时跳过复制，避免覆盖配置。应用名称为 Seekra，仓库、Python 包和命令名称统一为 `seekra`。

编辑项目根目录的 `.env`：

```dotenv
DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_MODEL=deepseek-v4-flash
TAVILY_API_KEY=你的Tavily密钥
```

密钥可在 [DeepSeek 平台](https://platform.deepseek.com/) 和 [Tavily 控制台](https://app.tavily.com/) 创建。调用服务会产生相应费用；`.env` 已被 Git 忽略。

然后启动：

```powershell
uv run seekra --repl
```

Seekra 在命令入口统一使用 UTF-8 处理输入输出。

## 直接使用 seekra 命令

如果希望每次只输入 `seekra --repl`，在项目根目录执行一次：

```powershell
uv tool install --python 3.13 --editable .
```

安装完成后：

```powershell
seekra --repl
seekra "查找 Python 3.13 的主要改进，并提供官方来源"
```

这是从当前本地项目安装，不需要从 PyPI 下载同名项目。可编辑安装会使用本地源码；请保留该项目目录。修改依赖或移动目录后，重新安装工具。

如果 uv 提示工具目录不在 PATH 中，执行 `uv tool update-shell`，再打开一个新终端。它会更新用户的 shell / PATH 配置；项目本身不会自动修改全局环境。[uv 工具安装说明](https://docs.astral.sh/uv/guides/tools/)

仅 clone 不会向 shell 注册命令。也可以不安装全局工具：继续使用 `uv run seekra`，或在当前终端激活项目虚拟环境后使用 `seekra`。PowerShell 的激活命令为 `.\.venv\Scripts\Activate.ps1`；若本机策略不允许运行该脚本，使用前两种方式即可。

移除工具可运行 `uv tool uninstall seekra`，不会删除项目源码。

## 连续对话

```text
Seekra · 对话 …
想查什么？输入问题开始，或用 /resume 接着上次聊。

› Python 3.13 的主要改进有哪些？请提供官方来源。
```

Enter 发送输入框中的完整内容；Shift+Enter 换行，终端不支持时可用 Ctrl+J。粘贴保留换行，不自动发送。Ctrl+Home/End 跳到输入内容首尾。Windows Terminal 中可按住 Shift 拖选文本，再用 Ctrl+Shift+C 复制。

模型运行期间仍可编辑和粘贴；此时按 Enter 会提示消息尚未发送。回答结束后，再按 Enter 发送下一条。交互界面使用全屏布局，标题和输入区状态栏持续显示当前对话；退出后回到原终端。向上滚动时保持阅读位置，回到底部后跟随新输出；两个区域都可查看折行的长文本。只有较早内容超出显示范围时，状态栏才提示“更早内容未显示”，保存的对话记录不受影响。

| 会话命令 | 作用 |
| --- | --- |
| `/help` | 查看会话命令与快捷键 |
| `/new` | 创建新会话并清空对话显示，保留旧会话文件 |
| `/sessions` | 列出保存的对话，隐藏无内容的空会话 |
| `/open ID或序号` | 打开并显示原问答；序号对应最近一次列表 |
| `/resume` | 打开最近一次有内容的会话，不调用模型；“最近”按最后写入时间判断 |
| `/continue` | 接续当前会话最后一次未完成执行 |
| `/exit` | 退出 Seekra |

回答期间 Ctrl+C 停止当前回答；空闲时 Ctrl+C 清空输入，输入为空时退出；空闲且输入为空时 Ctrl+D 也可退出。空输入不调用模型。默认保存失败或取消的执行记录；提交新问题使用此前完成的对话，输入 `/continue` 可继续最后一次未完成的回答。必要的续接提示在执行失败或打开对话时显示。

同一对话内，模型可以通过历史工具主动回读失败或取消的记录，结果会标明原问题与任务状态。`/new` 开始独立会话；Python 接口 `Session.clear()` 则在原会话中设置重置边界，清空对话和页面缓存、禁止恢复或回读旧记录，同时保留磁盘原始数据。

将文本通过管道传给 `--repl` 时，以 EOF 为一次提交边界，整个多行文本作为一条消息处理，其中的 `/new` 或 `/exit` 不作为会话命令执行。未提交的草稿不创建任务、不进入持久会话。

默认自动保存到启动目录的 `.seekra/sessions/`，该目录应保持本地私有。普通启动会新建会话；`/resume` 跳过空会话，恢复最近有内容的对话，已完成的对话也可恢复并继续追问。仅需续接最后一次未完成执行时使用 `/continue`；提交新问题后，旧未完成执行仍保留记录，但不再能通过 `/continue` 续接。重启后可以继续已有会话：

```powershell
seekra --sessions
seekra --session 会话ID --repl
seekra --resume
seekra --resume 会话ID
seekra --continue 会话ID
```

ID 可以使用至少 8 位且唯一的前缀。指定 `--session-dir PATH` 可使用其它目录；`--no-session` 不落盘。恢复不会重复已完成工具；若工具已开始但结果未知，则停止自动恢复。改变模型、工具定义或系统提示模板后，需要使用原配置恢复或提交新问题。

## 命令与配置

下列示例使用安装后的 `seekra`；在项目目录内均可替换为 `uv run seekra`。

```powershell
seekra --repl --verbose
seekra "对比两个版本的行为，并提供来源" --max-iterations 8
seekra --help
seekra --version
```

| 参数 | 作用 |
| --- | --- |
| `问题` | 单次研究问题，与 `--repl` 二选一 |
| `--repl` | 连续对话 |
| `--verbose` | 展示工具参数、结果、轮次预算和计时 |
| `--max-iterations N` | 每个问题最多请求模型 N 次，默认 12，恢复时不自动重置 |
| `--env-file PATH` | 指定配置文件，默认读取当前工作目录的 `.env` |
| `--structured-think` | 实验性结构化研究笔记，默认关闭 |
| `--session ID` / `--resume [ID]` | 选择会话 / 打开最近有内容的会话或指定会话进入 REPL |
| `--continue ID` | 单次接续指定会话最后一次未完成执行 |
| `--sessions` / `--session-dir PATH` | 列出会话 / 指定存储目录 |
| `--no-session` | 仅在内存中记录，不保存会话文件 |
| `--max-context-chars N` | 消息与工具定义字符上限，默认 120000；不是 token 数 |
| `--help` / `--version` | 查看帮助 / 版本，不需要配置密钥 |

默认配置以**启动时所在目录**为准，不从安装目录或父目录自动查找，也不使用系统环境中的同名密钥。它不会展开配置值中的环境变量。在其他目录启动时，可以明确指定文件：

```powershell
seekra --env-file "C:\path\to\seekra\.env" --repl
```

模型必须支持当前使用的工具调用协议。模型调用轮次不等于工具调用次数，也不是费用上限；达到轮次上限仍没有答案时会明确报错。

每次执行结束汇总一次输入/输出 token；有更早的用量时再显示对话累计，取消或失败也统计已报告部分。`--verbose` 额外显示逐请求用量及可用的缓存命中率。缺失用量会标明不完整；token 消耗不代表下一次请求的上下文长度。

正文写入 stdout，欢迎信息、输入提示、来源和运行状态写入 stderr。工具前的模型说明也属于正文；完成后不会重复打印答案。流中失败或取消时，已经显示的正文无法撤回，重定向 stdout 的文件可能包含中间说明或失败残稿，应结合退出状态判断。

## 功能与边界

- **迭代搜索**：模型根据实际结果调整查询，可选择 1–10 条候选并限定明确域名；结果经过字段校验、基础整理和单次精确去重。
- **读取来源**：通过 Tavily Extract 获取指定 URL 的文本，长页面可以继续读取同一内容快照。
- **连续追问与恢复**：持久记录完整步骤与错误；新问题使用完成历史，未完成研究可显式继续。
- **观察过程**：默认流式输出，工具调用完整接收后执行；普通界面显示来源，`--verbose` 提供执行细节。

工具成功只代表取得内容，不证明来源可靠、版本适用或模型结论正确。网页提取可能失败或不完整，不支持登录及交互式浏览器操作。同一 Session 不支持并发运行。上下文超限时，旧任务连同其问题、模型决策和工具结果整组移出，长工具正文可改为原文引用；当前任务的消息关系保留，历史可按需回读，不自动摘要。当前任务仍超限时暂停。字符上限不等于模型 token 窗口或本地内存上限，长问题仍可能被服务拒绝。结构化笔记尚未证明普遍改善答案质量。

## 开发者入口

先运行 `uv sync --locked`，再执行不联网的工程测试：

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

按需查阅：

- [架构与数据契约](docs/architecture.md)：研究循环、模块职责、Session、流式执行与设计理由。
- [开发指南](docs/development.md)：开发环境、修改位置、运行测试与构建分发包。
- [验证方法与能力边界](docs/validation.md)：测试用例、判定标准、已验证和未覆盖范围。
- [研究发现与已知问题](docs/research-findings.md)：think、证据归因、目标匹配和噪声的实际观察。
- [实验案例与证据](evals/README.md)：问题原文、精选结果和复现脚本。

## 项目结构

```text
pyproject.toml
README.md
src/
└─ seekra/
   ├─ __init__.py
   ├─ __main__.py    python -m seekra 入口
   ├─ cli.py         配置、命令行与 REPL
   ├─ input.py       持续编辑区、粘贴与输入调度
   ├─ windows_input.py  Windows 键盘协议适配
   ├─ agent.py       模型循环、预算、会话提交与回退
   ├─ runtime.py     模型与运行接口、取消令牌
   ├─ models.py      DeepSeek 适配与用量归一化
   ├─ context.py     上下文投影和历史索引
   ├─ deepseek.py    SSE 接收、正文与工具调用组装
   ├─ tools.py       工具定义、参数校验与分派
   ├─ retrieval.py   搜索结果处理、页面读取与续读
   ├─ operations.py  公共操作结果约定
   ├─ display.py     终端展示
   └─ session.py     持久执行记录与会话恢复
tests/             离线工程回归
docs/              开发者知识库
evals/             真实案例与实验证据
scripts/           开发和实验入口
```

## 帮助与参考

项目由 [kris45263](https://github.com/kris45263) 维护。问题或建议可提交到 [Issues](https://github.com/kris45263/seekra/issues)，请提供复现命令、预期与实际行为，并删除密钥及敏感内容。

研究闭环参考 [langchain-ai/deep_research_from_scratch 的 Notebook 2](https://github.com/langchain-ai/deep_research_from_scratch/tree/93f35e5d2a51590f9542207a9ff66a01901da5bc)。

协议文档：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)。
