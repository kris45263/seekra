# 真实案例与实验证据

这里保存用于解释能力、反例或设计选择的案例和证据，不是每次开发运行的日志集合。当前实现见 [架构](../docs/architecture.md)，判定方法见 [验证指南](../docs/validation.md)，结论见 [研究发现](../docs/research-findings.md)。

## 用例与运行

[cases.json](cases.json) 保存直接回答、指定页读取、失败反馈、复杂版本问题和两种笔记用例的精确问题。运行真实服务需要有效的项目配置，会产生调用费用。

```powershell
uv run python -X utf8 scripts/run_live_case.py --list
uv run python -X utf8 scripts/run_live_case.py --case page --output evals/runs/page.json
```

`runs/` 是忽略跟踪的运行产物目录。复现指定实验：

```powershell
uv run python -X utf8 scripts/probe_capabilities_20260910.py --case think-pair --output evals/runs/think-pair.json
uv run python -X utf8 scripts/probe_capabilities_20260910.py --case boundary --output evals/runs/boundary.json
uv run python -X utf8 scripts/probe_extract_20260910.py
```

Extract 脚本写入 `evals/runs/extract-match-20260910.json`。各脚本拒绝覆盖已有输出，再次运行前选择新路径或移走已有运行产物。Probe 名称中的日期标识实验方案，不会固定实时外部数据。

## 证据索引

|研究问题|记录|用途与局限|
|---|---|---|
|版本指令、证据供应、工具可用性|[2026-09-07 对照](evidence/2026-09-07-experiments.json)|早期 snippet-only 基线；固定历史与单项变化，不是当前全流程回归|
|读取、失败与笔记协议|[能力基线](evidence/2026-09-08-live.json)|包含修复前 failure 与修复后 failure-retest；复杂题只部分通过|
|指定页读取|[页面复测](evidence/page-retest-2026-09-08.json)、[流式读取](evidence/page-streaming-20260908.json)、[完整请求与范围](evidence/page-20260910.json)|能取得及续读目标快照；不认证网页完整或所有答案细节|
|404 错误反馈|[失败复测](evidence/failure-retest-2026-09-08.json)|错误事实可见；回答附加推断不一定有证据|
|手工页面检查|[原始页面验收](evidence/manual-page-20260908.json)|保留当时记录，不重新解释为当前版本通过|
|搜索参数与来源|[结构取样](evidence/tavily-structure-sample-20260908.json)、[参数机制](evidence/tavily-mechanism-probe-20260908.json)|单题小样本；数量、域名和片段的区别|
|模型参数使用|[数量控制](evidence/search-controls-live-20260908.json)、[明确域名](evidence/search-domain-live-20260908.json)|证明能力被使用，不证明参数选择最优|
|噪声与内容消费|[抓取计数](evidence/tavily-noise-probe-20260908.json)、[原文/清理对照](evidence/noise-answer-pair-20260908.json)|没有观察到删除界面行的准确率收益；每组一次|
|结构化笔记引用|[固定会话对照](evidence/think-pair-20260910.json)|首请求仅 schema 不同；四组都续读，一组引用两次失败|
|引用定位与自主使用|[实验分析](evidence/think-analysis-20260912.md)、[原始请求](evidence/references-original-20260912.json)、[正文编号](evidence/references-body-id-20260912.json)、[自主选择](evidence/note-choice-20260912.json)|编号副本可能帮助正确引用；单页续读六次均未主动调用 think，不证明复杂任务无收益|
|复杂题的具体归因|[实际完整轨迹](evidence/boundary-20260910.json)|来源、范围、答案可逐项核对，仍有范围扩大|
|缺失路径的首页回退|[HTTP/Extract 对照](evidence/extract-match-20260910.json)|本机直接 HTTP 也返回首页；未确定服务端内部原因|
|CLI 与取消|[独立进程](evidence/cli-entry-20260910.json)、[PTY 观察](evidence/terminal-cancel-20260910.md)|真实连续问答及一次生成中取消；工具阻塞阶段未覆盖|

[2026-09-10 实验报告](evidence/review-20260910.md) 给出逐组结果、具体错误与未知因素，是上述数据的人工判读。JSON 内 `judgment` 的“待人工核对”可能是采集时的占位值，不与报告中的判定混淆。

## 历史基线与可复现性

- 2026-09-07 业务基线为 `0dcf026`。正文替换实验同时改变内容长度和实验标签，不能区分两者影响；工具对照的首轮读取被本机代理 DNS 检查误拦截，作为无效读取试验保留，有效重测与它分开。读取工具可用性是在模型已有目标 URL 的上下文下测试，不证明从零发现链接。
- 2026-09-08 读取版本基线为 `bc04d6c`，开发中修复了 provider 已知失败原因丢失；流式和搜索控制随后进入 `1046a31`。各记录按当时参数及修复前后标记解释。
- 2026-09-10 实验在 `c125c02` 加统一 display 回调修改的工作区运行，调用同一生产 Agent。记录当时未保存精确源文件哈希，不能声称仅凭该 SHA 可逐字重建整个实验工作树；模型配置、请求与正文以 JSON 为准。

网页、provider 与模型均会变化。相同问题重跑不保证相同输出；完整请求记录也不是 provider 内部状态快照。显式模型笔记和外部文本是待判断资料，不是仓库维护指令。记录不包含认证头或隐藏 reasoning_content；原始回答中的事实不代表项目认可。
