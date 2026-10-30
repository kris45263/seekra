# Seekra 架构

本文面向理解基本 Python 和模型工具调用、但尚未读过 Seekra 源码的开发者，说明模块如何协作、运行状态由谁保存，以及任务中断后如何继续。安装和用户命令见 [README](../README.md)，开发命令见 [开发指南](development.md)，验证方法与实验结论分别见 [验证指南](validation.md) 和 [研究发现](research-findings.md)。

研究进展的字段、引用和发布过程见 [研究进展](research-progress.md)，模型行为的已知限制见 [研究发现](research-findings.md)。本文描述当前实现。

## 整体结构

Seekra 当前的产品是终端搜索研究助手。内部把可复用的执行机制与搜索能力分开：Harness 管理一次运行如何推进和提交，Search 决定搜索任务使用什么提示、工具、资料状态与上下文策略；Provider 负责模型协议，Terminal 负责用户交互。

```text
terminal.cli.main / run_question
  → search.agent.run_agent 或 run_chatgpt
  → search.agent.run_search：模型、SearchSession、工具、取消令牌
  → search.agent.execute_run：附加 SEARCH_AGENT 策略
  → harness.loop.execute_run
       ├─ AgentDefinition.prepare_request → Search 上下文/恢复/压缩
       ├─ Model.generate → Provider 协议
       ├─ ToolRegistry.execute → 注册的具体工具
       ├─ Session.record / complete_model → 持久化与状态投影
       └─ on_content / on_event → Terminal 展示
```

所有源码在 `src/seekra/`。两个命令入口都进入 `terminal.cli.main`。完整目录见 README，按修改目标查找实现见 [开发指南](development.md)。

|职责|主要入口|依赖和状态归属|
|---|---|---|
|执行生命周期|`harness/loop.py`|接收策略、模型、注册表和会话；不认识搜索工具名称或页面缓存|
|通用契约|`harness/runtime.py`、`harness/context.py`、`harness/tools.py`|模型响应、取消、请求准备结果、工具结果；无产品界面依赖|
|执行事实与通用状态|`harness/session.py`|追加日志、排他、消息、待执行调用、运行状态、模型续接缓存|
|搜索组装与状态|`search/agent.py`、`search/state.py`|绑定提示、工具和策略；SearchSession 拥有页面、文档、研究与交付投影|
|搜索请求准备|`search/context.py`、`search/recovery.py`、`search/compaction.py`|正文外置、当前阅读状态、续接恢复、摘要窗口；最终按 Provider 实际请求计量|
|资料与研究|`search/documents/`、`search/progress.py`、`search/history.py`|快照和引用、研究局部更新、历史证据归属；不推进模型循环|
|具体模型|`providers/`|DeepSeek 与 ChatGPT 各自组装协议，共用 SSE 和用量解析；不创建搜索运行|
|终端产品|`terminal/`|配置、会话命令、展示、编辑器；调用搜索入口并观察公开结果|

`harness` 不导入其余三个包；Provider 不导入 Search 或 Terminal。Search 的组装入口选择 Provider，其余搜索模块依赖通用契约和搜索资料实现。Terminal 可选择 Provider 配置和认证，但不实现另一套循环。测试同时检查生产导入方向和干净进程中不加载 Search 的普通工具运行。

通用 `Session` 只维护执行事实和通用运行投影；`SearchSession` 继承其提交和重放机制，添加搜索状态。下文涉及页面、研究与引用时均指 SearchSession。磁盘仍使用原有版本 1 记录，源码路径不参与恢复身份，未批量改写旧会话。

## 会话、任务与运行状态

理解执行流程前，需要区分三个层次：

- **会话（Session）**保存一段连续对话。用户可以在同一个会话中提出多个问题。
- **任务（run）**对应用户提交的一个新问题。从开始研究到最终回答，属于同一个任务；显式继续未完成的研究时，也仍是原任务。
- **模型调用**包括研究决策和按需进行的独立压缩。研究决策可以返回最终答案或工具调用；压缩调用只生成继续工作摘要，不进入研究消息。`run.calls` 记录研究决策轮数，会话用量总计同时计入压缩请求及其失败尝试。

例如，用户提出一个问题，模型搜索一次、读取一次网页，再给出答案，这些步骤属于一个任务。用户接着追问时，系统创建第二个任务，并带上此前已完成的对话。任务不设固定模型调用次数上限；累计调用用于观察和用量核对，字符预算则约束每次请求的消息与工具定义大小。

Session 同时保存执行记录和供后续问题使用的完成历史，两者用途不同：

|数据|内容与用途|
|---|---|
|`Session.records`|按顺序保存执行事件，包括模型响应、工具结果、用量和任务结束状态。失败任务的记录也保留，用于恢复和历史回读。|
|最近任务的 `messages`、`pages`|当前研究正在使用的消息和页面快照，包含本任务已经取得的中间结果。|
|`SearchSession.messages`、`SearchSession.pages`|截至最近一次成功任务的对话消息和页面快照。新问题从这里继承历史。|

任务成功后，Session 才把该任务的消息和页面快照设为完成历史。任务失败或取消时，完成历史保持不变，未完成研究留在执行记录中。这样，用户既能继续失败的任务，也能直接提一个新问题，而不必让新问题自动继承失败过程。

Session 只为最近任务保留完整的运行消息和页面缓存；更早任务保留元数据和原始记录，避免为每个旧任务都保存一份完整对话副本。提交新问题后，之前未完成的任务仍可通过历史工具查阅，但不能再作为最近任务继续执行。

## 一次任务如何执行

### 从一次真实运行连接代码与数据

2026-10-29 使用现有 `page` 验收题：读取 Python 3.13 的 `c-api/memory.html`，只据这一页说明 mimalloc 小节，并区分“未记载”与“不存在”。这次独立重试经过两轮完整工具调用，在第 3 次模型请求遇到 `RemoteProtocolError`，没有最终答案。下面以其实际记录说明调用链；正常结束分支另以源码与离线测试说明，不将失败运行写成完整质量验收。

实测入口为 [scripts/run_live_case.py](../scripts/run_live_case.py) 的 `main`：读取题目与配置，创建 SearchSession 和 HTTP 客户端，调用 `run_agent`。普通产品入口是 [terminal/cli.py](../src/seekra/terminal/cli.py) 的 `main → run_question`，额外负责参数、磁盘会话选择、展示与退出码；其后进入相同的 Search 运行入口。安装后的 CLI 前缀另由 `test_cli_package.py` 验证。

|实际阶段|源码与协作接口|本次输入、结果和状态变化|
|---|---|---|
|组装搜索运行|[search/agent.py](../src/seekra/search/agent.py)：`run_agent → run_search → execute_run`|创建 DeepSeekModel，绑定 Tavily、SearchSession 与取消令牌；SEARCH_AGENT 提供搜索提示和请求准备。模型适配器不创建工具集合|
|开始任务|[harness/loop.py](../src/seekra/harness/loop.py)：`execute_run`；[harness/session.py](../src/seekra/harness/session.py)：`record`|记录 1 为 `run_started`；保存原问题、系统提示和模型/提示/工具身份。通用投影生成消息与 pending；SearchSession 初始化页面和交付状态|
|准备第一次请求|`prepare_search_request →` [search/compaction.py](../src/seekra/search/compaction.py) `prepare_model_turn →` [search/recovery.py](../src/seekra/search/recovery.py) `prepare_model_context →` [search/context.py](../src/seekra/search/context.py) `prepare_context/build_context`|记录 2 选中消息 `[0,1]`，实际请求内容 16,702 字符；返回 PreparedRequest，再记录 `model_started`。本次未触发摘要|
|模型选择网页读取|[providers/deepseek.py](../src/seekra/providers/deepseek.py)：`generate`；[providers/deepseek_stream.py](../src/seekra/providers/deepseek_stream.py)：`stream_chat_completion`|SSE 在 [providers/sse.py](../src/seekra/providers/sse.py) 解析；完整响应通过检查，记录 5 保存 `read_page(url)`。未完整收齐时不会执行工具|
|获取并提交网页|[harness/tools.py](../src/seekra/harness/tools.py)：`ToolRegistry.execute` → [search/tools.py](../src/seekra/search/tools.py) 的绑定处理函数 → [search/retrieval.py](../src/seekra/search/retrieval.py)：`read_page`|记录 6 为工具开始。Tavily 返回的快照先在页面副本中准备；ToolResult 携带正文和 page_updates，循环提交记录 7|
|生成稳定资料与阅读入口|[search/state.py](../src/seekra/search/state.py)：`_prepare_record/_apply` → [documents/store.py](../src/seekra/search/documents/store.py)：`prepare/sync`|记录 7 对应 `S1-P1`，给模型的结果是目录与开头正文，状态 partial，引用 `S1-P1@0-1630`。完整获取数据与模型阅读视图不同；编号和快照在提交后可回查|
|依据目录读取目标小节|再次准备请求后调用模型；`read_section` 由 Search 注册表直接调用 `Documents.read_section`|记录 8 请求为 32,916 字符；记录 11 选择 `S1-P1-H14`；记录 13 返回目标小节，引用 `S1-P1@32264-32634`，状态 success。此次本地读取没有再次访问 Tavily|
|准备利用新正文|再次通过同一请求策略|记录 14 选中 `[0,1,2,3,4,5]`，35,443 字符，包含刚取得的小节；记录 15 开始第 3 次模型请求。准备/发送不等于已确认模型收到，交付范围只在完整响应提交后确认|
|处理实际中断|`execute_run` 的异常分支与 `Session.record('run_ended', ...)`|服务断开且没有完整响应，记录 16 为 failed。工具已有完成记录，因此不标为工具结果未知；没有新 assistant 消息，也没有最终答案|

字符数是供应商请求内容的序列化度量，不是 token 数。文档编号与偏移只属于这次快照，下一次抓取可能不同。这个例子对应的页面明确限定为 Python 3.13；一次获取/读取成功不证明资料足以回答所有构建条件问题。

正常完成时，Provider 返回非空、无工具调用的 `stop` 响应，循环校验并调用 `Session.complete_model`。下一次循环发现最后消息已是最终 assistant 响应，追加 `run_ended(completed)`，再通知终端。正文通过 `on_content` 流式显示，完成通知不会重复输出答案。崩溃发生在完整答案保存后时，恢复只补完成标记；显式终稿工具则从已保存工具结果交付。这些分支有 `test_persistent_runtime.py`、`test_runtime_edges.py` 和 `test_answer_submission.py` 保护，本次远端实测没有走到它们。

取消由同一个 CancellationToken 传到循环、Provider 和搜索工具。取消发生在工具返回后，先提交已知结果；工具抛出异常而结果未知时才进入 needs_attention。参数错误或检索失败若已形成工具结果，会返回模型供其调整。预算整理仍无法容纳时暂停；摘要成功落盘后才替换窗口。详细规则见下文，不能把这些不同分支统称为“失败后重试”。

### 循环的通用顺序

`harness/loop.py` 中的 `execute_run` 按下面的顺序推进任务。每个已完成的关键步骤先写入 Session，再进入下一步。

1. **创建任务。**保存用户问题、系统提示和运行配置身份，从 Session 的完成历史建立本任务的消息和页面缓存。
2. **准备模型输入。**`search.compaction.prepare_model_turn` 协调消息投影和必要的独立压缩；`search.context.prepare_context` 与 `search.context.build_context` 按预算外置正文，工具集合不随研究进展变化。系统提示不随调用次数或进展版本逐轮变化，便于复用请求前缀；调用次数保留在执行记录与展示事件中，进展版本随研究进展结果提供。日期与恢复仍可改变系统提示。摘要单独调用模型，没有工具权限，正文不作为用户回答显示；用量进入会话总计。
3. **请求模型。**先记录 `model_started`，再调用 `Model.generate`。正文片段通过回调即时显示，用量统计单独记录。
4. **接收完整响应。**校验响应后保存 `model_completed`。如果模型返回工具调用，执行核心按返回顺序逐个执行；如果返回非空最终答案，则结束任务。
5. **执行工具。**每次调用前保存 `tool_started`，调用返回后保存 `tool_completed`，其中包含结果和页面快照变更。结果作为 `tool` 消息加入本任务，随后回到模型决策步骤。
6. **完成任务。**最终答案保存后，追加状态为 `completed` 的 `run_ended` 记录，更新完成历史，再通知展示层任务已完成。

模型决定使用哪个工具以及何时回答。系统提示建议模型优先每轮使用一个工具，以便根据刚取得的结果调整方向；执行核心仍支持一轮返回多个工具调用，并依次执行它们。

执行核心不提供调用次数上限。累计计数用于观察和用量核对，包含失败请求尝试；继续任务不重置计数。用户取消、请求失败和上下文字符限制分别按自己的条件停止执行。评估运行器需要限制实验时，通过外部取消机制结束，不改变产品的研究完成条件。

### 模型和工具怎样接入执行核心

`harness.loop.execute_run` 接收 AgentDefinition、Model、ToolRegistry、Session 和取消令牌，返回 `RunResult`。Search 的同名入口负责传入 `SEARCH_AGENT`。结果包含任务编号、状态、答案、错误说明以及原异常 `cause`。普通执行错误在记录后通过结果返回；参数或恢复配置不合法、存储不可用等错误可以直接抛出。

Model 的 `generate` 方法接收消息、工具定义、正文回调、用量回调和取消令牌，返回包含完整 assistant 消息及结束原因的 `ModelResponse`；可选的 `continuation` 携带仅在内存中保留的供应商续接数据，`provider` 携带模式和实际服务标识等可记录信息。执行核心检查取消与响应完整性后调用 `Session.complete_model`；该方法提交公开响应和续接摘要，成功后才发布内存续接。存储失败或残缺响应不会发布新的续接缓存。

需要续接投影的适配器实现可选的 `ContextModel`：`project_messages` 接收业务消息及与其一一对应的 `ModelContinuation` 快照，返回请求投影与缺失续接的位置；`request_content` 提供实际发送的消息和工具内容；`can_rebuild_context` 明确是否允许预算压力下重建。快照由 `Session.model_continuations` 从会话业务消息取得，适配器无需访问会话内部映射。快照的 `required=True` 且 `value=None` 表示原响应需要续接、但内存数据已经缺失。仅实现 `Model.generate` 的嵌入模型仍可直接运行。

ToolRegistry 保存工具定义及其执行函数。执行函数只接收一条完整调用，依赖在注册处绑定；返回字符串或 `ToolResult(content, record_fields)`，注册表统一为后者。状态更新字段不能覆盖 run_id、call_id、结果或终稿字段等执行元数据。执行核心提交完成事件，具体参数规则和业务处理由工具负责。默认集合包含 `tavily_search`、`read_page`、`list_documents`、`read_document`、`read_section`、`find_in_documents`、`update_research`、`list_history` 和 `read_history`。

`search.agent.run_agent` 和 `run_chatgpt` 分别准备模型及凭据，再调用共同的 `run_search` 组装工具并执行。需要自行提供模型的搜索调用可使用 `run_search`；自行提供搜索工具集合时使用 `search.agent.execute_run`。普通任务使用 `harness.loop.execute_run` 与 `AgentDefinition.from_prompt`，不会自动获得搜索工具或搜索恢复策略。

### 策略和状态怎样接入

`AgentDefinition` 包含提示的稳定身份、系统提示生成函数和请求准备函数。Search 保持原提示模板哈希，日期在生成提示时更新。准备函数返回 `PreparedRequest(messages, tools, selection, system)`；`selection` 是公开投影记录，不含供应商私有推理。通用基础策略保留完整交互，按 `Model.request_content` 检查字符预算；缺少续接或超限会暂停，不擅自裁剪或假装能够恢复任意业务。

Search 的上下文策略在 `prepare_search_request → compaction.prepare_model_turn` 后交付相同结果。恢复快照和摘要依赖搜索资料、引用及研究状态，因此仍属于 Search，没有把它们改名为“通用上下文”后隐藏这种依赖。

`SearchSession` 通过三个受限方法协作：`_prepare_record` 在写盘前规范化候选资料并验证版本；`_persisted_record` 保留已脱敏正文的坐标，避免二次替换；`_apply` 在提交后或重放时更新搜索投影。父类继续独占追加、flush/fsync、锁和错误状态。它不是可任意调用外部服务的插件回调总线。

搜索工具在页面副本上运行，将差异作为 `ToolResult.record_fields['page_updates']` 返回；循环不再复制页面或比较 URL。写盘失败时不发布候选页面和文档编号。`tool_started` 已提交意味着外部操作可能已经发生，不能据内存没有更新推断远端操作没有发生。模型完整响应提交后，SearchSession 才将该请求中的正文范围计为已交付。

### ChatGPT 订阅模型接入

`--provider chatgpt` 选择 `ChatGPTModel`，固定 `gpt-6.1-sol` 和 `medium`；默认 DeepSeek 入口不变。`chatgpt_auth` 通过 loopback 回调、PKCE、state 和签名 ID token 验证授权，必须取得 `chatgpt.tokens.use.direct` 权限才允许调用。每个账号标签分开保存 client ID 与已验证身份；host ID 跨登录复用，刷新在文件锁内串行执行，成功后原子替换令牌。Windows 文件内容由 DPAPI 加密，Unix 文件权限为 0600。OAuth URL、授权码、令牌与 ID token 不写入研究日志。

适配器把系统提示转为 `instructions`，把工具定义放入 `seekra` namespace，并将工具结果转换为 `function_call_output`。普通正文增量进入现有展示回调；只有 `response.completed` 且完整结果通过校验，才发布 assistant 消息和待执行调用。额度失败、`response.incomplete` 和缺少完成事件的流都会停止；失败响应已报告的用量仍保留。

无服务端会话存储：每次使用 `store=false`、`stream=true`，显式发送当前所需历史，不使用 `previous_response_id`。不透明的 reasoning 续接项仅在进程内保存，并计入预算；重载后缺失则复用现有业务上下文重建流程。`request_content` 提供供应商实际的指令、输入与工具结构，供发送前验证字符上限和生成摘要哈希，避免只按中间消息格式计数。模型、effort 和协议版本纳入恢复身份；订阅通道不发送不支持的输出 token 上限或采样参数。

### 显式终稿交付

需要由程序填入资料原文时，嵌入调用者可通过 `answers.register_answer_tool(registry, session)` 注册 `submit_answer`，再使用同一执行核心。默认 CLI 和 `default_registry` 不注册该工具；注册也不会强制模型使用它，普通无工具回复仍可结束任务。它是显式交付入口，不是默认答案质量门禁。

模型提交四类内容：`evidence` 选择已交付正文的引用和用于定位的短原句；`judgments` 保存解释或场景判断、依据序号及条件；`recommendations` 关联判断和未确定事项；`unknowns` 保存问题及其对答案的影响。序号都从 1 开始。程序在保存快照内定位原句，拒绝改写、翻译、不匹配或跨越未交付正文的引用；展示原段和提交引用中的上下文，不静默删掉其中的条件。翻译、转述和推断属于模型表达，不冒充程序提取的原文。

`layout` 将内容关系与展示顺序分开。模型用 `heading` 节点提供单行 `text` 和 1–6 的 `level`，用 `judgment`、`evidence`、`recommendation`、`unknown` 节点的 `index` 引用相应列表，可以先给结论、穿插原文，再解释建议。正文保存在对应内容项中，可使用 Markdown；没有额外的自由正文节点绕开这些关系。省略 `layout` 保留旧 Python 调用的固定栏目展示，已经保存的历史终稿也不重新渲染。

排版必须安排每项判断和建议各一次，并展示每组合并后的原文。未知项须单独安排，或关联到建议并在建议旁显示问题与影响，不能只保存在参数中而不交付给用户。使用 `layout` 时，每项建议至少关联一项判断或未知事项；关联判断的条件随建议就近展示。

建议的 `unknown_indices` 表示所有相关未知，包括要调查的问题；可选的 `dependency_indices` 则是其中真正决定方案能否成立的未确认前提，必须是前者的子集。例如，询问重置规则只关联调查目标；依赖重置规则的周期方案还要声明这项依赖。依赖列表非空时，程序在建议正文之前标明“候选方案（关键前提尚未确认，不能据此保证可行）”，并在旁边区分“尚未确认的前提”与其他“待核实事项”。这一状态也适用于不带 `layout` 的调用。旧调用可省略依赖分类，但省略不证明没有依赖，也不能据普通关联自动判定调查动作受阻。

程序检查关联、覆盖范围和已声明依赖的交付状态，不能判断分类是否正确或完整、建议是否满足条件，或模型是否把建议误写成原文要求。候选标记不会自动改写建议正文，也不能消除正文中的确定性承诺；模型仍须把未确认的关键依赖写成建议尚待满足的前提。没有候选标记的建议不因此获得可行性认证。

同一快照内重叠的引用合并展示，正文及预算只计算一次。排版中的多个证据节点若映射到同一原文，只在首次位置展开，后续不产生孤立的重复提示；判断和建议中的依据编号继续指向这份原文。原文块区分原段、引用上下文和原段的局部片段；范围恰好等于已保存文本的一个小节时，标为“保存文本中的完整小节”。这些标签描述展示范围，不证明网页提取完整、模型理解或整页已核实。

编译器先完整检查引用、索引、预算与排版覆盖，再返回可保存的终稿。失败作为工具结果交回模型，指出需要修正的引用、节点或遗漏内容；不交付部分终稿。成功调用必须独占该批工具调用，其结果落盘后才由执行核心交付并结束，无需再请求模型重写。若终稿输出失败，恢复时复用已提交的答案。普通模型消息仍按现有流式规则显示，这个入口不撤回先前已显示的说明。工具定义改变会触发现有恢复配置检查，不能把旧配置中的未完成任务静默续接为新协议。

### 工具调用为何要等完整响应后才执行

DeepSeek 使用 SSE 分段返回正文和工具参数。正文可以边接收边展示，但工具参数可能尚未收齐，因此不能收到片段就执行工具。

`providers/deepseek_stream.py` 按工具的 `index` 分别累计调用，检查 `id`、`type`、`name` 是否一致，并拼接 `arguments` 字符串。收到正常的 `finish_reason` 和 `[DONE]` 后，才返回整轮响应。正常响应有两种：`stop` 且正文非空、没有工具调用；或 `tool_calls` 且工具调用结构完整。缺少结束标记、输出截断、协议错误和网络中断都会终止本次执行，不自动重试或切换为非流式请求。

执行核心确认整轮调用结构有效后，才执行第一个工具。参数字符串是否为合法 JSON、字段是否符合工具要求，则由各工具在执行时检查；参数错误会作为工具结果交回模型。结构完整与参数有效是两个不同的检查阶段。

正文片段不会逐段加入模型历史。只有完整响应通过校验后，assistant 消息才进入任务记录。已经显示但未完成的正文可作为失败残稿保存，恢复时不会把它当作完整模型回答。

## 研究工具与取得的资料

研究工具提供不同类型的内容：搜索返回来源片段，页面读取返回提取文本，研究进展保存模型自己的判断、未核实事项和阅读目标。系统提示要求模型区分这些内容，并把搜索和页面正文当作外部资料，而不是改变任务的指令。默认运行开启 DeepSeek 原生 reasoning，使用 `high` effort；研究进展作为可回查的业务状态参与后续决策。

工具结果用 `operation` 标明操作，用 `content_kind` 标明内容类型，用 `status` 描述本次取得内容的情况：

|状态|含义|
|---|---|
|`success`|本次操作取得了预期内容或保存了笔记，例如搜索取得可用结果、页面读取返回整个提取快照。具体含义由操作决定，不表示答案中的结论已经核实。|
|`empty`|搜索请求正常完成，服务没有返回结果。|
|`partial`|本次读取只返回页面快照或历史记录的一段。|
|`failed`|参数、请求、返回数据或内容提取发生错误；搜索结果全部被过滤也属于失败。|

失败结果包含 `error_code` 和可读的 `error`。Tavily API 的 HTTP 错误与提取服务报告的目标页面失败分别记录；能够识别的 404、403、超时保留对应原因，未知原因不会被猜测为某种具体故障。结果不包含认证请求头或原始认证错误正文。

### 搜索：发现来源并取得片段

`tavily_search(query, max_results=3, include_domains=None)` 使用 Tavily Search 的 `basic` 模式，返回标题、URL、正文片段、整理结果的计数和实际请求参数。请求不要求 Tavily 生成答案，也不要求返回页面原文。

模型可以把 `max_results` 设置为 1–10；未提供该参数时，Seekra 使用默认值 3。这个值是候选结果数量的上限，实际返回数量可能更少。模型也可以通过 `include_domains` 限定最多 10 个域名；未提供该参数或传入空列表时，不限制域名。有域名限制时，请求明确使用 `include_domains_mode=restrict`，失败后不会自动改为全网搜索。

可选 `chunks_per_source`（1–3）控制每个来源的相关片段数量，与 `max_results` 的来源数量不同；省略时沿用 Tavily 默认。域名参数只接受明确的 ASCII 主机名，拒绝协议、路径、IP 和通配符，并整理首尾空白、大小写和重复项。非法参数在联网前返回 `invalid_arguments`；合法请求即使失败，也在结果中保留实际搜索参数。

`normalize_search_results` 对服务返回的数据进行运行时校验。无效 URL、类型错误的字段和空正文会被排除，缺失或空标题使用 URL 代替。文本只整理首尾空白和换行，不删除导航或改写正文。一次响应中，只有整理后的 URL 和正文都相同才会去重；同一 URL 的不同片段会保留。URL 不合并 `www` 或删除查询参数。资料展示层发现跨轮同一来源的完整片段完全相同时，返回 200 字符预览和 `duplicate_of`，仍保存新查询、独立获取记录及完整文本；不同内容继续按常规片段展示。这只压缩明确重复的显示，不合并不同 URL 或根据相似度删除条件。

### 页面读取：提取一次，按同一快照续读

模型调用 `read_page(url)`，通过 Tavily Extract 的 `basic` 模式取得指定公开 HTTP(S) 网页的 Markdown 文本并保存快照。该工具只接受 URL，每次调用都重新获取；已有快照的阅读由本地资料工具负责。

完整提取文本保存在执行记录中。模型先看到最多 1,800 字符的开头正文，以及从原文标题识别的目录；搜索片段与页面快照共享来源编号，但使用不同资料编号。模型可选择 `section_ref` 调用 `read_section`，或用 `read_document` 和 `find_in_documents` 阅读、查找同一快照，无需再次联网。每次常规正文阅读最多 6,000 字符，并尽量在自然段结束；`continuation` 指定继续读取所需的工具和参数。

`find_in_documents` 返回代表命中及连续原文窗口：优先命中的完整文本块，按空行分块但不拆开围栏内空行，交替补充同一标题区域的前后完整块。每个窗口最多 2,000 字符，每页最多 10 个窗口；完全包含在已返回窗口中的重复命中合并，跨窗口的命中继续查找。普通命中不向另一个标题扩展，查询本身跨标题时保留整个命中。表格与连续列表按原文保留，不补写表头、围栏或解释。

超长文本块或请求预算不足时，窗口围绕命中裁剪并尽量对齐行边界。`target_start/target_end` 保留选定目标，`start/end` 与 `evidence_ref` 只描述实际交付；`read_context_action` 可分页读完整目标，`read_action` 展开命中附近，`section_ref` 用于更宽的小节阅读。二次预算分配继续使用同一裁剪规则，不能把缩短后的窗口误报为完整。`complete` 仅指所选目标已全部交付，不保证语义充分；`scan_complete` 仅表示字面扫描结束。复杂嵌套 Markdown、HTML 和跨小节语义依赖不由这些结构规则自动解决。

正文结果包含快照身份、实际返回的 `[start,end)`、目标范围、剩余字符数和 `evidence_ref`。`complete` 只表示本次结果包含整个阅读目标，末页可以仍为部分读取。查找结果的 `match_range` 只定位关键词，正文 `start/end` 和 `evidence_ref` 则覆盖实际返回的附近内容。模型可直接复制 `evidence_ref` 保存进展，程序将其解析为来源、资料和范围；旧的 `reference` 对象仍可使用。

核对已有引用时，模型使用 `read_document(document_id=evidence_ref, view="reference")`。该模式只读取原引用范围，较长引用在此范围内分页。普通 `text` 模式可以把引用或命中位置作为锚点展开周边，返回新的范围和引用；扩大后看到的文字不属于原引用自身。

这些状态描述的是提取文本的读取范围。提取服务可能遗漏网页内容，也可能取得网站的回退页面，因此读完快照不能证明网页抓取完整或目标匹配。结果分别保留请求地址和服务报告地址；由于没有可核实的最终重定向地址，`final_url` 保持为 `null`。

重新提取前，获取层移除该 URL 的最新页面缓存；失败结果不能冒充一次新的成功提取。资料索引仍保留此前已经提交的快照，旧资料编号可以回查旧文本。获取层的字符续读实现保留用于旧记录及内部兼容，当前模型工具不暴露 `read_page(start)`；恢复配置检查也不会把旧工具协议静默转换成新协议。

URL 校验拒绝本地文件、带凭据的 URL、localhost 和非公网 IP 字面量。它只检查请求地址的形式，不验证提取服务内部的 DNS 解析或重定向路径；本机代理的 DNS 结果不参与这个检查。

### 研究进展：保留判断、缺口与阅读入口

模型通过 `update_research(base_revision, operations, goal=None, next_step=None)` 提交局部变化。程序保留未修改项目，为发现、缺口和阅读目标分配稳定编号，并根据实际交付范围校验正文引用。用户原始问题始终单独保存，模型概括不能覆盖它。

更新先在副本中校验，`tool_completed` 成功提交后才发布完整新状态；失败保留旧版。最新有效进展进入后续请求，较早版本标为历史。发现、条件、短原句和缺口可以跨上下文整理保留，但保存成功、引用有效都不能证明判断正确或任务完成。

模型根据原问题决定继续查证还是带明确未知项回答。程序提供累计交付、当前可见范围和未读位置的导航，不要求读完全部资料或清空全部缺口。字段、更新操作与导航的完整说明见 [研究进展](research-progress.md)。

默认工具集合不提供 `think_tool`，CLI 没有结构化笔记开关。历史笔记仍可查看；显式 Python `structured_think=True` 和 `legacy_tool_definitions` 保留兼容用途。旧的全量进展调用只用于兼容，升级为局部格式后不接受全量覆盖。

## 执行记录如何支持恢复

Python 调用 `Session()` 得到内存会话；`Session.create(directory)` 创建持久会话，`Session.load(path)` 从文件重建会话。CLI 默认在启动目录的 `.seekra/sessions/` 保存会话。

持久会话采用版本 1 的 JSONL 格式，每行一条记录，带有递增的字符串 `id`、记录类型 `kind` 和 UTC 时间。加载时按记录顺序重建状态。主要记录与执行步骤对应：

|记录|保存时机与内容|
|---|---|
|`run_started`|新任务开始，保存问题、系统提示和配置身份。|
|`model_started`、`model_usage`|请求发起前记录调用次数；接收用量时保存该请求最新的累计统计。|
|`model_completed`|完整模型响应通过校验后，保存 assistant 消息。|
|`tool_started`、`tool_completed`|工具调用前记录开始，返回后保存结果及页面快照变更。|
|`run_ended`|保存任务结束状态、错误说明，以及必要时的未完成正文。|
|`observation`|保存展示回调等观察环节的异常，不作为正常模型消息。|

显式恢复针对最近一个未完成任务。执行核心先检查模型身份、工具定义（包括描述）和系统提示模板是否与原任务相同；不相同时拒绝续接。更换密钥不改变这些身份。通过检查后，执行位置由已经保存的记录决定：

- **完整工具调用已经保存，但尚未开始执行：**执行待处理的工具调用。
- **工具结果已经保存：**保留该结果，继续剩余操作或请求模型，不重复执行已完成的工具。
- **工具已经开始，却没有结果记录：**标记为 `needs_attention` 并拒绝自动重跑，因为无法确认外部操作是否已经发生。用户需核实后提交新问题。
- **完整最终答案已经保存，但缺少完成标记：**只补写完成状态，不重新请求模型。
- **模型响应尚未完整保存：**保留此前已确认的步骤，重新发起下一次模型请求，不续接半截 SSE。

任务状态包括 `running`、`completed`、`interrupted`、`failed`、`paused` 和 `needs_attention`，分别表示执行中、已完成、已取消、失败、达到预算限制而暂停，以及工具结果未知而需要处理。进程崩溃或存储故障可能使磁盘记录停在 `running`；恢复时仍以实际保存的步骤判断，而不把这个状态当作任务仍在运行的证明。

### 文件写入与并发

Session 使用进程内锁阻止同一对象被并发操作；持久会话另用操作系统文件锁阻止多个进程同时写入。取得锁后还会检查文件是否已被追加，防止旧 Session 对象继续使用过期状态。

每次写入执行 append、flush 和 fsync。写入失败时立即停止后续执行和追加，不再尝试写任务结束标记，以免覆盖最初的存储错误。外部工具的实际操作与本地结果写入之间仍存在时间间隔，所以即使本地写入可靠，也可能留下“工具已开始、结果未知”的状态。

加载时若发现最后一行没有完整结束，只恢复完整记录。下一次取得排他锁后，先把末尾残片另存，再修复原文件尾部。完整行无法解析、必要结构损坏、记录类型未知或格式版本不受支持时，停止加载。

`run_agent` 为 Session 配置当前密钥的遮蔽规则，在写入磁盘的相关文本字段中替换意外回显的密钥。现场内存仍保留原内容，因此重载后的文本可能不同。隐藏的 `reasoning_content` 不进入执行记录，认证头也不作为执行数据保存。直接使用 `execute_run` 的调用者应自行为 Session 配置需要遮蔽的 `secrets`。会话文件包含本地运行资料，默认被 Git 忽略。

## 模型上下文如何控制大小

执行记录会随着会话增长，但每次请求不必把所有原文都发给模型。`search.context.build_context` 从任务消息生成本次请求所用的副本，这个过程称为上下文投影；原始记录仍保存在 Session 中。

默认 `max_context_chars=120000`，限制消息和工具定义序列化后的字符数。它不是 token 数，也不是模型服务的上下文窗口容量。

未建立压缩或恢复窗口时，请求从任务消息中选择内容。正文较长时，程序把较旧的工具正文换成带来源和快照引用的预览；完整资料与执行记录留在 Session。刚取得的资料至少进入下一次决策，最新有效研究进展保留。仅外置正文不改写系统提示；确需移出旧用户任务时，增加缺失前提与历史回读说明。

当前以硬上限的 80% 作为提前整理阈值。先按软目标外置旧工具正文，保留最新有效进展、工具失败详情和历史回读结果；不为了达到软目标裁短最新结果组。`update_research` 是可选的研究记录工具，不再控制搜索、阅读权限或旧轮次裁剪。空更新只保存现有研究状态。

外置后仍达到阈值时，`search.compaction.prepare_model_turn` 尝试独立压缩。切点选择完整 assistant 交互组的起点，至少保留最新调用与全部结果；近期交互优先控制在请求预算的四分之一以内，不能因此拆散调用与结果。摘要使用拟移出范围内的公开交互、此前摘要、当前进展和实际保留的恢复资料。对同一消息，优先使用最近一次完整模型响应确认交付的公开投影，避免先把正文新缩成预览再要求摘要保留其中条件；其余使用本次投影。不从更早归档批量读回全文，不传入私有推理。摘要明确覆盖范围，不能把范围内未见的信息写成整个任务从未取得。

调用前先投影候选窗口并检查是否有可释放空间。摘要输入遵守实际请求字符上限；输出预留取 8000 字符、预算八分之一、硬上限剩余空间及最低释放量约束中的较小值，不足 256 字符时不调用。最终必须释放至少预算十分之一的字符，并使下一次完整请求不超过硬上限。这些是避免无效调用的初始工程参数，不是已证明的最佳配置，也不保证每次压缩都回落到软阈值以下。

摘要请求只调用模型，不提供工具，不改变研究版本或主任务消息。`compaction_started`、`compaction_usage` 分别保存尝试与用量；完整摘要通过格式、长度和候选请求检查后，`context_compacted` 才保存新窗口。运行投影在该记录成功写入后生效。窗口包含切点、摘要覆盖说明和必要的研究状态，较新交互继续按原消息发送；摘要中的编号和转述不能单独算作正文交付。

失败、取消、超长或无足够收益时，不发布新窗口。失败用量仍计入总计，同一历史切点不反复自动请求摘要；明确恢复任务后可以重试。旧请求仍能通过正常正文裁剪放入硬上限时继续，否则暂停并保留旧记录。存储失败立即停止，不把未落盘摘要用于下一次请求。只有私有供应商续接字段造成超限时，仍可执行原有的明确上下文重建。

研究进展与摘要都是模型整理，程序的结构和范围检查不能证明语义完整、准确或任务已完成。摘要负责保留继续任务需要的过程信息，研究进展负责模型主动维护的发现与缺口；二者不互相认证。旧任务进展与已被替代版本仍标为历史状态。

请求中的进展正文保留保存时的发现、缺口、阅读目标、引用与关联，不随后续阅读重新填入动态导航和覆盖状态。当前任务最后一个工具结果附带 `reading_state`，标明所依据的 `run_id/revision`，集中提供当前 `navigation` 与 `document_coverage`；状态先纳入预算，正文裁剪完成后再按实际可见范围确定 `current_input`。累计交付和最早未交付入口仍由原交付规则计算。这使阅读状态的变化靠近请求末端，减少重写较早进展造成的前缀变化；末端状态移动、正文外置和压缩窗口变化仍可能影响缓存。执行记录保留当时完整工具结果，历史交付概况可回查。供应商重建窗口尚无工具结果时，当前导航仍由用户问题附带的恢复资料提供。

旧正文外置与任务裁剪后仍超限时，`delivery.fit_latest_batch` 为最新完整工具结果组共同分配剩余展示预算。它向 `Documents.delivery_view` 取得可缩减的资料视图，按相同正文上限选择能容纳的结果；正文范围、引用、完整性及续读游标由资料视图统一重建。目录或匹配条目不足以全部展示时，明确标记省略条目并提供继续入口；完整获取记录不变。必要身份信息、原始问题或最新有效进展仍不能容纳时暂停。当前分配策略及 80% 阈值都尚未充分调优。

每次请求先在副本中构造系统提示，写入 `model_started` 时才发布到运行消息；构造失败而未发送的下一轮提示不会留在内存状态中。投影记录同时保存整批分配度量和真实替换值，便于核对实际请求及暂停恢复。

### 原生 reasoning 与恢复

DeepSeek 带工具的 reasoning 请求需要回传历史 assistant 消息对应的推理字段。`DeepSeekModel.project_messages` 在预算选择前附加这些数据，原始业务消息保持不变。消息投影与整批分配按实际字段计算字符量，`context_selected` 保存正文和工具定义的真实投影、推理字符数与完整请求摘要；推理全文不会写入记录或传给展示事件。输入中的推理字段来自内存缓存，缓存随已保留消息更新，reset 时清空。

缓存缺失或必要供应商历史无法放入预算时，`search.recovery.prepare_model_context` 明确建立新的请求窗口。系统说明恢复方式，用户问题附带恢复资料 JSON：此前已保存的压缩摘要、最新研究进展、有限的资料目录与近期工具状态、带精确范围的正文摘录和历史可见回复。来源正文放在资料部分，继续按外部内容处理；原始用户问题和完整执行事实仍保留在记录中。正文摘录实际送入模型并取得完整响应后，才进入已交付范围。

恢复资料中的进展导航重新生成：累计交付沿用已确认记录，`current_input` 根据本次最终选入的正文和恢复摘录计算，最早未交付范围提供相应续读入口。恢复记录自身保持不变；后续窗口已经保存新的进展时，旧恢复进展和摘录退出输入，避免重复携带相互冲突的状态。

供应商重建窗口在 `model_started` 发布，独立压缩窗口在 `context_compacted` 发布；重载可复现已提交的业务窗口。需要继续的工具调用先按原规则执行。源数据、范围和恢复方式可复核；公开记录保存推理长度、用量及完整请求摘要，不保存推理全文，无法据此重建原推理过程。范围及恢复检查保证工程行为，不保证复杂研究中的信息保留和答案质量。

### 历史工具与清空边界

`list_history(cursor=0, limit=20)` 浏览当前会话的记录索引。`read_history(record_id, view="text")` 默认返回最多 6,000 字符的原始消息、失败原因或资料导航，不重复交付完整快照和事件包装。资料导航保留原小节、引用或命中的针对性阅读入口。`view="raw"` 显式读取原事件 JSON，底层 `Session.read_record` 也保留这一用途；单次可指定到 24,000 字符。文本视图和 JSON 视图分别标明偏移依据，按本视图的 `continuation` 续读，不混用字符位置。查询限定在当前 Session 内，不接受文件路径。

历史结果附带原任务的问题和当前状态。失败、取消任务的记录也可以回读，但残稿仍是未完成回答，模型笔记仍是模型判断。调用这些工具与调用研究工具遵循同一个执行循环，模型为选择和使用历史内容发起的请求同样计入任务预算。

`Session.clear()` 追加 `session_reset`，清空完成历史、页面缓存和可恢复任务。此后历史工具只能访问最近一次 reset 之后的记录；reset 本身及更早记录仍留在磁盘，但不能通过当前会话的历史工具读取。重载会话后也保持这个边界。CLI 的 `/new` 则创建另一个 Session，旧会话文件仍可显式打开。

上下文预算仅限制发送给模型的请求。原始 records 仍驻留内存，加载和列出会话时也会重建会话状态，因此它不是整个进程的内存上限。

## 终端交互、输出与取消

CLI 创建 `AgentDisplay`，把它的正文和事件处理方法传给执行核心。`on_content` 接收正文片段，`on_event` 接收模型、工具及任务的生命周期通知。展示层读取这些内容，不修改模型消息或工具证据。程序调用者也可以独立采集事件，或不提供展示回调而只读取返回答案。

正文增量写入 stdout，状态和诊断写入 stderr。普通模式显示研究进度、搜索词和来源等信息，`--verbose` 增加参数、结果、轮次、用量与计时。状态、标题、URL 和详细诊断的显示副本会遮蔽密钥及终端控制字符，流式正文按原文输出。界面的“正在思考”只表示模型请求正在进行，不代表启用了 DeepSeek thinking。

模型响应结束时，展示层先补齐正文换行，再展示工具状态。CLI 完成后不会重复打印已经流式输出的答案；仅在恢复已保存答案、没有收到新片段时输出返回答案。由于已显示的文字无法撤回，stdout 重定向文件中可能包含工具调用前的说明或失败残稿。

verbose 中的首段正文时间，从本轮模型调用事件起算，到首个非空白正文片段写入并 flush 后为止。它反映输出提交时间，不是精确的屏幕显示时间；片段数也不等于 token 数。

### 输入区与执行任务如何协作

交互式 REPL 使用 prompt-toolkit 的持续 Application。输入区始终保留，模型和命令处理通过 `asyncio.to_thread` 在后台执行；同一时刻只处理一个用户提交。运行期间可以编辑草稿，但 Enter 不会创建排队任务。粘贴通过 BracketedPaste 标记插入草稿，只有发送按键才提交，因此不依靠输入速度区分粘贴和逐字输入。

后台输出通过转接器送到界面线程的对话区；stdout 被重定向时，正文仍写入重定向目标。启动或切换会话时，界面从执行记录重建问答和未完成片段。对话区只保留最近 20 万字符，滚动和裁剪不改变会话记录或模型消息；流式追加时保留用户未处于底部的阅读位置。

`terminal/windows_input.py` 负责 Windows 按键转换。使用 VT 输入时，先启用 win32-input-mode，解码带修饰键的按键记录，再交给 VT 解析器处理文字、粘贴和鼠标序列；退出时关闭协议并恢复控制台模式。非交互 stdin 则在 EOF 时把整段文本作为一次输入提交。具体快捷键见 README。

### 取消如何传到执行核心

交互式 REPL 捕获 Ctrl+C：任务运行时调用 `CancellationToken.cancel()`；空闲时则清空草稿，草稿为空时退出。取消令牌设置取消状态，并通知已登记的网络操作。模型适配器尝试关闭已经取得的响应连接；执行核心和流接收器在检查点调用 `check()`，发现取消后抛出 `RunCancelled`，由任务错误处理记录中断。`RunCancelled` 继承自 `KeyboardInterrupt`。

CLI 为 `https://api.tavily.com` 挂载 `TavilyTransport`。该传输层在同步工具调用内执行一次可取消的异步 HTTP 请求，令牌取消时取消请求任务并关闭连接，覆盖等待响应头和读取正文的阶段，不关闭共享的模型客户端。每次检索独立管理连接，不跨请求复用 Tavily 连接。`run_agent` 把同一取消令牌传给执行核心和默认工具；搜索、提取将已确认的取消记录为 `cancelled` 工具失败结果，随后任务结束为 `interrupted`。恢复能读到取消原因，不会因为未清除的待执行调用自动重做这次检索；模型仍可在后续决策中选择重新获取。

Python 嵌入调用自备 HTTP 客户端时，需要同样挂载 `TavilyTransport` 才能中断阻塞的检索请求；普通同步客户端或自定义传输层的取消能力由调用者负责。直接组合 `execute_run` 与 `default_registry` 时，应向两者传入同一个取消令牌。

非交互运行中，Ctrl+C 产生的 `KeyboardInterrupt` 通过同步调用栈进入同一任务错误处理。界面退出时也会请求令牌取消，并等待后台处理器结束，不强行终止线程。

取消不承诺所有网络和操作系统阶段都有固定响应时限；模型连接尚未建立时仍可能等待返回或超时，本机域名解析等底层资源清理也可能影响退出时间。如果工具已经返回结果，执行核心会先保存实际结果，再处理取消，避免恢复时丢失已确认的操作。取消本地检索连接不保证远端服务停止处理或不计费。

## 错误如何影响任务

错误处理依据发生的位置以及已经保存的事实决定后续行为：

|错误情况|处理方式|
|---|---|
|模型请求或响应校验失败|停止当前任务，保存失败原因和必要的残稿。|
|工具返回结构化失败结果|把结果交给模型，由模型决定调整参数、尝试其他来源或回答。|
|工具已经开始，但抛出异常而没有结果|标记 `needs_attention`，阻止恢复时自动重跑。|
|必要上下文经过整理仍无法放入请求预算|标记 `paused`，保留已完成步骤。|
|取消或 `KeyboardInterrupt`|通常标记 `interrupted`；如果有已开始但结果未知的工具，优先标记 `needs_attention`。|
|Session 存储失败|直接抛出原错误，停止继续执行和写入；磁盘可能没有任务结束记录。|

展示故障需要区分正文交付和状态观察。`on_content` 的写入、刷新失败，以及模型响应后的正文补换行失败，使用 `ContentOutputError`，任务记录错误码 `output_error`。正文没有成功交付时，不能仅把它当作一条无关诊断忽略。如果完整最终响应已经保存，之后恢复仍可直接使用该答案。

`on_event` 抛出普通异常时，执行核心记录一次 `observation(display_error)`，停用该回调并继续研究；异常不会加入模型消息。这个规则不吞掉执行期间的 `ContentOutputError` 或 `KeyboardInterrupt`，它们仍按输出失败或取消处理。`AgentDisplay` 自身则会忽略诊断流的常见写入错误。

完成通知有单独的边界：`final_answer` 在答案和 `completed` 状态都已保存后才触发，只通知完成，不承担正文传递。此时回调异常只追加观察记录，不能把已完成任务改为失败或取消。报告任务失败时再次遇到输出或取消异常，也只追加观察记录，保留原失败原因。如果连观察记录都无法保存，则仍按存储故障向外抛出。

## 用量统计

`DeepSeekModel` 把服务返回的 `prompt_tokens`、`completion_tokens` 和缓存命中字段转换为统一用量。缓存命中率以同一次请求的输入 token 数为分母；缺失、非法或不一致的字段保留为未知。

一个模型请求可能收到多份累计 usage，Session 只用该请求最后一份统计计算会话总量，避免重复相加。界面显示已报告部分，并说明有多少次请求的统计不完整，不把缺失用量当作已确认的零消耗。

这些数字反映已经发生的请求，不能预测下一次请求的上下文大小，也不能替代本地字符预算。

## 关键选择与后续演进

本次选择保留执行机制并抽离搜索策略，而非仅移动文件或整体重写异步框架。循环、记录提交和取消已有实际消费者与回归保护；改变它们的所有权即可解除搜索依赖，没有必要同时更换协议或磁盘格式。普通任务与 Search 已经是两种请求策略；SearchSession 是通用 Session 的具体业务投影，暂不引入动态插件注册、事件总线或数据库仓库层。

外部研究对应以下选择。源码链接固定到研究快照；开发分支上的具体实现不等于每项功能已经稳定，更不代表适合全部移植。

|问题|依据|Seekra 当前选择|
|---|---|---|
|Harness 与产品界面|[Codex 官方平台说明](https://developers.openai.com/blog/codex-as-a-platform)、[OpenCode Server 文档](https://opencode.ai/docs/server/)|核心通过函数、返回值和回调与终端协作；尚无第二个远程客户端，不引入服务器协议|
|工具注册与执行政策|Codex [registry](https://github.com/openai/codex/blob/5fd36fc5502136160a28a6bebec3590b0df9179a/codex-rs/core/src/tools/registry.rs) 与 [orchestrator](https://github.com/openai/codex/blob/5fd36fc5502136160a28a6bebec3590b0df9179a/codex-rs/core/src/tools/orchestrator.rs)|注册、具体依赖与结果提交分工明确；新增本机执行工具时再设计真实审批和执行环境，不把 HTTP 工具包装成沙箱|
|Agent 配置与通用循环|OpenCode [agent.ts](https://github.com/anomalyco/opencode/blob/3d7eceb48d449f8bd7d995e154f7a24649d40a9b/packages/core/src/agent.ts) 与 [tool.ts](https://github.com/anomalyco/opencode/blob/3d7eceb48d449f8bd7d995e154f7a24649d40a9b/packages/core/src/tool.ts)|提示/请求策略、工具和模型显式组合，不复制其 Effect 服务与插件体系|
|多任务运行所有权|OpenCode [run-coordinator](https://github.com/anomalyco/opencode/blob/3d7eceb48d449f8bd7d995e154f7a24649d40a9b/packages/core/src/session/run-coordinator.ts)、[subagent-job](https://github.com/anomalyco/opencode/blob/3d7eceb48d449f8bd7d995e154f7a24649d40a9b/packages/core/src/session/subagent-job.ts)，Codex [agent/control](https://github.com/openai/codex/blob/5fd36fc5502136160a28a6bebec3590b0df9179a/codex-rs/core/src/agent/control.rs)|未来在独立 Session 之上管理子任务、结果归属和取消；当前单会话排他保持不变|
|上下文与执行隔离|Claude Code [子代理](https://code.claude.com/docs/en/sub-agents)和[沙箱](https://code.claude.com/docs/en/sandboxing)官方说明|独立上下文不等于操作系统隔离；本次没有实现 Sandbox，也未把官方行为说明冒充其完整核心源码|

Coding Agent 接入时，需要具体的文件/命令工具、工作目录和执行政策；它可以复用循环、Model 和 Session，但搜索资料恢复策略不能直接当作 Coding 恢复。Multi-Agent 还需要委派输入、独立上下文、父子关系、等待/取消/回收和结果交付协议；目前没有这些功能。Workflow 可以在循环之外编排运行，model routing 可以在模型选择处发展，但都应等真实场景出现后再设计，不能仅根据未来名称预建目录。

剩余边界：消息和运行投影仍使用字典，协议仍采用当前工具调用消息形态；每个 Session 仍仅支持一个活动执行；基础通用请求策略不会自动摘要或重建任意业务。Search 中的恢复、正文预算和研究状态仍需要协作，这些实际依赖被保留并明确组织，没有宣称已经实现完全通用的上下文引擎。
