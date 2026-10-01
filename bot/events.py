"""本机器人的事件名（框架不认识这些名字, 只当它们是字符串）。

谁发的:
  trigger.received  消息源收到一条该响应的消息 —— 飞书那边是白名单群里 @ 了机器人 /
                    白名单用户私聊（lark_chat）; 定时那边是到点发来的一条【定时播报】
                    （timer）。两边发的是同一个事件, 处理链路完全一样。
  trigger.rejected  消息源收到一条不该响应的消息（群里 @ 了机器人但群不在白名单 /
                    私聊机器人但用户不在白名单）—— 用来回一句"当前会话未授权"
框架自己还会发 task.done（见 core.events）。

不是消息事件、非文本消息、机器人自己发的消息: 一律什么都不发（静默, 见 lark_chat）。
"""

TRIGGER = "trigger.received"
TRIGGER_REJECTED = "trigger.rejected"
