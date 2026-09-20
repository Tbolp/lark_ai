# 分析师群 AI 机器人

飞书群《分析师》里的行情分析机器人。在群里 @ 机器人，它就跑一次 codex 分析，
**跑完之后**把结果发回群里——每次 @ 只产生一条消息，中途的工具调用、命令输出、
过程解说都不外发。

程序是单线程 asyncio：收消息、发消息、起 codex 都走异步接口；读 prompt 这类几 KB 的本地文件保持同步读取。

## 代码结构

入口 `stock_codex_bot.py` 只做一件事：调用 `bot.app.main()`。实现按功能拆在 `bot/` 里：

| 文件 | 职责 |
| --- | --- |
| `bot/logging_tee.py` | 把 stderr 复制到按天滚动的日志文件 |
| `bot/prompts.py` | 模板热加载与占位符渲染 |
| `bot/procs.py` | 子进程收尾（整棵进程树）与输出解码 |
| `bot/events.py` | 本机器人的事件名（框架只当它们是字符串） |
| `core/` | 与业务无关的小框架：事件注册/发射 + 后台协程生命周期 |
| `bot/lark_chat.py` | 接入飞书：长连接收群消息，归一化 + 判断是否 @ 了机器人，触发 `mention.received` |
| `bot/lark_sender.py` | 往飞书发消息（官方 SDK） |
| `bot/codex_app_server.py` | codex app-server 客户端（JSON-RPC，取最终正文）+ 技能解析 |
| `bot/codex.py` | 调 codex：选 app-server 还是 exec 回退 |
| `bot/app.py` | 顶层流程：读 `config.json` + 事件监听 + @ 触发 + 组装启动 |

依赖是单向的：`core（框架）→ events → procs → logging_tee / prompts
→ lark_chat/lark_sender → codex_app_server → codex → app`，
所以每个模块都能单独导入、单独读。

飞书相关的事情都收在 `bot/lark_chat.py`：收消息、归一化、判断哪条算「@ 了机器人」。
判断过了它直接 `emit(mention.received)`，不回调 app —— app 只负责注册 handler 和处理业务。

参数传递也守着同一条线：外部参数只有 `config.json` 一个来源，`bot/app.py` 的 `main()`
读它、校验它，然后拆成具名参数（`chat_id` / `project_dir` / `codex_timeout` / `skills` …）
发给 `run()`；再往下每一层都只收自己用得到的那几个参数。
事件 handler 也一样：`mention_handler()` 在启动时就把这些参数装配好，handler 本身只收事件 payload。

发消息 / 改消息走飞书官方 SDK（`lark-oapi`），应用凭据（`app_id` / `app_secret`）和
其它配置一起放在 `config.json` 里——**这个文件含密钥，已在 `.gitignore` 里，不要提交**。
可选的 `domain`：国内飞书留空，Lark 国际站填 `https://open.larksuite.com`。
收事件也是同一条应用凭据建的 SDK 长连接。

## 启动

项目要求 Python 3.12，推荐使用 [uv](https://docs.astral.sh/uv/) 自动创建 `.venv` 并运行。

```bash
uv run python stock_codex_bot.py            # 常驻: 长连接监听 + @ 触发
```

机器人要一直收消息，进程得一直跑。需要后台常驻的话，用 `nohup` 或 launchd 托管，例如：

```bash
nohup uv run python stock_codex_bot.py > /dev/null 2>&1 &
```

机器人会默认把行为日志写到项目下的 `logs/` 目录，按天滚动生成 `bot-YYYY-MM-DD.log`，终端里也会同步显示。

## 技能（skills）

每次分析都会把 `config.json` 里 `skills` 列的技能显式挂给 codex：
app-server 路径在 turn 的输入里带上 `{type: "skill", name, path}`，让服务端直接把完整的
`SKILL.md` 指令注入进来，不用模型自己按名字找；`codex exec` 回退路径则在 prompt 开头加
`$westock-data` 标记。

技能从 `~/.agents/skills`（用户级）或项目下的 `.agents/skills`（仓库级）里找，按名字命中
`<目录>/<name>/SKILL.md` 才生效。配置里写了却找不到，启动时直接报错退出——不会跑着跑着
才发现技能没挂上。

## 日志

日志记录机器人的主要行为，包括：

- 长连接启动、重连与退出
- 群内 @ 触发、收到的消息内容
- prompt 加载与热更新
- 发送到群里的消息结果、Codex 分析启动与失败原因

默认日志文件在项目根目录下的 `logs/` 里，文件名形如 `bot-2026-09-10.log`，
每天会自动滚动到一个新文件，不会一直往同一个文件追加；每行带 `YYYY-MM-DD HH:MM:SS` 时间戳。

```bash
# config.json: 不想写文件时把 log_file 设成 ""
"log_file": "logs/bot.log"
```

`log_file` 也可以指向别处（目录不存在时会自动创建）：相对路径按 `config.json` 所在目录解析，
绝对路径原样使用。值里如果包含 `{date}`，会原样替换成当天日期；否则会在文件扩展名前
自动插入日期（例如 `/var/log/analyst-bot.log` 会写成 `/var/log/analyst-bot-2026-09-10.log`）。

## prompt：一段静态指令（system prompt）

| 文件 | 作用 |
| --- | --- |
| `prompts/mention.md` | 机器人的行为指令 |

它是**纯静态文本，没有占位符**，整段作为 codex 会话的 developer instructions 传进去——
也就是 system prompt 那一层。好处是：不占对话历史、不会被上下文压缩吃掉、每轮自动生效。

- **不需要写日期时间**：codex 自己知道当前时间、星期和机器时区（实测问它「现在几点」能答对）。
  用户那句话也不用写进指令里——它是每轮单独发过去的输入。
- **每个会话读一次**：会话建立时读盘（日志里有一行 `[prompt] 读取 ...`），所以改了文件，
  下一条新会话就用新的；已经建立的那条会话不受影响（**不热加载**）。
- **启动时会校验**：文件不存在、读不了、是空文件，程序直接退出（退出码 2）并说明原因。

## 配置：config.json

命令行参数一个都不需要，全部走项目根目录的 `config.json`（相对路径按该文件所在目录解析，
所以从哪个目录启动都一样）：

| 字段 | 示例 | 说明 |
| --- | --- | --- |
| `chat_id` | `oc_...` | 目标群 |
| `bot_open_id` | `ou_...` | 机器人 open_id，用于识别 @ |
| `app_id` | `cli_...` | 飞书应用凭据（**含密钥，别提交**） |
| `app_secret` | `...` | 飞书应用凭据 |
| `domain` | `""` | 可选项：国内飞书留空，Lark 国际站填 URL |
| `project_dir` | `.` | codex 的工作目录 |
| `prompts_dir` | `prompts` | prompt 模板目录 |
| `mention_prompt` | `mention.md` | @ 触发时用哪个模板 |
| `codex_timeout` | `600` | 单次分析超时秒数 |
| `session_idle_timeout` | `1800` | 群消息之间超过这么多秒没有新 @，就结束会话；下一问重新起一条 |
| `skills` | `["westock-data"]` | 挂给 codex 的技能名 |
| `log_file` | `logs/bot.log` | 行为日志文件，按天生成 `bot-YYYY-MM-DD.log`；空字符串关闭 |

启动时逐项校验：字段缺没缺、类型对不对、时区认不认、目录和凭据文件在不在、技能装没装、
prompt 读不读得到。任何一项不过关就打印一行原因并以退出码 2 结束，不带 traceback。

## 一次 @ 是怎么跑的

- `lark_chat` 收到消息、判断是不是 @ 了机器人，是就发 `mention.received` 事件；
- handler 把**群里那句话原样**交给 codex（指令不在这里发，它在会话上）；
  默认走 `codex app-server`（stdio 上 JSON-RPC），起不来就自动回退到 `codex exec --json`
  （回退时指令会拼在 prompt 前面一起发，因为 exec 没有会话）；
- **会话是常驻的**：一个 app-server 进程 + 一条 thread，群里每来一条 @ 就往这条 thread
  里发一个 turn。所以 30 分钟（`session_idle_timeout`）以内的连续提问共享上下文——
  问完「看下茅台」再问「它今天涨跌幅多少」，它知道「它」是谁；
- **指令在会话上**：建 thread 时作为 developer instructions 传进去，不进对话历史；
- **跑到一半有人插话**：那句话被追加进当前这一轮（`turn/steer`），codex 接着往下做，
  最终答案还是一条。日志里是 `[codex] 追问已追加到当前分析`；
- 超过 `session_idle_timeout` 没有新 @，就把进程关掉，**下一问重新起一条新会话**
  （上下文清空）。turn 超时、出错也走同一条路：关掉重来，不留半死不活的会话；
- app-server 这条路会把技能作为 `skill` 输入项挂上去（完整 `SKILL.md` 直接注进上下文）；
  exec 回退时只能用 prompt 开头的 `$技能名` 标记，让模型自己去找，而且是全新会话、没有上下文；
- 只取最终正文（`phase` 是 `final_answer` 的那段，没有就退化成全部正文），
  命令执行、工具调用、中间解说一律不取；
- 跑完把结果整条发到群里。成功了发正文，失败了发 `⚠️ 分析失败` 加原因——
  失败也一定有回音；
- 机器人没人能点审批，所以 app-server 发来的审批请求（需要写文件/联网等）一律拒绝，
  与 `codex exec` 非交互时的行为一致。

## 已知限制

- **@ 触发**一次只跑一个。分析期间再有人 @，机器人把这句话**追加进正在跑的那一轮**
  （app-server 的 `turn/steer`）——codex 会把它当成同一轮的补充要求，最后仍然只回一条消息，
  群里不会有第二条回复。追加不了的时候（那一轮还没真正开跑、或者走的是 exec 回退）
  才回一句"上一条分析还在进行中，请稍等片刻"。
  分析跑在后台 asyncio 任务里，事件监听不会被堵住。
- **共享上下文是双刃剑**：`session_idle_timeout` 窗口内的提问互相可见，好处是能追问，
  代价是别人问的标的会留在上下文里，可能影响下一条的回答。想彻底隔离就把这个值调小
  （比如 60 秒），相当于「只有紧跟着的追问才共享上下文」。
- **上下文主要被工具输出占满**：一次 `westock quote` 返回的表有四十多列，比对话本身长得多。
  所以窗口内多问几轮之后，codex 会自己压缩历史；压缩是有损的，早先的细节可能记不清。
- 长连接断开由 SDK 自己重连（`auto_reconnect`）；退出就是 Ctrl+C 或进程被拆掉。
