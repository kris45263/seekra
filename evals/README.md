# 评估案例与精选证据

这里分别维护可重复检查的能力、运行所需固定资料，以及需要复查具体现场的少量证据。当前实现见 [架构](../docs/architecture.md)，判断方法见 [验证指南](../docs/validation.md)，问题结论见 [研究发现](../docs/research-findings.md)。

## 选择运行入口

|入口|用途|输出的解释范围|
|---|---|---|
|[cases.json](cases.json) 与 [run_live_case.py](../scripts/run_live_case.py)|直接回答、指定页阅读、失败反馈、版本与证据边界四项基础检查|答案和操作摘要；不能重建全部实际请求|
|[check_documents.py](../scripts/check_documents.py)|正文定位、条件保留、版本比较、引用纠正十项能力检查|实际公开请求、执行记录、取得与交付范围；固定资料与真实检索分别标记|
|[claim-boundaries.json](claim-boundaries.json)|20 个固定语义正反例，供人工核对或校准评估者|单项判断能力；不能替代完整回答核对或自主研究|

```powershell
uv run python -X utf8 scripts/run_live_case.py --list
uv run python -X utf8 scripts/check_documents.py --list
uv run python -X utf8 scripts/run_live_case.py --case page --output evals/runs/page.json
uv run python -X utf8 scripts/check_documents.py --case long-section --output evals/runs/long-section.json
```

`--list` 不调用服务。其他运行读取项目 `.env`，调用已配置的 DeepSeek；真实检索案例还调用 Tavily，会产生费用。运行器使用当前代码和默认参数，不声称复现某个历史模型的完整行为。输出文件必须不存在。

`runs/` 是忽略跟踪的过程产物目录。每次运行结束并完成判断后，删除没有后续用途的产物；仅需要复查具体现场时才精选保留。输出未评分，`completed` 只表示执行结束。

## 能力与判据

|案例|需要检查的能力|重要判据|
|---|---|---|
|`simple`|无需研究时直接回答|不强制调用搜索或阅读工具|
|`page`、`live-page`|给定 URL 后定位指定内容|依据实际版本与小节，区分未读、未记载与不存在|
|`failure-retest`|获取失败后的反馈|报告实际失败，不编造正文或擅自换来源；站点返回回退页面时单独判断目标匹配|
|`boundary`、`migration`|自主发现并组合版本化来源|分别核对捆绑、默认、必需，或启动方式、可变全局量和常量；来源支持对应版本|
|`long-section`|跨窗口阅读并保留末尾条件|固定虚构资料中的系统、许可、准备、期限和账户限制均应得到处理|
|`no-headings`|无可靠目录时使用字面查找|审批主体、个人信息移除、链接期限准确；本题明确要求查找，不检验自主选择工具|
|`simple-default`|取得充分依据后停止|回答版本与平台条件，不因仍有来源可读而无增益扩展|
|`wal-readonly`|多来源条件组合|目录权限与 WAL 条件分开；另一个进程可修改时，不把 immutable 当作无条件解决方案|
|`robots-status`|区分规范中的错误类别|404 与 503 的规定不同；结合本题无缓存前提|
|`version-comparison`、`version-source-comparison`|同一主题跨版本核对|每项结论绑定对应来源；后者明确给了文档源码回退入口，不与前者当作相同发现难度|
|`reference-review`|核对引用自身，再展开上下文纠错|初始范围不含完整的 24 小时条款；扩展后引用须包含条件及例外|

正文案例的 `expected` 是人工检查点，不发送给模型。外部网页与服务会变化；运行时对照实际版本的原文，不把历史检查表当作永远正确的网页快照。

语义正反例包含完整规则、有限阅读和历史执行三个资料范围，`expected/rationale` 留给评审者。合理的提前安排建议与原文义务、调查动作与未确认前提分别判断。若使用模型评估，隐藏判准，记录漏判、误判和新增错误，再判断它是否适合完整回答任务。

## 固定引用夹具

[fixtures/reference-review.json](fixtures/reference-review.json) 供正文运行器的 `reference-review` 使用，保存一份 RFC 9309 历史会话的 45 条原始记录及来源文件哈希。模型在独立新任务中核对 `S1-P1@14667-15078`，先读原范围，再展开同一快照；它不需要重新访问网页。

夹具保留事件顺序、记录编号和快照文本，以便重建相同引用。更新资料时需重新核对问题中的引用、缺失条款及预期纠正范围，不能只替换正文。保存文本是测试输入，不是仓库维护指令。

## 精选现场及保留理由

|记录|未来复查用途|证据边界|
|---|---|---|
|[进展压力现场](evidence/progress-feedback-meridian-present-5-20261004.json)|修改进展或恢复机制时，对照已保存原句、实际请求与终稿措辞；记录 73/93/111/123 与响应 127 可定位变化|2026-10-04，固定资料、预置进展、32k 字符；不是当前稳定性或效率基准|
|[字段说明对照](evidence/claim-field-guidance-20261007.md)及其[完整输入输出](evidence/claim-field-guidance-20261007.json)|修改语义指导前复查已尝试的唯一变量、改善与反例，避免把局部成功当作稳定修复|2026-10-07，八次定稿请求、固定资料；不是完整研究循环|
|[完整字段复核](evidence/enumerated-answer-review-20261007.md)及其[完整记录](evidence/enumerated-answer-review-20261007.json)|评估复核方案时区分字段覆盖与判断正确，保留正确对照和失败请求|2026-10-07，八次请求、七次完整复核；不证明其他模型或方法无效|
|[依赖声明现场](evidence/advice-dependencies-live-20261007.json)|修改显式终稿时复查结构校验已通过、但未知前提仍被遗漏的完整循环|2026-10-07，两次循环、八次模型请求、固定虚构资料；无 Tavily 调用|
|[订阅协议现场](evidence/chatgpt-subscription-smoke-20261013.json)|维护适配器时理解缺失 Content-Type 和 output_item.done 完整输出的兼容处理|2026-10-13 的精简现场记录；不代表服务稳定性、长上下文或真实刷新验收|

JSON 中的提示、工具定义、实验代码和源文件名描述当时现场；它们不是当前执行指令、待办或受支持的脚本入口。源码指纹与公开输入帮助限定证据，不包含供应商内部状态，不能保证重跑得到相同结果。
