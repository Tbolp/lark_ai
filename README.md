# 飞书行情分析机器人（白名单群 + 白名单用户私聊）

飞书里的行情分析机器人，服务两份白名单：**白名单群**（群里 @ 机器人）与**白名单用户私聊**
（直接说话即可）。收到消息它就跑一轮分析，**跑完之后**把结果发回同一个聊天——
每次触发只产生一条消息，中途的工具调用、命令输出、过程解说都不外发。

每个聊天**按 `chat_id` 复用同一条会话**：同一个群里追问、同一个人私聊追问都
共享上下文（「它今天涨跌幅多少」知道「它」是谁），不同聊天之间互不可见（群里问过的标的
不会跑到别人的私聊里）。会话数不设上限，每条会话各自按空闲时间静默回收。

模型这一侧是**本项目自己搭的 harness，不经过 codex**：一轮就是「把那句话交给模型 →
模型要调工具就调 → 结果回填 → 直到它给出不带工具调用的正文」。指令、工具表、对话历史
都在这个进程里，模型端点走 OpenAI 兼容的 Chat Completions（当前接的是 DeepSeek）。

程序是单线程 asyncio：收消息、发消息、调模型、跑命令都走异步接口；读 prompt / 技能这类
几十 KB 的本地文件保持同步读取。

除了等人问，机器人还可以**每个整点自己发一条消息**（`timer_enabled`）：那条消息和人在群里
@ 它时说的话走**完全一样**的分析链路，只是发消息的人是定时源、进的是一条内部会话。分析完
由模型判定「这一轮有没有值得推的」，正文是 `NO_PUSH` 就只写日志、不发群，否则发到目标会话
（群白名单 + 已经私聊过机器人的白名单用户），并把这一条记进去重清单，下一轮带给模型参考。
用户也能在对话里让它**关注股票**（第三个工具 `watchlist`），整点播报会额外看这些股票自己的
新闻。

## 代码结构

入口 `stock_codex_bot.py` 只做一件事：调用 `bot.app.main()`。实现按功能拆在 `bot/` 里：

| 文件 | 职责 |
| --- | --- |
| `bot/logging_tee.py` | 把 stderr 复制到按天滚动的日志文件 |
| `bot/prompts.py` | prompt 文件的读取、启动校验与时间占位符（`{datetime}` / `{date}` / `{weekday}`）渲染 |
| `bot/procs.py` | 子进程收尾（整棵进程树）与输出解码 |
| `bot/events.py` | 本机器人的事件名（框架只当它们是字符串） |
| `core/` | 与业务无关的小框架：事件注册/发射 + 后台协程生命周期 |
| `bot/lark_chat.py` | 接入飞书：长连接收消息，归一化 + 判断该不该响应，触发 `trigger.received` / `trigger.rejected` |
| `bot/lark_sender.py` | 往飞书发消息（官方 SDK） |
| `bot/skills.py` | 技能：读 `SKILL.md` 正文 + 登记 `references/` 白名单 + 和 prompt 文件拼成静态指令 |
| `bot/store.py` | 状态库（标准库 `sqlite3`，一个文件）：关注列表 / 私聊会话表 / 推送清单 / 失败计数 |
| `bot/timer.py` | 定时消息源：每个整点给每个目标发一条【定时播报】，发的是和飞书那边同一个 `trigger.received` |
| `bot/tools.py` | 三个工具：`run_westock`（跑一条 westock 命令）/ `read_reference`（读参考文档）/ `watchlist`（看/加/删当前会话关注的股票），安全边界都在这里 |
| `bot/harness.py` | 自建 harness：工具循环 + 按 `chat_id` 复用的会话 + 历史裁剪与空闲回收 |
| `bot/app.py` | 顶层流程：读 `config.json` + 事件监听 + 触发/拒绝处理 + 组装启动 |

依赖是单向的：`core（框架）→ events → procs → logging_tee / prompts
→ lark_chat/lark_sender / timer → skills / store / tools / harness → app`，
所以每个模块都能单独导入、单独读。

飞书相关的事情都收在 `bot/lark_chat.py`：收消息、归一化、判断这条该不该响应
（`classify()`：白名单群要 @ 机器人、白名单用户私聊要人对得上）。该响应的
`emit(trigger.received)`，明确不该响应的 `emit(trigger.rejected)`，其余（非文本消息、
机器人自己发的、群里没 @）什么都不发 —— app 只负责注册 handler 和处理业务。

参数传递也守着同一条线：外部参数只有 `config.json` 一个来源，`bot/app.py` 的 `main()`
读它、校验它，然后拆成具名参数（`chats` / `users` / `api_key` / `model` / `skills` …）
发给 `run()`；再往下每一层都只收自己用得到的那几个参数。
事件 handler 也一样：`trigger_handler()` / `reject_handler()` 在启动时就把这些参数装配好，
handler 本身只收事件 payload。

发消息 / 改消息走飞书官方 SDK（`lark-oapi`），应用凭据（`app_id` / `app_secret`）和
其它配置一起放在 `config.json` 里。**明文密钥不进 `config.json`**：`app_secret` / `api_key`
这类字段写成 `"env:NAME"`，真实值放环境变量或同目录的 `.env`（见下面「密钥：不放明文」）。
可选的 `domain`：国内飞书留空，Lark 国际站填 `https://open.larksuite.com`。
收事件也是同一条应用凭据建的 SDK 长连接。

## 启动

项目要求 Python 3.12，推荐使用 [uv](https://docs.astral.sh/uv/) 自动创建 `.venv` 并运行。
第一次跑先按上面的说明准备凭据：`cp .env.example .env` 并填上 `DEEPSEEK_API_KEY` /
`LARK_APP_SECRET`（或改成你自己环境里已有的变量名，同步改 `config.json` 里的 `env:` 引用）。

```bash
uv run python stock_codex_bot.py            # 常驻: 长连接监听 + 群内 @ / 私聊触发
uv run pytest                               # 跑单元测试（配置解析、判断规则、会话注册表、handler）
```

变成「白名单群 + 白名单用户私聊」需要在开发者后台给应用加**私聊消息**相关权限：
`im:message.p2p_msg:readonly`（群消息还是原来的 `im:message` 那一套），加完要重新发布版本
才生效。没开这个权限时私聊消息根本收不到，日志里不会有任何痕迹。

机器人要一直收消息，进程得一直跑。需要后台常驻的话，用 `nohup` 或 launchd 托管，例如：

```bash
nohup uv run python stock_codex_bot.py > /dev/null 2>&1 &
```

（密钥走 `.env` 时不用额外 source；走真实环境变量的话记得在启动前 `export` 好。）

机器人会默认把行为日志写到项目下的 `logs/` 目录，按天滚动生成 `bot-YYYY-MM-DD.log`，终端里也会同步显示。

## 技能（skills）

`config.json` 里 `skills` 列的技能，在**进程启动时**读成正文，和 `instructions.md` 一起拼成
一段静态指令（会话的 system 消息）：

- **正文常驻**：读 `~/.agents/skills/<name>/SKILL.md`（或项目下的 `.agents/skills/...`），
  去掉 front matter 后整段进指令。当前两个技能合计约 13k 字符，一万 token 上下，对 1M
  上下文的模型不算负担，而且这段前缀是稳定的、能命中 prompt 缓存；
- **参考文档按需读**：技能的 `references/*.md` 不进 prompt，模型需要时自己调
  `read_reference(skill, name)`。白名单是启动时扫目录得到的，表外的文件名拿不到；
- **改了要重启**：`SKILL.md` 在会话建立时读一次，所以改完得重启进程（或等空闲超时清掉
  那段上下文）才生效。

和 codex 那套的区别值得单独说一句：codex 每轮会把**所有**被发现技能的 name + description
+ path 塞进 prompt（这台机器上有 31 个用户技能 + 8 个插件技能，约 9k 字符每轮），正文才是
按需读。这里反过来 —— 只有配置里点名的技能进 prompt，正文还是一次性常驻。代价是改技能要
重启，好处是 prompt 里有什么完全由配置决定，不会因为多装了一个技能就悄悄变长。

## 整点播报（定时消息）

`config.json` 里 `timer_enabled: true` 之后，机器人会**每个整点**（09:00、10:00…，按机器本地
时间）给每个目标发一条消息：

- **目标**：`chats` 白名单里的群 + `users` 白名单里**已经私聊过机器人**的人。私聊的 `chat_id`
  是运行期从收到的消息里学到的（写进状态库），从没私聊过的人要等他说第一句话；重启不丢。
- **消息内容**：`【定时播报】` + `timer_message` 那句话（默认是「看看最近一段时间有没有对股市
  影响比较大的消息，有就整理成一条可以直接发到群里的消息，没有就只回一行 `NO_PUSH`。」），
  后面附一段系统附加信息：本轮的时间窗口、这个会话关注了哪些股票、最近已经推送过什么。
- **走的是同一条分析链路**：定时源只发事件（和飞书那边同一个 `trigger.received`），处理交给
  同一个 `trigger_handler`；只不过进的是 `timer:<目标>` 这条**内部会话**，不碰那个聊天自己的
  上下文（每轮开始前还会把内部会话的历史清一遍，保证每小时都是干净的一轮）。
- **发不发由模型判定**：正文第一行是 `NO_PUSH` 就只写日志、群里保持安静；否则加一行时间头部
  发到那个目标，并把「时间 + 一句话结论」记进去重清单（每个目标留最近 5 条 / 48 小时）。
- **失败了会吭声**：连续 3 轮失败（模型、网络、工具出错）就给那个目标发一条
  「⚠️ 整点播报连续 3 轮失败」，之后 24 小时内最多再提一次；单次失败只写日志。
- **目标之间错开 3 秒发出**，免得同一秒把多个会话一起放开、一起去打 westock。

想改「盯什么」改 `timer_message` 就行（例如「看看大盘和半导体板块」）；想关掉就
`timer_enabled: false`。想立刻看一轮效果、不等整点，见下面的手工验收。

## 关注列表（对话里增删查）

在群里 @ 或私聊说「关注一下宁德时代」「我关注了哪些」「取消关注宁德时代」，机器人会：

1. 用 `westock search` 把名字换成带市场前缀的代码（`sh600519` / `hk00700` / `usAAPL.OQ`…）；
2. 调 `watchlist` 工具，管的是**当前这个会话**的列表（群和私聊各一份），然后回一句确认，
   例如「已关注宁德时代 sz300750，当前共 3 只」。

整点播报时，这个会话的关注列表会被带进消息，机器人会**额外拉这些股票自己的新闻**
（`westock news list <代码们> --limit 5`）：只要**对该股自身行情有影响**（业绩暴雷、重大合同、
监管处罚、停牌重组、大额增减持、直接冲击它的行业政策…）就值得推，不必到「影响整个市场」的量级。

`watchlist` 是机器人**唯一会写东西的工具**：只写状态库里这个会话的关注列表，路径来自配置、
不来自模型；代码格式（市场前缀 + 字符集）和每会话 20 只的上限都在代码里校验，不靠提示词自律。

## 状态库（logs/state.db）

关注列表、私聊会话表（`open_id -> chat_id`）、推送去重清单、连续失败计数都放在**一个 SQLite
文件**里（标准库 `sqlite3`，不引依赖），默认 `logs/state.db`，可以用 `state_file` 改。

```bash
sqlite3 logs/state.db '.tables'
sqlite3 logs/state.db 'select chat_id, code, name, datetime(added_at, "unixepoch", "localtime") from watchlist'
sqlite3 logs/state.db 'select chat_id, streak, datetime(alerted_at, "unixepoch", "localtime") from timer_health'
```

时间存的是 epoch 秒，所以查的时候套一层 `datetime(..., 'unixepoch', 'localtime')`。

两点要注意：

- **`logs/` 里现在有用户数据**（关注列表），不再是纯日志目录；备份/迁移要把这个文件一起带上。
- 这个项目放在 iCloud 同步目录里（`~/Library/Mobile Documents/...`），活跃的 SQLite 被云同步
  有风险：**同一时间只在一台机器上跑**；觉得卡就把 `state_file` 指到本地目录（例如
  `~/Library/Application Support/lark-analyst/state.db`）。

## 日志

日志记录机器人的主要行为，包括：

- 长连接启动、重连与退出
- 触发（`<- 触发 chat_id=... chat_type=... sender=...`）与拒绝（`<- 拒绝 ...`）各一行，
  带上 `chat_id` / `chat_type` / `sender_id`，收到的消息内容也在这一行里
- prompt 加载与热更新
- 发送到聊天里的消息结果——成功/失败都带 `chat_id`（`消息发送成功 chat_id=oc_... om_...`），
  群和私聊的发送交织在一起时能一眼对上号；以及模型调用与工具调用
  （`[harness] 工具 run_westock {"command": ...}`）和失败原因

默认日志文件在项目根目录下的 `logs/` 里，文件名形如 `bot-2026-09-10.log`，
每天会自动滚动到一个新文件，不会一直往同一个文件追加；每行带 `YYYY-MM-DD HH:MM:SS` 时间戳。

```bash
# config.json: 不想写文件时把 log_file 设成 ""
"log_file": "logs/bot.log"
```

`log_file` 也可以指向别处（目录不存在时会自动创建）：相对路径按 `config.json` 所在目录解析，
绝对路径原样使用。值里如果包含 `{date}`，会原样替换成当天日期；否则会在文件扩展名前
自动插入日期（例如 `/var/log/analyst-bot.log` 会写成 `/var/log/analyst-bot-2026-09-10.log`）。

## prompt：一段指令（system prompt）

| 文件 | 作用 |
| --- | --- |
| `prompts/instructions.md` | 机器人的行为指令（按消息类型写：提问 / 追问怎么答、定时播报怎么判、关注列表怎么管） |

它和技能正文拼起来作为会话的 system 消息，不占对话历史、不会被上下文压缩吃掉。

- **可以写时间占位符**：`{datetime}`（`2026-09-28 14:03 周一`）、`{date}`、`{weekday}`；
  不认识的 `{…}` 原样留着。用户那句话不用写进指令里——它是每轮单独发过去的输入。
- **每段新对话渲染一次**：开始一段新对话时读盘 + 渲染（日志里有一行 `[prompt] 读取 ...`），
  所以改了文件下一条会话就生效，时间也不会冻在进程启动那一刻；同一段对话里它是稳定的。
- **启动时会校验**：文件不存在、读不了、是空文件，程序直接退出（退出码 2）并说明原因。

## 配置：config.json

命令行参数一个都不需要，全部走项目根目录的 `config.json`（相对路径按该文件所在目录解析，
所以从哪个目录启动都一样）：

| 字段 | 示例 | 说明 |
| --- | --- | --- |
| `chats` | `["oc_..."]` | 群 / 话题群白名单（`chat_id`），群里**要 @ 机器人**才响应 |
| `users` | `["ou_..."]` | 私聊白名单（用户 `open_id`），私聊里直接说话就响应，不用 @ |
| `bot_open_id` | `ou_...` | 机器人 open_id，用于识别 @ |
| `app_id` | `cli_...` | 飞书应用 id（不是密钥，可直接写） |
| `app_secret` | `env:LARK_APP_SECRET` | 飞书应用密钥；**建议用 `env:NAME` 引用环境变量**，别写明文 |
| `domain` | `""` | 可选项：国内飞书留空，Lark 国际站填 URL |
| `api_key` | `env:DEEPSEEK_API_KEY` | 模型密钥；**建议用 `env:NAME` 引用环境变量**，别写明文 |
| `api_base_url` | `https://api.deepseek.com/` | 模型端点，OpenAI 兼容；留空用 SDK 默认 |
| `model` | `deepseek-flash` | 模型名 |
| `turn_timeout` | `600` | 一轮分析的总超时秒数（含所有工具调用） |
| `api_timeout` | `120` | 单次模型调用的超时秒数 |
| `command_timeout` | `60` | 单条 `westock` 命令的超时秒数 |
| `tool_output_limit` | `60000` | 单条工具输出最多带回去多少字符，超了保留头尾 |
| `max_tool_rounds` | `40` | 一轮里最多允许模型调多少次工具 |
| `max_parallel_tools` | `8` | 同一批工具调用最多同时跑几个 |
| `project_dir` | `.` | 技能搜索与命令执行的参考目录 |
| `prompts_dir` | `prompts` | prompt 文件目录 |
| `prompt_file` | `instructions.md` | 读哪个 prompt 文件当机器人的指令（群和私聊共用这一份） |
| `session_idle_timeout` | `600` | 连续这么多秒既没有输入也没有输出，就结束会话；下一问重新起一条 |
| `skills` | `["westock-data", "westock-screener"]` | 要注入的技能名（正文常驻 prompt） |
| `timer_enabled` | `false` | 整点播报开关；打开后每个整点给每个目标发一条消息 |
| `timer_message` | 一段内置默认文案 | 定时发的那句话（盯大盘还是盯某只股票由它决定） |
| `state_file` | `logs/state.db` | 状态库：关注列表 + 私聊会话表 + 推送清单 + 失败计数 |
| `log_file` | `logs/bot.log` | 行为日志文件，按天生成 `bot-YYYY-MM-DD.log`；空字符串关闭 |

启动时逐项校验：字段缺没缺、类型对不对、时区认不认、目录和凭据文件在不在、技能装没装、
prompt 读不读得到。任何一项不过关就打印一行原因并以退出码 2 结束，不带 traceback。

`api_key` / `model` 是必填；`api_base_url` 与那几个超时/限额都有默认值，不写就用默认。
换模型只改 `model` 和 `api_base_url` 两个字段——只要那个端点支持 OpenAI 的
Chat Completions + 工具调用，harness 不用动。

### 密钥：不放明文，config.json 可以进 git

任何字符串字段都可以写成 `"env:NAME"`，启动时它会替换成环境变量 `NAME` 的值：

```json
"api_key": "env:DEEPSEEK_API_KEY",
"app_secret": "env:LARK_APP_SECRET"
```

- **取值顺序**：先看进程真实环境变量，没有再看配置文件同目录的 `.env`（真实环境变量优先）。
  也就是说 `export DEEPSEEK_API_KEY=...` 和写 `.env` 两种方式都行，部署环境（launchd /
  容器）适合前者，本机常驻适合后者。
- **`.env` 是老实的 KEY=VALUE**：空行和 `#` 注释会跳过，`export KEY=...` 和带引号的值也能认；
  只想用环境变量、不放 `.env` 也完全可以（文件不存在就不加载）。
- **缺了就启动失败**：必填字段（`api_key` / `app_secret` 等）引用的变量不存在或为空时，
  进程直接退出并提示变量名，不会拿着空 key 跑；可选字段（如 `domain`）引用的变量缺失则按空处理。
- **只用 `.env` 存密钥**：`.env` 已在 `.gitignore` 里，`config.json` 里不再有明文密钥，
  所以 `config.json` 现在可以正常纳入 git 管理。第一次跑时复制 `.env.example` 成 `.env` 填上即可：

```bash
cp .env.example .env      # 然后把里面两行换成真实值
```

关于两份白名单：

- `chats` 与 `users` 至少有一个非空，两个都是空数组（或者都没写）会直接启动失败——那等于
  机器人谁都不理；
- 数组元素必须是非空字符串，混进空串或非字符串会在启动时报错，不会带着坏白名单跑；
- 旧的 `chat_id` 字段**已废弃**：配置里还留着它，启动时会直接报错并提示迁移成
  `chats` / `users`；
- 想私聊机器人，先把你的 `open_id` 填进 `users`。`open_id` 是「应用 + 用户」维度的 id，
  用飞书开放平台的「获取用户信息」接口或者机器人日志里那行 `sender=ou_...` 查；
- `chats` / `users` 改了要重启进程才生效（配置只在启动时读一次）。

## 一次触发是怎么跑的

- `lark_chat` 收到消息、判断该不该响应：白名单群里 @ 了机器人、或者白名单用户私聊，
  发 `trigger.received`；不在白名单的群/人（明确 @ 了机器人或私聊了机器人）发
  `trigger.rejected`；非文本消息、机器人自己发的消息、群里没 @ 机器人，什么都不发；
- `timer` 是**第二个消息源**：每个整点给每个目标（群白名单 + 见过的私聊）发一条
  `【定时播报】`，发的是**同一个** `trigger.received`，只是 `chat_id` 是 `timer:<目标>`
  这条内部会话；`classify()`（白名单 / @ 那套）是飞书专属的，定时源不经过它。
  定时消息先把那条内部会话的历史清一遍，所以每小时都是干净的一轮；
- **触发**走 `trigger_handler`：记一行 `<- 触发` 日志（带 `chat_id` / `chat_type` /
  `sender_id`），然后把**那句话原样**（连同它的 `chat_id`）`post` 给会话注册表（指令不在
  这里发，它是会话的 system 消息）。开轮还是并进当前那一轮，全在会话里决定，
  handler 不等结果、也不看返回值。白名单私聊来消息时，顺带把 `open_id -> chat_id`
  记进状态库（整点播报的私聊目标就靠它）；这一步失败只写日志，不影响这条消息；
- **拒绝**走 `reject_handler`：回一句「当前会话未授权」提示。同一个 `chat_id` **60 秒内
  只回一次**，冷却期内的重复触发只写日志（`未授权提示已抑制`）不再发消息，免得被刷屏；
- **会话按 `chat_id` 分**（`ChatSessions`）：一个 `chat_id` 一条会话，第一次来消息时懒建，
  之后复用同一个对象。会话类型不参与 key —— 群里就是群，私聊就是那一个人的 `chat_id`，
  所以私聊之间、群与私聊之间天然隔离。话题群里不同话题**共用一条**会话（不按 `thread_id` 拆）；
- **会话就是一个消息历史**：内存里的 `messages` 列表（第一条是 system 指令），同一个聊天
  每来一条触发就往里加。所以 10 分钟（`session_idle_timeout`）以内的连续提问共享上下文——
  问完「看下茅台」再问「它今天涨跌幅多少」，它知道「它」是谁；
- **一轮 = 一个工具循环**：调模型 → 如果它要求调工具就执行、把结果作为 tool 消息回填 →
  再调模型，直到它给出不带工具调用的正文。工具调用写
  `[harness] 工具 run_westock 1.2s {"command": ...}`（带耗时）；
  单轮上限 `turn_timeout` 秒 / `max_tool_rounds` 次，超了就中止这一轮并回一句失败原因；
- **同一批工具调用并发跑**：模型一轮里常常一口气发好几条互不依赖的查询（技能文档也要求
  这样的查询同轮发出），这些并发执行、上限 `max_parallel_tools`（默认 8），结果按模型给的
  顺序回填——所以 tool 消息与 `tool_calls` 一一对齐，日志里也能对着看。实测一次个股综合
  分析从 6 条串行的 16.7 秒降到 5 秒上下；
- **跑到一半有人插话**：那句话被追加进当前这一轮，模型接着往下做，最终答案还是一条。
  实现上是在循环的**每一轮开始前**把待处理的输入插进 messages——位置排在工具结果之后，
  所以 `assistant(tool_calls)` 和它对应的 `tool` 消息始终紧挨着，协议不会坏。
  日志里是 `[harness] 追问已并入当前分析`；
- **出去的只有进程的输出**：会话拿到的正文/失败原因走它自己的输出回调**发回它那个聊天**
  （回调按 `chat_id` 绑好），外部不等结果、也不记「忙不忙」——那是会话的事（它自己看轮状态）。
  输出回调对 `timer:` 开头的会话另有一套规则：`NO_PUSH` 或失败都不发，真发了才记去重清单；
- **会话是活动驱动的**：连续 `session_idle_timeout` 秒既没有输入也没有输出，就**清掉那段
  消息历史**（下一问从干净上下文开始）。清历史一律静默：只写一行日志，群里不回任何消息。
  其余会话不受影响（各算各的）。会话数不设上限；
- **工具输出带退出码**：`run_westock` 返回的是 `[exit=0]` 加输出（有 stderr 就附上），
  模型据此区分「没数据」和「命令写错了」。太长就保留头尾、中间标出省略了多少字符；
- 聊天里看到的就是模型给的正文：成功是正文，失败/超时/超轮数都是 `⚠️ 分析失败` 加原因——
  失败也一定有回音；
- 工具只有一个会写东西：`watchlist` 写状态库里的关注列表（路径来自配置、代码格式与上限都在
  代码里校验）；另外两个是只读的（跑查询命令、读本地文档）。没有审批环节，也没有改系统的能力。

## 测试

```bash
uv run pytest          # 单元测试: 归一化 / 判断规则 / 配置解析 / prompt 渲染 / 技能 /
                       # 工具 / 状态库 / 会话与工具循环 / 定时源 / 输出与触发 handler
```

测试不碰网络、不调模型、不起 `westock`：飞书 sender、会话注册表、时钟、定时源的 `sleep` 与
`emit` 都用假替身
（`tests/helpers.py`），模型客户端与 `westock` 可执行文件在用例里各自造假
（`tests/test_harness.py` 的 `FakeClient`、`tests/test_tools.py` 的 `fake_program`）；
状态库那部分用真的 SQLite 写在临时目录里（`tests/test_store.py`）。

手工验收（要连着真实飞书 / 真实模型跑）：

1. 白名单群里 @ 机器人 → 出结果；
2. 白名单用户私聊里说一句话（不用 @）→ 出结果；
3. 紧接着在私聊里追问「它」→ 回答的是私聊里刚问的标的，不会带上群里那条会话的上下文；
4. 未授权群里 @ 机器人 → 回一句「当前会话未授权」；一分钟内再 @ 一次，不再收到提示；
5. 配置里留着旧的 `chat_id` 字段 → 启动直接报错退出，并提示迁移成 `chats` / `users`；
6. 群里说「关注一下宁德时代」→ 回一句确认；「我关注了哪些」→ 列出列表；「取消关注宁德时代」
   → 移掉。私聊里同样操作只影响私聊那份（`sqlite3 logs/state.db 'select * from watchlist'`
   能看到落库）；
7. 想立刻看一轮整点播报的效果（不等整点、也不发飞书），用下面这段：它只把这一轮会发的内容
   打在终端上；
8. 把 `api_key` 改错、连跑 3 轮 → 收到一条「整点播报连续 3 轮失败」，之后不再刷屏。

```bash
# 手工跑一轮定时播报: 真模型、真 westock, 但只打印、不发飞书
uv run python - <<'PY'
import asyncio

import openai

from bot.app import load_config
from bot.harness import ChatSessions
from bot.prompts import render_prompt
from bot.skills import build_instructions, reference_table
from bot.store import StateStore
from bot.timer import TimerSource
from bot.tools import Toolbox, resolve_program

cfg = load_config()
store = StateStore(cfg["state_file"])

# 直接把这一轮的正文打出来（output_handler 换成打印版）
async def show(chat_id, text, ok):
    print(f"\n=== {chat_id} ok={ok} ===\n{text}")

toolbox = Toolbox(references=reference_table(cfg["skills"]),
                  command_timeout=cfg["command_timeout"],
                  output_limit=cfg["tool_output_limit"],
                  program_path=resolve_program(), watchlist=store)
client = openai.AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["api_base_url"] or None)
sessions = ChatSessions(client=client, model=cfg["model"],
                        instructions=lambda: build_instructions(
                            render_prompt(cfg["prompts_dir"], cfg["prompt_file"]),
                            cfg["skills"]),
                        toolbox=toolbox, turn_timeout=cfg["turn_timeout"],
                        api_timeout=cfg["api_timeout"],
                        idle_timeout=cfg["session_idle_timeout"],
                        max_tool_rounds=cfg["max_tool_rounds"],
                        max_parallel_tools=cfg["max_parallel_tools"],
                        on_output=show)

async def main():
    events = []
    source = TimerSource(message=cfg["timer_message"],
                         targets=lambda: [*cfg["chats"], *store.peers().values()],
                         watchlist=store.describe, recent=store.recent_pushes,
                         emit=lambda name, event: events.append(event))
    await source.tick()
    for event in events:                       # 一条事件 = 一条内部会话的一轮
        await sessions.post(event["chat_id"], event["text"])
        await sessions.get(event["chat_id"]).task
    await sessions.close_all()

asyncio.run(main())
PY
```

（正常进程里这一步是 `core.emit` + `trigger_handler` 做的；这里把事件收集起来自己驱动，只为
了跑完就退出。）
不想连飞书、只想验证 harness 本身，可以直接拿 config 跑一轮（不经过飞书、不发消息）：

```bash
uv run python - <<'PY'
import asyncio

import openai

from bot.app import load_config
from bot.harness import ChatSessions
from bot.prompts import read_prompt
from bot.skills import build_instructions, reference_table
from bot.tools import Toolbox

cfg = load_config()

async def on_output(chat_id, text, ok):
    print(f"\n=== {chat_id} ok={ok} ===\n{text}")

instructions = build_instructions(
    read_prompt(cfg["prompts_dir"], cfg["prompt_file"]), cfg["skills"])
toolbox = Toolbox(references=reference_table(cfg["skills"]),
                  command_timeout=cfg["command_timeout"],
                  output_limit=cfg["tool_output_limit"])
client = openai.AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["api_base_url"] or None)
sessions = ChatSessions(client=client, model=cfg["model"], instructions=lambda: instructions,
                        toolbox=toolbox, turn_timeout=cfg["turn_timeout"],
                        api_timeout=cfg["api_timeout"], idle_timeout=cfg["session_idle_timeout"],
                        max_tool_rounds=cfg["max_tool_rounds"], on_output=on_output)

async def main():
    s = sessions.get("cli")
    await s.post("查一下贵州茅台 sh600519 最新的行情")
    await s.task
    await sessions.close_all()

asyncio.run(main())
PY
```

## 已知限制

- **一个聊天里一次只跑一个**（不同聊天的会话互不影响，各跑各的）。分析期间同一个聊天里
  再有人问，那句话会被**并入正在跑的那一轮**，模型把它当成同一轮的补充要求，最后仍然只回
  一条消息。两条消息同时到也不会撞车：`post()` 里「看状态 + 起轮」是串起来的，
  一条开轮、另一条并入。分析跑在后台 asyncio 任务里，事件监听不会被堵住。
  （「并入」只在模型还在这轮里跑的时候有效：如果上一轮刚好已经给出正文、只是任务还没收尾，
  那句话会开成新的一轮，于是那个聊天里会有两条回复。）
- **单轮有硬超时**：一轮超过 `turn_timeout` 秒、或者工具调用超过 `max_tool_rounds` 次就会
  中止并回一句失败原因。这是相对 codex 那版的行为变化——那版只要还在出东西就一直跑下去。
  真遇到复杂的多步筛选，把这两个值调大即可。
- **空闲清上下文是静默的**：连续 `session_idle_timeout` 秒没有输入也没有输出就清掉那段
  消息历史，只写日志、**那个聊天里不回任何消息**——轮还在跑、用户还在等答案时也是一样
  （这一轮的答案就没了，需要再问一次）。
- **共享上下文是双刃剑（但只在一个聊天内部）**：同一个 `chat_id` 在 `session_idle_timeout`
  窗口内的提问互相可见，好处是能追问，代价是**同一个聊天里**别人问的标的会留在上下文里，
  可能影响下一条的回答（群聊尤其明显）。想彻底隔离就把这个值调小（比如 60 秒），
  相当于「只有紧跟着的追问才共享上下文」。不同聊天之间从来不共享（key 就是 `chat_id`）。
- **未授权提示有冷却**：不在白名单的群/人，同一个 `chat_id` 60 秒内只会收到一次
  「当前会话未授权」，之后的重复触发只写日志；非文本消息（图片、文件、语音…）一律静默，
  连提示都不回。
- **上下文主要被工具输出占满**：一次 `westock quote` 返回的表有四十多列，比对话本身长得多。
  两个旋钮：`tool_output_limit` 限制单条工具输出（超了保留头尾、标出省略了多少字符），
  `max_history_messages`（代码里，默认 200 条）在历史过长时从**用户消息处**切掉最早一段
  ——切点是挑过的，不会把 `assistant(tool_calls)` 和它对应的 `tool` 消息拆开。
  被丢掉的细节就记不清了，这和压缩是一个道理。
- **命令能力是有边界的**：`run_westock` 只放行 `westock ` 开头的**单条**命令，不接受管道、
  重定向和命令链。这既是安全边界，也是技能文档里「禁止终端截断输出」那条规矩的代码落实；
  模型想少取一点数据，只能用 `--limit` / `--offset` 这些参数。
- **计费换了个口径**：以前是 codex 那套凭据，现在是 `api_key` 按 token 直接付给模型厂商。
  一轮分析通常会调 1~10 次模型（每次工具往返算一次）；实测一次行情问答约 5 秒，
  一次筛选问答约 15 秒。
- **整点播报每轮都是新会话**：它跑在 `timer:<目标>` 这条内部会话里，每轮开始前会把历史清掉，
  所以它**不知道那个人在群里刚问过什么**；反过来，群里接着追问「刚才那条什么意思」时，
  群会话里也没有那段上下文（播报没有回填进去），需要自己复述一句。
- **整点播报的目标是「学来的」**：私聊目标来自消息里见过的 `chat_id`。刚部署、对方还没私聊过
  机器人时，那个人的整点播报要等他说第一句话才会开始。
- **整点触发依赖机器不停**：进程不在（重启、关机）就跳过那些整点，**不补跑**；重启后要等
  下一个整点。触发按挂钟对齐，系统时间被大幅校正时可能跳过或提前一次。
- **定时流程失败是静默的**（这是有意的：群里不因为一次超时就响一声），只有连续 3 轮失败才发
  一条提示；排查看日志里的 `[timer]` / `[harness]` / `[store]` 行。
- **关注列表有上限**：每个会话 20 只。整点播报要逐个看这些股票的新闻，太多了单次工具输出会
  被 `tool_output_limit` 截断，反而漏看。
- 长连接断开由 SDK 自己重连（`auto_reconnect`）；退出就是 Ctrl+C 或进程被拆掉。
