# 系统架构

Seekra 由终端交互、应用入口、搜索能力和 SDK 集成组成。应用入口选择模型与会话，搜索 Agent 使用 Deep Agents 执行研究任务，来源快照和会话工作区保存任务使用的资料与文件。

## 整体结构

```text
terminal/cli.py → app.Seekra → search.create_search_agent → Deep Agents / LangGraph
                      │                  ├─ ChatLiteLLM → 模型服务
                      │                  ├─ search_web / fetch_page → Tavily → Sources
                      │                  ├─ 原生文件工具 / Skills / 摘要
                      │                  └─ SQLite checkpointer
                      └─ sdk/events.py → terminal/display.py
```

|入口|职责|
|---|---|
|[app.py](../src/seekra/app.py)|组装一次提交，检查会话状态、执行图并确认结果|
|[config.py](../src/seekra/config.py)|读取配置和密钥、校验命名模型档案|
|[sdk/models.py](../src/seekra/sdk/models.py)|将模型档案转换为 ChatLiteLLM 实例和 model profile|
|[sdk/sessions.py](../src/seekra/sdk/sessions.py)|会话目录、兼容指纹、进程间互斥和 checkpoint 连接|
|[sdk/workspace.py](../src/seekra/sdk/workspace.py)|工作区 backend、文件工具权限和元数据原子发布|
|[sdk/events.py](../src/seekra/sdk/events.py)|将 SDK 流转换为产品展示事件|
|[search/agent.py](../src/seekra/search/agent.py)|组合研究提示、搜索工具、文件工具、Skills 和 checkpointer|
|[search/retrieval.py](../src/seekra/search/retrieval.py)|Tavily 搜索、页面提取和业务失败反馈|
|[search/tools.py](../src/seekra/search/tools.py)|将获取结果保存为来源，并向 Agent 返回读取入口|
|[search/sources.py](../src/seekra/search/sources.py)|不可变来源快照、元数据和内容完整性检查|
|[terminal/](../src/seekra/terminal/)|命令、输入编辑、Windows 按键、取消和事件展示|

Search 通过工具和 backend 使用 SDK；终端通过应用入口和产品事件使用 Search。SDK 集成模块不依赖终端或搜索业务。

## 请求执行

1. CLI 读取配置，选择或创建会话；应用核对档案和 SDK 兼容指纹。
2. 取得会话文件锁并打开 SQLite checkpointer，构造模型、检索服务和搜索图。
3. 通过图的 `aget_state` 读取状态。新问题写入 `HumanMessage`；继续执行传入 `None`。有待执行节点时拒绝新问题，避免覆盖待恢复任务。
4. 消费图的原生 v3 流，向终端发送文本、工具和用量事件。
5. 流结束后再次读取图状态。有待执行节点则返回 `interrupted`；否则检查最后一条消息是否为无工具调用的完整 AI 回答。
6. 完整回答发布 `final` 事件并更新会话时间。截断、过滤、无最终正文或无效工具调用不会被宣布为完整答案。退出上下文时释放 SQLite 连接和会话锁。

模型失败向调用方传播；Ctrl+C 通过 `terminal/cancellation.py` 取消运行中的 asyncio 任务，流上下文负责关闭图执行。

## 会话、状态与存储

|对象|存储|归属与读取方式|
|---|---|---|
|模型消息、工具执行、摘要、待执行节点|`checkpoints.sqlite`|LangGraph 管理；通过编译图状态 API 读取|
|ID、标题、模型名、更新时间、兼容指纹|`sessions/<ID>.json`|`Sessions` 管理，用于选择会话|
|运行互斥|`sessions/<ID>.lock`|文件锁保护跨进程执行|
|来源、笔记、输出和历史文件|`workspaces/<ID>/`|文件 backend 和各写入方管理|

checkpoint 可能使用 delta channels，底层 `channel_values` 不能直接视为完整历史。对话展示也通过图 API 读取消息。

兼容指纹包含模型档案、选定依赖版本和应用运行契约标记。会话恢复依赖这些条件一致。更改恢复契约时应检查并更新应用标记。

checkpoint 与来源文件分别持久化，没有跨存储事务。恢复可以重放工具；来源重复发布按内容幂等，外部请求仍可能执行多次。取消后已保存的数据保留。

## 搜索与来源资料

`search_web` 取得标题、URL 和片段并保存 `snippet`；`fetch_page` 提取正文并保存 `page`。提取结果返回文件路径，Agent 通过原生 `read_file`、`grep`、`glob`、`ls` 读取和定位。

来源 ID 由实际 URL、请求 URL、类型与正文哈希生成。相同身份的内容可重复发布；正文变化产生新快照。元数据包含标题、URL、类型、获取时间、正文哈希与读取路径。只有正文和元数据都已发布，工具才返回可用来源。已存在正文损坏时拒绝复用。

模型用真实 URL 引用答案。研究提示要求核对版本、条件、例外和未知前提；笔记用于组织研究，事实依据仍来自来源。提取服务可能遗漏内容或返回回退页面，程序保存成功不证明目标页面完整，也不认证结论支持关系。

## 模型与上下文

`ModelProfile` 提供 provider、model、端点、密钥变量、token 预算和厂商选项。`create_model` 构造流式 ChatLiteLLM，将输入预算写入 model profile，供 SDK 管理摘要阈值。凭据显式传给集成；模型档案本身只保存密钥变量名。

Deep Agents 管理模型与工具执行、消息整理、摘要及历史外置。文件 backend 保存外置资料；模型可以回查来源和可用历史。SDK 摘要成功只说明流程执行，条件保真需要另外评估。

## 终端与事件

`sdk/events.py` 并发消费消息流和工具流，生成 `text`、`tool_start`、`tool_end`、`model_end` 等事件。SDK 回调的 `lc_source=summarization` 标记用于识别内部摘要；其正文不显示，用量仍发出。

`terminal/display.py` 将中间输出写入 stderr，只有应用确认的 `final` 写入 stdout。展示过滤控制字符并隐藏已知 API 密钥。事件是展示投影，不能用工具事件数量推算网络请求数；Tavily 包装工具也可能产生内部事件。

输入编辑器在任务运行期间保留草稿、处理粘贴与滚动。Windows 适配负责原生按键记录，取消桥接负责把输入线程的信号传给异步任务。

## 文件权限与工具范围

工作区使用虚拟路径。模型的原生文件工具可写 `/notes/**` 和 `/outputs/**`，`/sources/**`、`/skills/**` 对模型只读；SDK 内部摘要和工具结果外置通过 backend 写入。

网络工具使用程序生成的路径发布来源。这些自定义工具的写入由自身实现约束，原生文件权限不覆盖任意自定义工具。URL 校验检查 HTTP(S)、主机和地址形式，不控制 Tavily 端的 DNS 或跳转。

Search 注册的 HarnessProfile 禁用默认子 Agent 及 `task`、`execute` 工具。profile 注册表具有进程级作用域，扩展不同能力时需检查配置互相影响。`/skills/` 由 SDK 原生加载，目前未预置技能。FilesystemBackend 提供文件访问，不提供进程隔离沙箱。
