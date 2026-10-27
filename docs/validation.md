# 验证方法与能力边界

验证应能回答当前改动涉及的问题。工程行为、真实模型行为和答案质量分别判断；运行命令见 [开发指南](development.md)，可重复案例见 [评估入口](../evals/README.md)。

## 工程行为

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

测试替换外部服务，在真实执行核心、解析器或存储边界上检查行为。测试文件之间可能共享模拟响应助手；名称相近的测试不一定覆盖同一失败边界。

|修改范围|相关测试|需要证明的行为|
|---|---|---|
|循环与工具分派|`test_agent.py`、`test_harness.py`、`test_tool_stream.py`|根据前轮结果继续、调用结果配对、参数错误回传、完整响应前不执行工具|
|模型协议与 usage|`test_deepseek_stream.py`、`test_chatgpt.py`、`test_chatgpt_auth.py`、`test_usage.py`|增量解析、正常结束、连接释放、身份与刷新、缺失用量不伪造|
|检索与取消|`test_search_results.py`、`test_search_controls.py`、`test_retrieval_edges.py`、`test_tavily_cancellation.py`|参数透传、失败类别、快照与传输层取消|
|正文与交付|`test_documents.py`、`test_find_context.py`、`test_delivery.py`|同快照续读、完整文本块与标题边界、范围与游标、整批裁剪后的实际输入和原目标回读|
|研究进展|`test_research.py`、`test_progress_updates.py`|局部更新、条件保留、已交付引用、发布与重载|
|预算与恢复|`test_checkpoint.py`、`test_compaction.py`、`test_reasoning.py`、`test_persistent_runtime.py`、`test_runtime_edges.py`|旧正文外置、压缩取消/失败/提交、摘要重载与费用、供应商续接、未知结果阻止重跑|
|历史与终稿|`test_history_review.py`、`test_answer_submission.py`|原任务归属、当时进展、原文与排版校验、保存后交付|
|CLI 与会话|`test_session.py`、`test_cli_sessions.py`、`test_cli_package.py`、`test_repl.py`|会话隔离、独立目录入口、配置来源与恢复命令|
|显示与输入|`test_display_events.py`、`test_stream_display.py`、`test_input_editor.py`、`test_repl_paste.py`、`test_windows_input.py`|正文与诊断故障分离、草稿、粘贴、按键转换|
|验收与诊断脚本|`test_live_case.py`、`test_check_documents.py`、`test_streaming_check.py`|基础验收取消后保留已提交工具摘要；正文运行器区分研究与摘要请求并核对实际请求；无工具诊断的追问、错误、取消和输出故障；不认证产品研究能力|

涉及历史、进展或压缩时，检查实际选入下一次请求的内容。工具成功、磁盘保存、准备选择和确认交付是不同事件。测试用假模型可以证明交付和控制流程，不能证明真实模型会理解或采用这些内容。

存储与恢复检查应覆盖与改动相关的提交失败、取消或进程退出边界。已完成工具不应重跑；已开始却没有结果的工具应阻止自动恢复。跨进程恢复和文件锁不能仅用同一对象的内存状态证明。

## 实际环境

输入解析器和 pipe input 可验证分批粘贴与草稿提交规则；控制台注入可验证 Windows 按键转换。这些检查不能替代实际终端上的物理组合键和剪贴板操作。人工验收依次检查：粘贴不自动发送、运行中编辑不排队、换行键不提交、结束后 Enter 只提交一次。

取消检查区分模型生成、工具等待响应头和接收正文。关闭本机测试连接不证明所有 DNS、TLS、代理或远端服务均在固定时间内停止。服务连通检查只证明该次请求的相应路径可用。

## 真实模型与答案质量

运行前明确案例检查点。使用独立会话，或明确标记固定资料、历史种子和预置状态；不能将预置目标或明确要求某个工具的任务称为从零自主发现。当前 DeepSeek 入口默认开启 reasoning，历史实验按记录中的模型和配置解释。

答案逐项核对来源、版本、实际读取范围、条件、例外和建议前提，标记正确、遗漏、错误或无法核实。引用格式合法、`completed`、无异常退出都不是质量通过条件。无法取得页面与取得页面后误读要分别归因。

对照实验固定共同输入、模型、预算和非目标变量，记录实际发送内容及差异；自主轨迹分叉后分别判断。少量样本用于定位问题，不据此估计稳定成功率。无效输入和失败请求需进入本次判断，不能通过只选成功样本得到收益结论。

`claim-boundaries.json` 中的 `expected` 和 `rationale` 是人工判准，不发送给待评估模型。单项分类、完整答复核对和自主研究属于不同能力层次，应分别报告结果。

## 记录与停止

新运行写入 `evals/runs/`，使用独立输出路径。它是忽略跟踪的过程产物位置，不是永久归档承诺。任务结束后判断是否仍需复查这一次现场；需要保留时，连同输入、输出、判准、配置和限制保存到合适位置。已作为运行输入使用的固定资料放在 `evals/fixtures/`。

记录不得包含凭据或私有推理全文。公开请求、选入的正文范围与请求摘要可用于核对客户端交付，但不能还原服务内部状态。正文运行器保留公开请求投影和执行记录；基础运行器只有摘要，不能承担完整请求对照。

相关检查通过后，没有新改动、失败或未解决疑点，就停止扩大验证。“尚未验证”界定已有证据，不自动成为补测任务。当前仍需注意的模型行为见 [研究发现](research-findings.md)。
