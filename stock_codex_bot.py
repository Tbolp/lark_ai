#!/usr/bin/env python3
"""分析师群 @Codex 行情分析机器人（入口）。

实现都在 bot/ 包里, 按功能拆开:

  logging_tee.py      把 stderr 复制到按天滚动的日志文件
  prompts.py          模板热加载与占位符渲染
  procs.py            子进程收尾（整棵进程树）与输出解码
  events.py           本机器人的事件名（框架只当它们是字符串）
  lark_chat.py        接入飞书: 长连接收群消息, 归一化 + 判断是否 @ 了机器人, 发事件
  lark_sender.py      往飞书发消息（官方 SDK）
  codex_app_server.py codex app-server 客户端（JSON-RPC）+ 技能解析
  codex.py            调 codex: 选 app-server 还是 exec 回退
  app.py              顶层流程: 读 config.json + 事件监听 + @ 触发 + 组装启动

另外 core/ 是与业务无关的小框架: 事件注册/发射 + 后台协程生命周期, app 启动时把
handler 注册进去（mention.received / task.done）。

所有参数都在项目根目录的 config.json 里（相对路径按配置文件所在目录解析）:

  chat_id / bot_open_id   目标群与机器人 open_id（用于识别 @）
  app_id / app_secret     飞书应用凭据; domain 可选（国内飞书留空, Lark 国际站填 URL）
  project_dir             codex 的工作目录
  prompts_dir             prompt 模板目录
  mention_prompt          用哪个 prompt 文件当机器人的指令
  codex_timeout           单次分析超时秒数
  session_idle_timeout    群消息之间超过这么多秒没有新 @, 就结束会话（下一问重新起）
  skills                  挂给 codex 的技能名
  log_file                行为日志文件

这个文件里有应用密钥, 已在 .gitignore 里（别提交）。

启动时这些配置都会被校验（文件在不在、时区认不认、技能装没装、prompt 读不读得到）,
有问题直接退出（退出码 2）, 不会带着坏配置跑起来。

运行路径:
  群内 @ 触发 -> 把这条消息发给常驻的 codex 会话（一条 thread）-> 跑完把结果发到群里
  （中途不外发任何东西）。会话空闲超过 session_idle_timeout 秒就关掉, 下一问重新起一条。

prompt 是**纯静态指令**, 建会话时作为 developer instructions（system prompt 那一层）传进去:
不占对话历史, 不会被压缩掉, 每轮自动生效; 日期时间 codex 自己知道, 用户那句话是每轮单独发的。
改 prompt 文件不用重启, 但只对**下一条新会话**生效（已经建立的那条不受影响）。

用法:
  python stock_codex_bot.py
"""

from bot.app import main

if __name__ == "__main__":
    main()
