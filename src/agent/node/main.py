from typing import TypedDict

from langchain_core.messages import SystemMessage, filter_messages, HumanMessage
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing_extensions import Literal

from src.agent.common.context import ContextSchema, get_user_id
from src.agent.common.llm import model, model_deterministic
from src.agent.common.persona import with_persona
from src.agent.state.main import State


# 节点：查询持久化信息
def get_store_info(state: State, runtime: Runtime[ContextSchema], *, store: BaseStore):
    # 搜索用户信息
    user_id = get_user_id(runtime)
    namespace = (user_id, "preferences")
    prefs_result = store.search(namespace)
    if prefs_result and prefs_result[0]:
        return {
            "user_preferences": prefs_result[0].value,
        }
    else:
        return {"user_preferences": {}}


class NeedIntroduceOutput(TypedDict):
    introduce_choice: str  # 用户挑的序号（"1"/"2"…），或"不用"
    # 这个状态不会出现在最终状态

class UserMessage(BaseModel):
    type: Literal[
        "recommend_blog", "get_info", "introduce_blog", "other"
    ] = Field(
        description="根据用户问题描述判断问题类型："
        "推荐博客文章（让我推几篇）、获取信息、"
        "介绍某一篇文章（用户点名了具体篇名，如「介绍一下继承这篇文章」，"
        "注意与推荐的区别：推荐是「几篇」，介绍是「一篇/点名」）、其它内容"
    )


# 节点：识别用户意图
def identify_question(state: State):
    # state["messages"] # 用户问题  -》 LLM  -> 结构化输出（type） : 推荐、收藏、我的、其它
    # DeepSeek 不支持 response_format={"type": "json_schema"}（会返回
    # "This response_format type is unavailable now"），必须显式走 tool-calling。
    # 路由要的是稳定，不是文采 —— 用低温度的那个模型。
    user_intent = model_deterministic.with_structured_output(
        UserMessage, method="function_calling"
    ).invoke(
        [
            SystemMessage(
                content="你是一个根据描述提取信息的提取专家。请从用户的描述中提取想要咨询的相关信息。"
                "严谨根据语义推断信息，但是不能猜测或者编造信息。"
            ),
            state["messages"][-1],
        ]
    )
    # 新请求才经过这里；恢复选文中断不会重跑入口。保留 messages 历史。
    return {"user_intent": user_intent.type, "target_query": ""}


def _pick_recommended_post(answer, posts: list):
    """把用户的回答对成候选里的某一篇。只认序号。

    为什么只认序号、不认篇名：用户在推荐清单里看到的就是编号，
    "回我序号就行"是我给的指令。认不出来的话一律当**拒绝** ——
    这是"顺便问一句"，不是必经步骤，再问一遍就成了纠缠。
    （对比：介绍子图里命中多篇时的反问是必经步骤，所以那边会重问一次。）
    """
    try:
        idx = int(str(answer).strip()) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(posts):
        return posts[idx]
    return None


# 节点：推荐跑完后问一句要不要介绍其中一篇
def need_introduce(state: State) -> NeedIntroduceOutput:
    """中断询问要不要挑一篇讲讲。挑中就把它写进 target_query。

    为什么在这里就把篇名解析好：条件边函数**不能改状态**（它只返回分支名），
    而介绍子图需要的是篇名而不是序号 —— 用户答的是"1"。

    为什么键名是 target_query：它要与介绍子图 IntroduceState 的同名字段对上，
    父子图才能自动贯通（子图靠它做"已有篇名就不再抽"的早退）。
    """
    posts = state.get("recommended_posts") or []
    if not posts or state.get("recommend_failed"):
        # 降级 / 一条都没推荐出来 → 没东西可讲，别问
        return {"introduce_choice": "不用", "target_query": ""}

    lines = "\n".join(
        "%d. 《%s》 · %s · 约 %s 字"
        % (
            i,
            p.get("title"),
            p.get("category") or "未标",
            p.get("word_count"),
        )
        for i, p in enumerate(posts, 1)
    )
    answer = interrupt(
        "上面这些，要不要我挑一篇给你讲讲？回序号就行，不用就回「不用」：\n" + lines
    )
    picked = _pick_recommended_post(answer, posts)
    if picked is None:
        return {"introduce_choice": "不用", "target_query": ""}
    # 预填**精确标识**，而不是篇名。
    #
    # 为什么不是 title：篇名会撞名。实测推荐出 id=66《C++》，把 'C++' 填下去后，
    # 介绍子图的 find_by_title 是 `title LIKE '%C++%'`，一次命中 3 篇
    # （《C++基础语法》《C++11 并发编程》《C++》），于是反问用户"你说的是哪一篇"
    # —— 可我们本来就精确知道是哪一篇。撞名是系统自己造成的，不该让用户收拾。
    #
    # blog_url 在库里是唯一的（推荐结果的 blog_url 就是库里原始值），
    # 介绍子图 locate_article 见到 URL 会直接等值取那一篇，绕过模糊匹配。
    # 万一 URL 缺失（理论上不会），退回篇名，行为与改动前一致。
    return {
        "introduce_choice": str(answer).strip(),
        # 键名与子图 IntroduceState.target_query 一致，才能跨父子图传过去。
        "target_query": str(picked.get("blog_url") or picked.get("title") or ""),
    }


# 节点：返回用户偏好信息
def get_user_preferences(state: State):
    # 获取最新历史偏好信息（参考答案）
    prefs = state.get("user_preferences", {})
    # 筛选用户消息（获取到用户问题）
    user_messages = filter_messages(state["messages"], include_types="human")

    result = model.invoke(
        [
            SystemMessage(
                content=with_persona(
                    "结合用户的历史偏好回答他的问题。"
                    "如果有的偏好数据为空，不要猜测或编造数据。"
                    "不要直接把偏好数据念出来，要结合问题自然地说。"
                    "如果问题与偏好数据无关，正常回答即可。"
                )
            ),
            HumanMessage(
                content="用户的历史偏好信息如下"
                f"1. 最少字数：{prefs.get('word_count_min')}"
                f"2. 最多字数：{prefs.get('word_count_max')}"
            ),
            user_messages[-1],# 问题
        ]
    )
    return {
        "messages": [result]
    }
