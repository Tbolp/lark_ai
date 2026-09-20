"""分析师群 @Codex 行情分析机器人。

按功能拆成几个小模块, 依赖方向是单向的:

    logging_tee / prompts
        -> lark_chat / lark_sender -> codex_app_server -> codex -> app

所有外部参数走项目根目录的 config.json（app.py 负责读 + 校验）。

入口是仓库根目录的 stock_codex_bot.py, 它只负责调用 app.main()。
"""
