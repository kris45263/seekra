# 搜索评估

[cases.json](cases.json) 保存可复用任务和人工判据，[fixtures/](fixtures/) 保存固定资料。评估通过产品的 `Seekra.run` 执行，不自动评分。案例名称表示检查目标，选题以当前问题为准，不需要每次运行整套。

## 运行

```powershell
uv run python -X utf8 scripts/run_live_case.py --list
uv run python -X utf8 scripts/run_live_case.py --case no-search
uv run python -X utf8 scripts/run_live_case.py --case offline-requirements
uv run python -X utf8 scripts/run_live_case.py --case wal-readonly
```

运行必须显式指定 `--case`；`--list` 不读取密钥、不调用服务。模型和配置与产品一致，支持 `--config`、`--env-file`。

- `live` 使用真实模型与产品检索服务；`no-search` 仍需加载产品要求的密钥，但预期不发出检索请求。
- `fixed` 使用真实模型，检索由运行器提供固定正文或明确错误，不访问 Tavily。它不检验搜索服务或真实网页提取能力。

## 案例与输入条件

|案例|输入条件|检查目标|
|---|---|---|
|`no-search`|无资料任务|无需检索时直接完成|
|`retrieval-failure`|固定工具错误|失败后如实说明、不编造正文|
|`offline-requirements`|给定长篇虚构资料|分散条件、账户范围与期限|
|`unstructured-policy`|给定无标题虚构资料|自行定位审批、匿名化与有效期|
|`citation-support`|给定 RFC 快照及待核对断言|原引用是否支持结论、相关条款与例外|
|`page-evidence-scope`|给定网页与小节|结论是否超出指定来源|
|`mimalloc-build-conditions`|自主发现官方来源|CPython 3.13.0 构建支持与默认行为|
|`multiprocessing-default`|自主发现官方来源|简单事实检索和停止|
|`multiprocessing-upgrade`|自主发现两个版本来源|配置变化、全局状态与实际建议|
|`wal-readonly`|给定两个官方页面|组合条件与 immutable 前提|
|`robots-http-status`|给定规范 URL|状态类别与 MAY/MUST 区别|
|`free-threading-versions`|给定两个版本页面|对象范围和触发描述的版本绑定|

固定长资料使用重复背景段落构造定位负担，不代表自然网页分布。给定来源题与自主搜索题分别判断；不要合并成一个“搜索准确率”。

## 案例字段

|字段|作用|
|---|---|
|`id`、`revision`|案例身份与修订日期；题面或判据变化时更新修订值|
|`capability`|检查能力|
|`mode`|`live` 或 `fixed`|
|`source_access`|无来源、给定 URL、自主发现、给定固定正文或受控失败|
|`question`|实际发送模型的任务|
|`fixture`、`url`|固定正文文件及其工具 URL|
|`retrieval_error`|固定错误响应；与 `fixture` 二选一|
|`expected`|人工核对的必要结论和行为|
|`references`、`reviewed_on`|判据来源、对应章节、支持关系和最近核对日期|
|`review_notes`|输入限制及判读注意事项|

只有 `question` 作为用户消息发送。固定正文或错误通过工具返回；能力标签、参考判据、来源核对说明不注入模型。`references` 是评估者核对入口，不能证明模型实际获取了这些来源。

## 诊断与摘要压力

默认不保存运行记录，临时 checkpoint 和工作区在运行后清理。需要复查时指定一个不存在的输出文件：

```powershell
uv run python -X utf8 scripts/run_live_case.py --case offline-requirements --compaction-messages 5 --output evals/runs/offline-compaction.json
```

记录包含当时完整案例定义、模型档案、事件、最终答案、来源正文、固定正文哈希及停止条件。它不是完整 HTTP 抓包，也不包含全部 SDK 内部状态和推理正文。保存记录的同时说明代码/依赖版本与人工结论；运行器不会自动认证答案。

`--compaction-messages` 只用于有固定正文的案例，使用原生 SummarizationMiddleware，最近保留 2 条消息；触发阈值至少为 4。必须从内部摘要事件确认实际触发，不能仅凭参数判断。压力条件与默认配置分别记录。

`--timeout` 和 `--max-model-calls` 可按诊断需要显式设置，默认均不启用。调用限制在第 N+1 次模型请求前停止，包含摘要调用；超时取消实际任务。它们不改变产品的完成逻辑。

## 判读

先核对题面条件、实际来源版本、提取范围和参考判据，再看答案。记录三部分：

1. **执行结果**：完成、模型/工具失败、取消或评估器中止。
2. **答案判读**：逐项满足、错误、遗漏或因资料不足无法核实；给出具体断言与来源，不按关键词命中评分。
3. **行为观察**：来源发现、重复搜索/读取、是否取得必要内容、停止情况；不以调用次数单独判断好坏。

资料取得不足时，如实承认未知可以在诚实性方面合格，但不等于事实任务已完成。资料版本不符或参考页面已变化时先修正条件，不强行算作模型正确/错误。执行中止的任务没有完整答案分数；评估器自身问题另列。

引用必须支持相应断言。必要条件不能擅自变成充分条件，建议不能冒充来源义务，部分未见不能扩大为不存在。回答正确但没有完成要求的来源核验，也不能认定该项能力通过。

当前题集没有经过难度校准，不发布合并总分。案例重写后的结果与历史题目分别记录；真实运行结论维护在本地验证记录中。
