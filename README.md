# Seekra

在终端中搜索、读取来源、继续追问的轻量研究助手。

Seekra 根据已有信息决定下一步：搜索网络、读取网页、整理研究笔记，或给出带来源的回答。你可以看到它搜索了什么、找到了哪些来源，以及正在读取哪个页面。

采用普通 Python，默认使用 DeepSeek 和 Tavily，也提供 ChatGPT 订阅模型入口，无需额外 Agent 框架。适合轻量资料研究，也便于观察、理解和扩展迭代搜索过程。

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
DEEPSEEK_MODEL=deepseek-flash
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
seekra "对比两个版本的行为，并提供来源"
seekra --help
seekra --version
```

| 参数 | 作用 |
| --- | --- |
| `问题` | 单次研究问题，与 `--repl` 二选一 |
| `--repl` | 连续对话 |
| `--verbose` | 展示工具参数、结果、累计调用和计时 |
| `--env-file PATH` | 指定配置文件，默认读取当前工作目录的 `.env` |
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

模型必须支持当前使用的工具调用协议。研究不按模型调用次数停止；信息足够时由模型回答，也可由用户中止。累计调用统计包含失败尝试，不等于工具调用次数，也不是费用上限；上下文字符限制独立生效。

每次执行结束汇总一次输入/输出 token；有更早的用量时再显示对话累计，取消或失败也统计已报告部分。`--verbose` 额外显示逐请求用量及可用的缓存命中率。缺失用量会标明不完整；token 消耗不代表下一次请求的上下文长度。

正文写入 stdout，欢迎信息、输入提示、来源和运行状态写入 stderr。工具前的模型说明也属于正文；完成后不会重复打印答案。流中失败或取消时，已经显示的正文无法撤回，重定向 stdout 的文件可能包含中间说明或失败残稿，应结合退出状态判断。

## 功能与边界

- **迭代搜索**：模型根据实际结果调整查询，可选择 1–10 条候选、每来源 1–3 个相关片段并限定明确域名；未指定片段数量时沿用 Tavily 默认。结果经过字段校验、基础整理和单次精确去重。跨轮同一来源的完全相同片段只展示短预览并标明关联，每次查询与完整片段仍保存；内容不同则保留。
- **读取与回查来源**：`read_page(url)` 通过 Tavily Extract 获取并保存页面文本；已有快照用 `read_section` 或 `read_document` 在本地阅读和续读。也可查找关键词，读取命中附近的正文。同一页面的搜索片段与提取快照保持来源关联；重载会话后可继续回查原快照。
- **核对引用范围**：`read_document(document_id=evidence_ref, view="reference")` 严格读取该引用的原文，在引用范围内分页；普通 `text` 模式可展开周边，返回覆盖新正文的新引用。扩大后的正文不能充当旧引用自身的内容。
- **保留研究进展**：模型用 `update_research` 局部新增或修订发现、条件、正文引用、缺口和阅读目标，未修改项目继续保留。每项有稳定编号；可用 `gap_ids` 关联正在调查的缺口，重要发现可用 `evidence_quotes` 选取短原句。程序限定引用范围定位文字，保存实际原文和坐标；它检查出处，不认证结论。被引用资料保留累计交付与当前可见概况，阅读目标移除后仍可查看。没有依据的事项可先保存为缺口，并用 `answer_impact` 说明它对原问题的作用；已知小节、查找词或续读游标可保存为阅读目标。最新有效版本可重载，正文仍回查同一快照。模型根据原问题决定是否继续研究，信息足够时可带未确定项回答，无需清空所有缺口。
- **整理长任务上下文**：接近上下文字符预算时，先利用已有进展和旧正文回查入口腾出空间；仍需整理时，进入临时保存阶段，保存进展后恢复普通搜索与阅读。该阶段保留已有引用的局部回查，也允许直接回答；更新失败时保留旧进展与旧轮次。成功保存后按检查点移出较早的完整轮次，保留原始问题、最新进展和近期结果。完整记录与快照仍可回查，中断重载后恢复阶段与检查点。整理无法保证没有信息遗漏或模型误述；必要内容仍超限时暂停。
- **整批结果与历史查看**：多个新工具结果超出剩余预算时，程序缩短实际展示内容并调整引用和继续入口。`read_history` 默认查看原始消息、失败原因与资料入口；`view="raw"` 显式查看事件 JSON。原文小节和引用可通过资料工具回查。
- **连续追问与恢复**：持久记录完整步骤与错误；新问题使用完成历史，未完成研究可显式继续。
- **观察过程**：默认流式输出，工具调用完整接收后执行；普通界面显示来源，`--verbose` 提供执行细节。

工具成功只代表取得内容，不证明来源可靠、版本适用或模型结论正确。网页提取可能失败或不完整，不支持登录及交互式浏览器操作。同一 Session 不支持并发运行。

研究状态、资料窗口和每次请求分别有大小限制；字符上限不等于模型 token 窗口或本地内存上限。必要内容无法放入请求时任务会暂停，保存的记录仍可回查。默认使用研究进展工具；旧笔记保留 Python 兼容入口。接口与恢复规则见 [架构说明](docs/architecture.md)。

## 开发者入口

先运行 `uv sync --locked`，再执行不联网的工程测试：

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

按需查阅：

- [架构与数据契约](docs/architecture.md)：研究循环、模块职责、Session、流式执行与设计理由。
- [开发指南](docs/development.md)：开发环境、修改位置、运行测试与构建分发包。
- [验证方法与能力边界](docs/validation.md)：测试用例、判定标准、已验证和未覆盖范围。
- [研究进展](docs/research-progress.md)：发现、缺口、引用与阅读目标的保存和更新。
- [研究发现与已知问题](docs/research-findings.md)：条件忠实性、进展使用和模型复核的证据边界。
- [评估案例与证据](evals/README.md)：可重复的能力检查、固定夹具和精选反例。

### 原生 reasoning 与恢复

模型配置使用 `DEEPSEEK_MODEL=deepseek-flash`。CLI 与 Python 入口 `run_agent` 默认开启原生 reasoning，默认 `reasoning_effort="high"`、`max_output_tokens=8192`。后续研究能力验证以开启 reasoning 为前提，核对 harness 的信息交付和模型行为；答案质量仍需实际核对。Python 的显式 `thinking=False` 用于旧记录兼容与受控诊断。模式改变会触发恢复配置检查，旧的关闭模式任务不能静默切换后续接。旧笔记仅保留显式 Python 实验入口。

工具调用所需的推理字段在进程内随实际保留的交互回传，并计入请求字符预算；日志和历史工具不保存或展示推理全文。重载后缺少续接数据，或必要推理历史无法容纳时，程序依据原问题、保存进展、资料目录、最新工具结果组和较早正文摘录重建输入。最新结果优先保留；预算不足时先移出旧摘录，再明确裁剪新正文并提供续读入口，必要内容仍放不下时暂停。保存失败的错误仍可见；原执行记录与快照继续回查。这种恢复保留业务进度，长任务的恢复质量还需验证。模式、effort 和输出预算纳入恢复配置身份。

## 使用 ChatGPT 订阅模型

可选入口固定使用 `gpt-6.1-sol`、`medium` reasoning，研究循环、资料工具和终端仍由 Seekra 管理。需要符合条件的 ChatGPT 账号，并在浏览器中单独授权 Seekra 使用套餐；Codex 的登录状态不会自动转移过来。实际模型权限及可用额度由服务确认。

```powershell
uv run seekra --chatgpt-login
uv run seekra --provider chatgpt --repl
```

首次命令会打开系统浏览器。登录后，此入口只要求 `.env` 中的 `TAVILY_API_KEY`，不需要 DeepSeek 或 OpenAI API key。普通 `uv run seekra --repl` 仍使用 DeepSeek。使用多个账号或工作空间时，为每个注册指定不同的 `--chatgpt-profile 名称`，登录和运行时使用同一标签；同标签重授权不能覆盖成另一账号。

凭据存放在启动目录的 `.seekra/chatgpt/`，Windows 使用当前用户的 DPAPI 加密，其他系统使用仅所有者可访问的文件权限。访问令牌临近过期时自动刷新；刷新失败会停止并提示重新登录，不切换到 API key 计费。该目录应留在本机，不复制到证据或 Git。使用量与撤销授权可在 [ChatGPT 设置](https://chatgpt.com/settings/usage) 管理。

订阅入口使用 [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source) 和 Responses API，流结束必须收到 `response.completed` 才算成功；额度错误、截断和中断不会作为最终回答。该通道当前不接受 `max_output_tokens`，不宣称与 DeepSeek 的 8192 输出预算相同。两种供应商的未完成任务不能静默互换模型后续接。

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
   ├─ chatgpt.py     ChatGPT 订阅模型适配
   ├─ chatgpt_auth.py  订阅授权与受保护凭据
   ├─ checkpoint.py  预算整理阶段与权限
   ├─ provider_context.py  供应商续接字段与业务上下文重建
   ├─ context.py     上下文投影和历史索引
   ├─ deepseek.py    SSE 接收、正文与工具调用组装
   ├─ tools.py       工具定义、参数校验与分派
   ├─ retrieval.py   搜索结果处理、页面读取与续读
   ├─ documents.py   来源关联、正文快照、小节与本地回查
   ├─ research.py    研究进展校验与正文交付范围
   ├─ answers.py     可选显式终稿校验与交付
   ├─ progress.py    进展局部合并、稳定编号与阅读导航
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
