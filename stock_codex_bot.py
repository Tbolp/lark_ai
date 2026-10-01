#!/usr/bin/env python3
"""飞书行情分析机器人（入口）: 群里 @、私聊、每个整点的定时播报都能触发。

实现都在 bot/ 包里, 按功能拆开:

  logging_tee.py      把 stderr 复制到按天滚动的日志文件
  prompts.py          prompt 文件读取、启动校验与时间占位符渲染
  procs.py            子进程收尾（整棵进程树）与输出解码
  events.py           本机器人的事件名（框架只当它们是字符串）
  lark_chat.py        接入飞书: 长连接收消息, 归一化 + 判断该不该响应, 发事件
  lark_sender.py      往飞书发消息（官方 SDK）
  skills.py           技能: 读 SKILL.md 正文 + 登记 references 白名单
  store.py            状态库（SQLite）: 关注列表 / 私聊会话表 / 推送清单 / 失败计数
  timer.py            定时消息源: 每个整点发一条【定时播报】（和飞书那边发同一个事件）
  tools.py            三个工具: run_westock / read_reference / watchlist
  harness.py          自建 harness: 工具循环 + 按 chat_id 复用的会话
  app.py              顶层流程: 读 config.json + 事件监听 + 触发/拒绝处理 + 组装启动

另外 core/ 是与业务无关的小框架: 事件注册/发射 + 后台协程生命周期, app 启动时把
handler 注册进去（trigger.received / trigger.rejected / task.done）。

所有参数都在项目根目录的 config.json 里（相对路径按配置文件所在目录解析）:

  chats                   群/话题群白名单（chat_id）; 群里要 @ 机器人才触发
  users                   私聊白名单（用户 open_id）; 私聊直接说话就触发
  bot_open_id             机器人 open_id（用于识别群里的 @）
  app_id                  飞书应用 id
  app_secret              飞书应用密钥; domain 可选（国内飞书留空, Lark 国际站填 URL）
  api_key / api_base_url / model
                          模型凭据与端点（OpenAI 兼容; 走 Chat Completions + 工具调用）
  turn_timeout            一轮分析的总超时秒数
  api_timeout             单次模型调用的超时秒数
  command_timeout         单条 westock 命令的超时秒数
  tool_output_limit       单条工具输出最多带回去多少字符（超了保留头尾）
  max_tool_rounds         一轮里最多允许模型调多少次工具
  project_dir             技能搜索与命令执行的参考目录
  prompts_dir             prompt 文件目录
  prompt_file             用哪个 prompt 文件当机器人的指令
  session_idle_timeout    连续这么多秒既没有输入也没有输出, 就结束会话（下一问重新起）
  skills                  要注入的技能名（正文常驻 prompt, 参考文档按需读）
  timer_enabled           整点播报开关（默认 false）; 每个整点给每个目标发一条消息
  timer_message           定时发的那句话（不改就用默认文案）
  state_file              状态库文件（关注列表 / 私聊会话表 / 推送清单 / 失败计数）
  log_file                行为日志文件

密钥字段（api_key / app_secret）可以写成 "env:NAME", 启动时按环境变量取值（也支持配置文件
同目录的 .env, 真实环境变量优先）。所以 config.json 里不放明文密钥、可以纳入 git; 真正
存密钥的 .env 在 .gitignore 里, 别提交。

启动时这些配置都会被校验（文件在不在、时区认不认、技能装没装、prompt 读不读得到）,
有问题直接退出（退出码 2）, 不会带着坏配置跑起来。

运行路径:
  群里 @ 机器人 / 白名单用户私聊 -> 把这条消息发给这个 chat_id 的会话（一个聊天一条）
  -> 模型可以调工具取数, 跑完把结果发回同一个聊天（中途不外发任何东西）。
  同一个聊天里的追问共享上下文（正在跑的那一轮会把追问并进去, 所以一个聊天只出一条回复）,
  不同聊天之间互不可见; 连续 session_idle_timeout 秒没有输入/输出就清掉那段上下文,
  下一问重新开始。不在白名单的群/人会被回一句"当前会话未授权"（同一个会话 60 秒只回一次）。

prompt 与技能正文是**纯静态指令**, 会话建立时拼成 system 消息: 不占对话历史,
不会被压缩掉, 每轮自动生效; 日期时间模型自己知道, 用户那句话是每轮单独发的。
改 prompt / SKILL.md 不用重启, 但只对**下一条新会话**生效（已经建立的那条不受影响）。

这里不再经过 codex: 模型调用、工具循环、会话历史都在本进程里（见 bot/harness.py）。

用法:
  python stock_codex_bot.py
"""

from bot.app import main

if __name__ == "__main__":
    main()
