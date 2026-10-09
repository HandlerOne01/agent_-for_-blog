# -*- coding: utf-8 -*-
"""介绍子图状态。

target_post / post_content / introduce_failed 只在本子图内流转。
失败标记由子图条件边消费，无需传回父图。
"""
from langgraph.graph import MessagesState


class IntroduceState(MessagesState):
    introduce_failed: bool
    # 从用户问句里抽出的篇名（如「继承」），用于标题匹配
    target_query: str
    # 定位到的文章元数据：id/title/category/word_count/difficulty/blog_url
    target_post: dict
    # 正文 Markdown（缺正文行时为空串）
    post_content: str
    # blog_content.toc（JSON，可能为空）
    post_toc: object
    # 正文长度（字符数），用于分档
    content_len: int
    # 只有元数据、没有正文（LEFT JOIN 出 NULL）时为 True，供答复如实说明
    body_missing: bool
