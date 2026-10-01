"""行情分析机器人（飞书）: 白名单群里 @ 机器人、白名单用户私聊都能触发。

按功能拆成几个小模块, 依赖方向是单向的:

    logging_tee / prompts
        -> lark_chat / lark_sender -> skills / tools / harness -> app

每个聊天（群 / 私聊）按 chat_id 复用同一条会话: 聊天内追问共享上下文, 聊天之间互不可见。
模型调用是自建的 harness（harness.py 的工具循环 + tools.py 的两个工具）,
不再依赖 codex —— 指令、工具表、对话历史全在这个进程里。

所有外部参数走项目根目录的 config.json（app.py 负责读 + 校验）; 密钥字段可以写成
"env:NAME" 引用环境变量（app.py 还会顺带加载同目录的 .env, 真实环境变量优先）,
所以 config.json 里不放明文、可以进 git。

入口是仓库根目录的 stock_codex_bot.py, 它只负责调用 app.main()。
"""
