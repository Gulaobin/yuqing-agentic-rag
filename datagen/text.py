"""帖子正文模板。按情感取模板，填入事件标题和关键词，便于按关键词检索帖子。"""

from __future__ import annotations

import random

ORIGINAL_TEMPLATES = {
    "negative": (
        "{kw}这件事太离谱了，{title}，必须给个说法。",
        "看到“{title}”的消息很气愤，{kw}的问题拖了这么久没人管？",
        "又是{kw}！{title}，希望相关部门认真查一查。",
        "{title}，身边已经有人受到影响了，太让人担心了。",
    ),
    "neutral": (
        "关注一下：{title}，目前还没看到官方回应。",
        "有人知道{kw}的最新进展吗？{title}。",
        "{title}，具体情况还在了解中，等更多信息。",
    ),
    "positive": (
        "{title}，相关部门回应还算及时，点个赞。",
        "{kw}的事情已经在处理了，希望尽快解决。",
        "{title}，后续处理措施挺到位的。",
    ),
}

REPOST_TEMPLATES = {
    "negative": ("转发。太过分了", "必须严查！", "{kw}这种事不能再有了", "转发，求一个真相"),
    "neutral": ("转发", "mark，持续关注", "转发，等后续", "蹲一个官方通报"),
    "positive": ("转发，支持依法处理", "处理得不错", "已经在解决了，大家别慌"),
}


def original_text(rng: random.Random, sentiment: str, title: str, keyword: str) -> str:
    return rng.choice(ORIGINAL_TEMPLATES[sentiment]).format(title=title, kw=keyword)


def repost_text(rng: random.Random, sentiment: str, keyword: str) -> str:
    return rng.choice(REPOST_TEMPLATES[sentiment]).format(kw=keyword)
