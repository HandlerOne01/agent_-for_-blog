from langchain_core.messages import SystemMessage, filter_messages, HumanMessage
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing_extensions import Literal

from src.agent.common.context import ContextSchema, get_user_id
from src.agent.common.llm import model, model_deterministic
from src.agent.common.persona import with_persona
from src.agent.state.main import State, NeedCollectOutput


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


class UserMessage(BaseModel):
    type: Literal["recommend_blog", "collect_blog", "get_info", "other"] = Field(
        description="根据用户问题描述判断问题类型：推荐博客文章、收藏博客文章、获取信息、其它内容"
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
    return {"user_intent": user_intent.type}


# 节点：中断询问是否需要帮助收藏博客文章
def need_collect(state: State) -> NeedCollectOutput:
    # 中断文案用爱弥斯口吻：不用「您」，不是客服话术。
    # 「需要 / 不需要」这两个约定词必须原样保留 ——
    # graph.py 的 should_collect 就是靠 collect == "需要" 判断要不要进收藏子图。
    prompt = "给你放了几篇在下面。要不要我收进本子？\n"
    prompt += "要收就回我「需要」，不用就回「不需要」。\n"
    prompt += "（这两个词我认得准；回别的话，我这边就当你没说。）"
    answer = interrupt(prompt)
    return {"collect": answer}  # 条件边获取到后，是否执行收藏子图


# 节点：返回用户偏好信息
def get_user_preferences(state: State):
    # 获取最新历史偏好信息（参考答案）
    prefs = state.get("user_preferences", {})
    # 筛选用户消息（获取到用户问题）
    user_messages = filter_messages(state["messages"], include_types="human")
    collected_info = prefs.get("collect_info", {})
    if collected_info:
        # 有收藏过的信息
        collected_str = "\n"
        for i, item in enumerate(collected_info, 1):
            collected_str += (
                f"{i}. 收藏单ID: {item.get('collect_id')}，"
                f"文章标题：{item.get('title')}，"
                f"文章作者：{item.get('author')}\n"
            )
    else:
        # 没有收藏
        collected_str = "无"

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
                f"3. 已收藏过的信息：{collected_str}"
            ),
            user_messages[-1],# 问题
        ]
    )
    return {
        "messages": [result]
    }
