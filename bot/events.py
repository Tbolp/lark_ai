"""本机器人的事件名（框架不认识这些名字, 只当它们是字符串）。

谁发的:
  mention.received  群消息源收到一条 @ 机器人的消息
框架自己还会发 task.done（见 core.events）。
"""

MENTION = "mention.received"
