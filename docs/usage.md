# 使用说明

安装与首次运行见 [README](../README.md)。本页说明配置、会话和终端操作。

## 配置文件

默认读取启动目录下的 `seekra.toml` 和 `.env`。可以显式指定位置：

```powershell
uv run seekra --config C:/path/seekra.toml --env-file C:/path/.env --repl
```

模型名称、端点和选项放在 TOML，密钥放在 `.env` 或进程环境。显式 `.env` 中的同名值优先，不展开 `${...}`。示例见 [seekra.example.toml](../seekra.example.toml) 和 [.env.example](../.env.example)。

|配置|用途|
|---|---|
|`app.default_model`|新会话默认使用的命名档案|
|`app.capability`|当前为 `search`|
|`app.data_dir`|会话和工作区根目录；相对路径以 TOML 所在目录为基准|
|`models.<名称>.provider`|`deepseek`、`moonshot` 或 `zai`|
|`adapter`|使用 `litellm`|
|`model`、`base_url`|实际型号和服务端点|
|`api_key_env`|保存该模型密钥的环境变量名|
|`max_input_tokens`|提供给 SDK 的输入 token 预算，至少 4,096；按实际型号容量配置|
|`max_output_tokens`|单次模型输出上限，默认 8,192|
|`options`|厂商参数：`thinking`、`reasoning_effort`、`temperature`、`top_p`、`extra_body`|
|`search.api_key_env`|Tavily 密钥变量名，默认 `TAVILY_API_KEY`|

配置示例使用 64,000 token 输入预算。选择型号时应核对其容量和输出余量；配置校验不替代厂商容量检查。SDK 根据 model profile 管理摘要。服务端不支持的选项可能报错，程序不会自动关闭思考选项。

每个会话绑定一个档案。修改该档案或相关 SDK 版本后，已有会话可能因兼容指纹变化而拒绝打开或运行；使用原配置和依赖，或新建会话。轮换同一变量中的密钥值不改变模型档案指纹。

DeepSeek 已有有限真实执行记录；Kimi 和 GLM 目前检查了路由、国内端点配置、工具绑定和模型构造，其实际型号响应与思考续接仍需验证。

## 命令与会话

```powershell
uv run seekra "问题"
uv run seekra --repl
uv run seekra --model kimi-research --repl
uv run seekra --sessions
uv run seekra --session 会话ID --repl
uv run seekra --resume
uv run seekra --continue 会话ID
```

|交互命令|作用|
|---|---|
|`/new [档案]`|新建独立会话，可选择模型|
|`/sessions`|列出会话 ID、模型和标题|
|`/open ID`|打开会话；接受至少 8 位的唯一 ID 前缀|
|`/resume`|打开最近会话|
|`/continue`|继续当前会话中未完成的执行|
|`/help`、`/exit`|查看帮助、退出|

打开会话只加载对话展示，不主动调用模型。`/continue` 按 checkpoint 继续待执行节点；存在未完成任务时，需要先继续或新建会话，才能提交新问题。切换模型使用新会话。

## 输入与输出

Enter 发送；Shift+Enter 或 Ctrl+J 换行。粘贴中的换行不会自动提交。任务运行期间可以编辑草稿，结束后再发送。Ctrl+C 在运行时取消任务，空闲时清空输入，再次按下退出。Windows Terminal 可使用 Shift 拖选和 Ctrl+Shift+C 复制。

管道输入以 EOF 为一条完整消息，其中 `/new` 等文本作为问题内容。中间文本、工具状态和用量显示到 stderr，确认完成后的最终回答写到 stdout。`--verbose` 显示工具参数。内部摘要和推理正文不作为进度文本展示，摘要调用的已报告用量仍计入总量；缺失用量单独标记。

需要全局本地命令时，可运行 `uv tool install --python 3.13 --editable .`，之后使用 `seekra`。移动源码或调整依赖后需重新安装；普通 `uv run` 使用项目环境。

## 本地数据与恢复

默认数据根目录为 `.seekra/v2/`：

- `checkpoints.sqlite` 保存图运行状态。
- `sessions/` 保存会话目录和运行锁。
- `workspaces/<会话ID>/` 保存来源快照、笔记、输出、Skills 及 SDK 历史文件。

不要在任务运行中移动或删除这些数据。同一会话同时只能有一个执行者。取消或失败不会回滚已保存的 checkpoint 和文件；继续执行可能重新请求网络或重放工具。

来源文件保存的是搜索片段或提取正文。读完一个快照不等于取得了网页全部内容，来源相关也不等于支持答案的每个判断。
