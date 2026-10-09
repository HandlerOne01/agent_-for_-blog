# -*- coding: utf-8 -*-
"""介绍文章子图的三个节点。

设计见 docs/superpowers/specs/2026-10-05-introduce-module-design.md

为什么定位顺序是"标题优先、向量兜底"（与推荐子图相反）：
用户点名了一篇文章，篇名就是**精确证据**；向量是概率猜测。
推荐场景用户只给了模糊方向、没有精确标识，所以那边才向量优先。
"""
import json
import os
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from src.agent.common.content import (
    fetch_content,
    fetch_post_meta,
    find_by_title,
)
from src.agent.common.db import DBUnavailable
from src.agent.common.llm import model, model_deterministic
from src.agent.common.persona import with_persona
from src.agent.common.reply import degrade_reply
from src.agent.common.retrieval import search_ids
from src.agent.node.recommend import _to_relative
from src.agent.state.introduce import IntroduceState

_INTRO_URL_RE = re.compile(r"/posts/[A-Za-z0-9_\-./]*")


class _TitleOnly(BaseModel):
    """只抽篇名，不做任何别的判断。"""

    query: str = Field(
        default="",
        description="用户想介绍的那篇文章的篇名，例如「继承」「前缀和」。"
        "用户没有点名任何文章时返回空字符串。",
    )


def extract_title(state: IntroduceState) -> dict:
    """把用户问句里的篇名抽出来。

    只被 locate_article 调用（不是图节点）—— "抽篇名"和"按篇名找文章"是
    同一件事的两半，拆成两个节点只会在图里多一个框，还让"抽完忘了用"这类
    装配错误有机可乘（历史上真发生过：它曾被单独注册，而 locate_article
    读的是 state 里的字段，两者一度接不上）。

    **异常在这里吞掉是有意的**：调用方 locate_article 的降级是"给用户一句
    人话"，不是重试；抽取失败降级为空篇名，用户会看到"没定位到"。
    """
    try:
        info = model_deterministic.with_structured_output(
            _TitleOnly, method="function_calling"
        ).invoke(
            [
                SystemMessage(
                    content="从用户的话里找出他想要介绍的那篇文章的篇名。"
                    "只返回篇名本身，不要带「这篇」「那篇」「介绍」等词。"
                    "用户没有点名具体文章时返回空字符串。"
                ),
                state["messages"][-1],
            ]
        )
        query = (info.query or "").strip()
    except Exception as exc:  # noqa: BLE001 抽取失败降级为空篇名，绝不冒 traceback
        print(f"[introduce] 篇名抽取失败，按没点名处理：{exc}")
        query = ""
    return {"target_query": query}


def locate_article(state: IntroduceState) -> dict:
    """定位文章：抽篇名 → 标题精确匹配优先 → 向量兜底。不读正文。

    所有分支都回写 target_query —— 抽出来的篇名是这条链路的输入留痕，
    丢了它之后排查"到底抽成了什么"只能靠翻日志。
    """
    extracted = extract_title(state)
    query = str(extracted.get("target_query") or state.get("target_query") or "").strip()
    if not query:
        # 没篇名 = 用户没点名，或抽取失败（已降级成空串）。
        # 两种都由 compose_intro 统一说"没定位到"。
        return extracted

    try:
        rows = find_by_title(query)
    except DBUnavailable as exc:
        print(f"[introduce] 定位时连库失败，降级：{exc}")
        return dict(degrade_reply(state), **extracted)

    if len(rows) == 1:
        return dict(extracted, target_post=rows[0])

    if len(rows) > 1:
        picked = _ask_which(rows, query)
        return dict(extracted, target_post=picked) if picked else extracted

    # 标题没命中 → 向量兜底
    try:
        hits = search_ids(query) or []
    except Exception as exc:  # noqa: BLE001 兜底路径不该把介绍拖垮
        print(f"[introduce] 向量兜底异常，当作没找到：{exc}")
        hits = []

    if not hits:
        return extracted

    gap = _cfg_float("INTRO_MATCH_GAP", 0.05)
    if len(hits) > 1 and (hits[0][1] - hits[1][1]) < gap:
        cands = [fetch_post_meta(i) for i, _ in hits[:5]]
        picked = _ask_which([c for c in cands if c], query)
        return dict(extracted, target_post=picked) if picked else extracted

    meta = fetch_post_meta(hits[0][0])
    return dict(extracted, target_post=meta) if meta else extracted


def _ask_which(rows: list, query: str, tries: int = 2):
    """命中多篇时反问用户。返回选中的那行；始终问不出来才返回 None。

    为什么要重问：用户很可能回「第二个」「继承那篇」这类话。
    只问一次、识别不了就放弃的话，locate_article 会返回空，
    用户被告知「没这篇」—— 可文章明明找到了（与成功标准 5 冲突）。
    """
    lines = "\n".join(
        "%d. 《%s》 · %s · 约 %s 字"
        % (
            i,
            r.get("title"),
            r.get("category") or "未标",
            r.get("word_count"),
        )
        for i, r in enumerate(rows, 1)
    )
    base = f"「{query}」对上好几篇，你说的是哪一篇？回我序号就行：\n{lines}"

    for attempt in range(max(int(tries), 1)):
        prompt = base if attempt == 0 else (
            "没认出你回的是哪篇。回我**序号**（1、2…）就行：\n" + lines
        )
        answer = interrupt(prompt)
        picked = _match_answer(answer, rows)
        if picked is not None:
            return picked
    return None


def _match_answer(answer, rows: list):
    """把用户的回答对成某一行。序号优先，其次按标题包含。"""
    text = str(answer).strip()
    if not text:
        return None
    try:
        idx = int(text) - 1
        if 0 <= idx < len(rows):
            return rows[idx]
    except (TypeError, ValueError):
        pass
    # 标题匹配只做单向包含（回答 ⊆ 标题）：反向包含会让「C」误配「C++」
    for r in rows:
        if text in str(r.get("title")):
            return r
    return None


def _cfg(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        print(f"[introduce] {name}={raw!r} 不是整数，用默认 {default}")
        return default


def _cfg_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        print(f"[introduce] {name}={raw!r} 不是浮点数，用默认 {default}")
        return default


def fetch_content_node(state: IntroduceState) -> dict:
    """按定位到的 id 取正文。不调 LLM。"""
    post = state.get("target_post") or {}
    post_id = post.get("id")
    if not post_id:
        return {}

    try:
        row = fetch_content(int(post_id))
    except DBUnavailable as exc:
        print(f"[introduce] 取正文时连库失败，降级：{exc}")
        return degrade_reply(state)

    if not row:
        # 定位到了但取不到行（并发下架 / 权限）——按降级处理，别硬编
        print("[introduce] 定位到的文章取不到行，按降级处理")
        return degrade_reply(state)

    content = str(row.get("content") or "")
    return {
        "target_post": row,
        "post_content": content,
        "post_toc": row.get("toc"),
        "content_len": int(row.get("content_len") or len(content)),
        "body_missing": not content.strip(),
    }


def pick_tier(content_len: int) -> str:
    """按正文长度分档：short / medium / long。"""
    if content_len <= _cfg("INTRO_FULL_MAX", 5000):
        return "short"
    if content_len <= _cfg("INTRO_MAP_MAX", 50000):
        return "medium"
    return "long"


def validate_intro(text: str, post: dict) -> list:
    """校验答复。返回问题清单，空列表 = 通过。"""
    problems = []
    text = str(text or "")
    if not text.strip():
        problems.append("空回复")
    if "您" in text:
        problems.append("出现了「您」")
    if "**" in text or re.search(r"^\s*\|.*\|\s*$", text, re.M):
        problems.append("出现了 markdown")
    if "http://" in text or "https://" in text:
        problems.append("出现了带域名的链接")

    title = str(post.get("title") or "")
    if title and title not in text:
        problems.append("漏了标题")

    allowed = _to_relative(post.get("blog_url"))
    used = [u.rstrip("）)。,，、") for u in _INTRO_URL_RE.findall(text)]
    if allowed and allowed not in used:
        problems.append("漏了原文链接")
    for u in used:
        if allowed and u != allowed:
            problems.append(f"链接不属于这篇: {u}")
    return problems


def template_intro(post: dict, body_missing: bool = False) -> str:
    """兜底模板：不经过 LLM，篇名与链接逐字来自数据库，绝不可能编。"""
    title = post.get("title") or ""
    url = _to_relative(post.get("blog_url"))
    bits = [f"《{title}》"]
    if post.get("category"):
        bits.append(str(post["category"]))
    if post.get("word_count"):
        bits.append(f"约 {post['word_count']} 字")
    head = " · ".join(bits)
    if body_missing:
        return f"{head}\n我这边只拿到它的摘要，正文没取到——你点进去看吧：\n{url}"
    return f"{head}\n这篇我翻了一遍，正文太长，一句两句说不完，你直接看原文更痛快：\n{url}"


def compose_intro(state: IntroduceState) -> dict:
    """把正文说成爱弥斯的话。只负责说。"""
    post = state.get("target_post") or {}
    content = str(state.get("post_content") or "")
    body_missing = bool(state.get("body_missing"))

    if not post:
        return {
            "messages": [
                AIMessage(
                    content=(
                        "我这边没定位到你说的那篇文章。换个说法？"
                        "比如把篇名说得再准一点，我照着标题找。"
                    )
                )
            ]
        }

    if body_missing:
        return {"messages": [AIMessage(content=template_intro(post, True))]}

    tier = pick_tier(int(state.get("content_len") or len(content)))
    body = content if tier == "short" else _shrink(content, state, tier)

    task = (
        "这一轮你做的是「介绍一篇文章」。下面这篇的标题和正文都给你了，"
        "用你自己的话把它讲给他听。\n"
        f"标题：{post.get('title')}\n"
        f"链接（原样抄，不许改）：{_to_relative(post.get('blog_url'))}\n"
        f"正文：\n{body}\n\n"
        "怎么写：\n"
        "- 先一句话说这篇在讲什么，再说它分哪几块、各自解决什么问题。\n"
        "- 不许原样搬运正文的句子，用你自己的话讲。\n"
        "- 结尾把上面那个链接给出来，让他能点进去看原文。\n"
        "- 不要 markdown 表格，不要加粗，不要写域名，不要用「您」。\n"
        "- 长度：五六句到十来句，别写成一篇文章。"
    )

    rendered = ""
    try:
        response = model.invoke(
            [
                SystemMessage(content=with_persona(task)),
                HumanMessage(content="上面是这篇文章的内容。按规则讲给他听。"),
            ]
        )
        rendered = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[introduce] 介绍生成失败，退模板：{exc}")

    problems = validate_intro(rendered, post)
    if problems:
        print(f"[introduce] 介绍未通过校验，退模板：{problems}")
        rendered = template_intro(post, False)
    return {"messages": [AIMessage(content=rendered)]}


def _shrink(content: str, state: IntroduceState, tier: str) -> str:
    """按档位准备要喂给模型的正文。

    medium：**默认配置下就是把全文给它**（因为 medium 上界 INTRO_MAP_MAX=50000
            小于预算 INTRO_TOKEN_BUDGET×1.5=45000，"超预算"永远不成立）。
            截断分支只在把 INTRO_MAP_MAX 调得比预算更大时才生效 —— 留着是为了
            配置不变式被破坏时不会把整个上下文撑爆，不是为了日常路径。
    long  ：只取开头若干字（+ 目录，如果 blog_content.toc 有值的话 —— 实测该列
            在真实数据里是 NULL，所以这部分目前拿不到东西，long 档实际就是"开头"）。
    """
    if tier == "long":
        head_chars = _cfg("INTRO_LONG_HEAD", 3000)
        head = content[:head_chars]
        toc = state.get("post_toc")
        toc_text = ""
        if toc:
            try:
                toc_text = json.dumps(toc, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):
                toc_text = ""
        return f"{head}\n\n（以下为目录结构）\n{toc_text}" if toc_text else head

    budget = int(_cfg("INTRO_TOKEN_BUDGET", 30000) * 1.5)
    if len(content) <= budget:
        return content
    return content[:budget] + "\n\n（正文过长，以下略）"
