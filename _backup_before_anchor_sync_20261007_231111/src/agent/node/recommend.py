import ast
import os
import re
import uuid
from typing import Optional

from dotenv import load_dotenv
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.tools import QuerySQLDatabaseTool
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import (
    filter_messages,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    AIMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from structlog.tracebacks import extract

from src.agent.common.context import ContextSchema, get_user_id
from src.agent.common.db import (
    DBUnavailable,
    db_uri as _db_uri,
    get_db,
    safe_err as _safe_err,
)
from src.agent.common.llm import model, model_deterministic
from src.agent.common.persona import with_persona
from src.agent.common.reply import (
    DB_UNAVAILABLE_REPLY as _DB_UNAVAILABLE_REPLY,
    degrade_reply as _degrade_reply,
    pending_tool_call_ids as _pending_tool_call_ids,
)
from src.agent.common.store import UserPreferences
from src.agent.state.recommend import RecommendState, get_recommend_info


class UserInfo(BaseModel):
    """用户的博客阅读需求信息"""

    topic: Optional[str] = Field(
        default=None,
        description="用户想要阅读的博客领域/分类，例如：人工智能、前端开发、数据库、职场成长",
    )
    category: Optional[str] = Field(
        default=None,
        description="博客栏目/专栏名称，例如：LangGraph实战、源码剖析、每周随笔、面试笔记",
    )
    word_count_min: Optional[int] = Field(
        default=None,
        description="用户能接受的最少字数，单位为字。如果是xx字以内，要设置最小值为0",
    )
    word_count_max: Optional[int] = Field(
        default=None,
        description="用户能接受的最多字数，单位为字。如果是xx字以上，最大值设置为20000",
    )
    blog_type: Optional[str] = Field(
        default=None,
        description="文章类型，例如：教程、实战、原理剖析、随笔、踩坑记录、工具介绍",
    )
    tags: Optional[str] = Field(
        default=None,
        description="文章标签，例如：LangGraph、RAG、Agent、Docker、性能优化",
    )
    blog_count: Optional[int] = Field(
        default=None, description="需要推荐的博客文章数量"
    )
    others: Optional[str] = Field(
        default=None,
        description="特殊要求，例如：带代码示例、有架构图、附参考文献、适合新手、有demo仓库等",
    )


def collect_user_info(
    state: RecommendState, runtime: Runtime[ContextSchema], *, store: BaseStore
):
    """收集用户希望的推荐信息"""

    # 1. 获取需要被解析的数据：最新的用户消息 + 用户的偏好数据
    # 场景1：
    # 最新的用户消息： 人工智能 3篇
    # 用户的偏好数据： 字数 1000-2000字
    user_messages = filter_messages(state["messages"], include_types=["human"])
    pref = state.get("user_preferences")
    if pref and (pref.get("word_count_min") or pref.get("word_count_max")):
        # 偏好中包含最少和最多字数
        extract_messages = [
            HumanMessage(
                content="用户的历史偏好信息如下："
                f"1. 最少字数：{pref.get('word_count_min')}"
                f"2. 最多字数：{pref.get('word_count_max')}"
            ),
            user_messages[-1],
        ]
    else:  # 无偏好数据
        extract_messages = user_messages[-1]

    # 2. 提取信息(LLM结构化返回)
    # 拓展：将信息与数据库表中的字段进行映射
    def extract_info(messages) -> UserInfo:
        system_message = SystemMessage(
            content="""
你是一个博客阅读需求信息提取专家。请从用户的描述与历史信息中提取博客阅读相关信息。
如果用户历史偏好信息与最新用户消息冲突，以最新的用户消息为主。
只提取用户明确提到的信息，不要猜测或推断。
如果某个信息用户没有提到，就返回null。
注意字数的单位可能是字、千字、万字等，请统一转换为字。
如果用户提到篇幅范围，请分别提取最少字数和最多字数。
如果用户提到推荐几篇文章，提取blog_count字段。"""
        )
        if not isinstance(messages, list):
            # extract_messages 可能是单条消息（如用户补充的回答），统一成列表
            messages = [messages]
        return model_deterministic.with_structured_output(
            schema=UserInfo, method="function_calling"
        ).invoke(
            [system_message, *messages]
        )

    # 更新状态函数
    def update_state(current_state: dict, info: UserInfo) -> dict:
        if not info:
            return current_state

        user_info_dict = info.model_dump(exclude_none=True)
        current_state.update(user_info_dict)
        return current_state

    # 根据历史偏好和用户消息提取消息
    updated_state = {}
    extracted_info = extract_info(extract_messages)
    updated_state = update_state(updated_state, extracted_info)

    # 3. 中断咨询推荐的必须参数
    # 场景2：
    # 最新的用户消息：给我推荐几篇博客（并未表明推荐领域，模糊推荐）
    # 询问用户意向领域

    # 检查是否缺失关键信息: 领域、字数范围
    missing_info = []
    if not updated_state.get("topic"):
        missing_info.append("方向（想看哪一块）")
    if (
        updated_state.get("word_count_min") is None
        or updated_state.get("word_count_max") is None
    ):
        missing_info.append("篇幅（大概多少字、能看多久）")

    if missing_info:
        # 中断文案用爱弥斯口吻：不用「您」，不是客服话术。
        # 「不提供」这个约定词必须原样保留 —— 下面就是靠它判断用户是否放弃补充。
        prompt = f"行，我去翻。不过你还没说清：{'；'.join(missing_info)}。\n"
        prompt += "不想说也行，回我「不提供」——我按你说的范围挑，挑完你再嫌。"
        # 根据缺失的信息进行中断
        answer = interrupt(prompt)
        if str(answer).strip() == "不提供":
            # 用户放弃补充。
            #
            # 字数**不设默认值**：没提就是不筛。曾经这里硬塞 500-5000，
            # 结果 category='八股文' 的 12 篇里有 11 篇（1.5万~17万字）被筛掉，
            # 用户只拿到 1 篇，看起来像"召回坏了"。
            # 方向仍要给个占位（后续提示词里要用到它）。
            if not updated_state.get("topic"):
                updated_state["topic"] = "随机领域"
            if not updated_state.get("blog_count"):
                updated_state["blog_count"] = 5
        else:
            # 缺失关键信息，但用户已经补充
            # 将answer构建为HumanMessage
            user_response_msg = HumanMessage(content=str(answer))
            extracted_info = extract_info(user_response_msg)
            updated_state = update_state(updated_state, extracted_info)

    # 4. 持久化处理：更新字数偏好
    # 场景3：
    # 最新的用户消息：人工智能 3篇  字数0-5000字
    # 用户的偏好数据：字数 1000-2000字
    if updated_state.get("word_count_min") or updated_state.get("word_count_max"):
        # 有可能会更新
        user_id = get_user_id(runtime)
        namespace = (user_id, "preferences")
        prefs_result = store.search(namespace)
        if len(prefs_result) == 0:
            # 新增
            prefs = UserPreferences(
                word_count_min=updated_state.get("word_count_min"),
                word_count_max=updated_state.get("word_count_max"),
            )
            store.put(namespace, str(uuid.uuid4()), prefs.model_dump(exclude_none=True))
            updated_state["user_preferences"] = prefs.model_dump(exclude_none=True)
        else:
            prefs = prefs_result[0].value
            store_min = prefs.get("word_count_min")
            store_max = prefs.get("word_count_max")
            cur_min = updated_state.get("word_count_min")
            cur_max = updated_state.get("word_count_max")
            update_min = False  # 是否更新最少字数
            update_max = False  # 是否更新最多字数
            if store_min is not None and cur_min is not None and cur_min < store_min:
                # 都不为空，就比较
                update_min = True
            elif store_min is None and cur_min is not None:
                # store 没有，但 cur 有
                update_min = True

            if store_max is not None and cur_max is not None and cur_max > store_max:
                update_max = True
            elif store_max is None and cur_max is not None:
                update_max = True

            if update_min or update_max:
                if update_min:
                    prefs["word_count_min"] = cur_min
                if update_max:
                    prefs["word_count_max"] = cur_max

                store.put(
                    namespace,
                    prefs_result[0].key,  # 根据查询到的key进行更新
                    prefs,
                )
                updated_state["user_preferences"] = prefs

    # 5. 准备最终的消息，并更新
    updated_state["messages"] = [
        HumanMessage(content=get_recommend_info(updated_state))
    ]
    print(
        f"已收集用户信息：\n领域：{updated_state.get('topic')}"
        f"栏目：{updated_state.get('category')}"
        f"字数：{updated_state.get('word_count_min')}-{updated_state.get('word_count_max')}字"
        f"文章数：{updated_state.get('blog_count')}"
    )

    # 返回：覆盖了推荐参数和偏好数据，更新了消息列表
    return updated_state


# ============================================================================
# 数据库：懒加载 + 失败降级
# ----------------------------------------------------------------------------
# 为什么改：这里原来在【导入期】就执行 SQLDatabase.from_uri(...)。
# langchain 的 SQLDatabase.__init__ 会 inspect(self._engine)，也就是**当场握手**
# （见 langchain_community/utilities/sql_database.py:106）。
# 于是数据库抖一下、或库名写错，import 就抛 OperationalError ——
# langgraph 连图都建不起来，连「在吗」这种纯闲聊也一起挂。
# 取数只是推荐路径里的一步，不该有拖垮全部能力的话语权。
# 现在：第一次真正要用时才连；连不上抛 DBUnavailable，
# 由各节点接住并降级成一句人设化的提示（见 persona.txt 5.1）。
#
# 库名走 DB_NAME 环境变量，所以可以临时指向影子库/不存在的库做验证。
# ============================================================================

# 数据库不可用时代替推荐结果说的话：实现已抽到 common/reply.py，
# 这里保留同名引用，调用方与测试都按原名使用。
DB_UNAVAILABLE_REPLY = _DB_UNAVAILABLE_REPLY

# 推荐答复只认这五列，且顺序固定 —— 与 generate_query 里的取数列约定一致。
RECO_COLUMNS = ("title", "category", "word_count", "difficulty", "blog_url")

# 站内文章总数，是个事实常量；它不算「本轮结果集的条数」。
LIB_TOTAL = 133


# 数据库连接与错误脱敏的实现已抽到 common/db.py（介绍模块也要用，
# 但它不该反向 import 本模块）。_tools_singleton 是本模块自己的缓存。
_tools_singleton: Optional[list] = None
_tool_node_singleton: dict = {}


def get_sql_tools() -> list:
    """SQL 工具集，同样懒加载。"""
    global _tools_singleton
    if _tools_singleton is None:
        _tools_singleton = SQLDatabaseToolkit(db=get_db(), llm=model).get_tools()
    return _tools_singleton


def _tool(name: str):
    return next(tool for tool in get_sql_tools() if tool.name == name)


def _tool_node(cache_key: str, tool_name: str) -> ToolNode:
    """ToolNode 也懒建 —— 它的构造要拿到已经连上的 db。

    handle_tool_errors=False：让取数失败抛出来，交给下面的包装函数降级。
    默认值会把异常吞成一条 ToolMessage，图会带着坏数据继续往下走。
    """
    if cache_key not in _tool_node_singleton:
        _tool_node_singleton[cache_key] = ToolNode(
            [_tool(tool_name)], name=cache_key, handle_tool_errors=False
        )
    return _tool_node_singleton[cache_key]


def _degraded(state: RecommendState) -> dict:
    """统一的降级返回（实现已抽到 common/reply.py）。

    关键行为不变：**顺带把悬空的 tool_call 应答掉**（见 pending_tool_call_ids）。
    没有这一步，降级就会把消息序列弄坏，下一个 LLM 节点必然 400 ——
    那时候用户看到的是一坨 traceback，而不是这句人设化的提示。

    这里保留原名与原签名，是为了不动调用方，也不动
    tests/unit_tests/test_recommend_degrade.py（它直接导入这个名字）。
    """
    return _degrade_reply(state, mark="recommend_failed")


# 节点：获取表信息（ToolNode 的降级包装）
def get_schema_node(state: RecommendState):
    try:
        return _tool_node("get_schema", "sql_db_schema").invoke(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 取表结构失败，降级：{_safe_err(exc)}")
        return _degraded(state)


# 节点：执行sql查询（ToolNode 的降级包装）
def run_query_node(state: RecommendState):
    try:
        return _tool_node("run_query", "sql_db_query").invoke(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 执行查询失败，降级：{_safe_err(exc)}")
        return _degraded(state)


# 节点：获取全量表（连不上库就降级，让图能收场而不是抛异常）
def list_tables(state: RecommendState):
    try:
        # 1. 获取AIMessage(tool_calls)
        tool_call = {
            "name": "sql_db_list_tables",
            "args": {},
            "id": "123123",
            "type": "tool_call",
        }
        # 模拟必定调用工具
        tool_call_message = AIMessage(content="", tool_calls=[tool_call])

        # 2. 手动调用工具：sql_db_list_tables
        list_tables_tool = _tool("sql_db_list_tables")
        tool_message = list_tables_tool.invoke(tool_call)
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 列表面失败，降级：{_safe_err(exc)}")
        return _degraded(state)

    # 整合结果
    response = AIMessage(content=f"可用的表:{tool_message.content}")
    return {"messages": [tool_call_message, tool_message, response]}


# 节点：绑定工具（获取表信息），让LLM将来必定执行工具节点
def call_get_schema(state: RecommendState):
    try:
        get_schema_tool = _tool("sql_db_schema")
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 取表结构工具不可用，降级：{_safe_err(exc)}")
        return _degraded(state)
    llm_with_tools = model.bind_tools([get_schema_tool], tool_choice="any")
    # llm 根据历史消息筛选表，并获取需要的表信息
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


# 节点：生成SQL
# 注意：这个节点**只负责取数**，不负责「说给用户听」。
# 答复由 compose_answer 用人设重新组织 —— 见下面的说明。
def generate_query(state: RecommendState):
    # ========================================================================
    # 【向量召回】先按"意思"挑出候选 id，拼一条只含 id 条件的 SQL 交下去。
    #   命中  → 直接返回 AIMessage(tool_call)，后面的 check_query / run_query /
    #           compose_answer 原样工作（SQL 是真 SQL，ToolMessage 也是真的）
    #   没命中 / 索引没建 / 接口挂 → 往下走，用原来那套 LLM 写 SQL
    # 下面原有代码一个字未改。
    #
    # 为什么这么接：向量只负责"挑哪几篇"，取字段和字数过滤仍然交给数据库，
    # 图、state、compose_answer 一个都不用动。
    # ========================================================================
    from src.agent.common import retrieval  # 局部 import：它坏了也不影响本模块加载

    query_text = " ".join(
        str(state.get(f) or "")
        for f in ("topic", "category", "tags", "blog_type", "others")
    ).strip()
    # 查询串重复一遍再喂给向量。为什么：
    # 用户只说一个分类名时（"推荐几篇八股文"→ topic="八股文"），单次出现的
    # 查询在向量空间里"锚点太弱"：top1 只有 0.562，刚好过 0.50 的门，
    # 但 max(0.45, top1-0.15)=0.45 的尾巴把其余候选全砍掉，只召回 1 篇。
    # 实测同一串重复一遍：top1 升到 0.668，召回从 1 篇变 5 篇；
    # "Linux" 从 0 篇（被门槛拦）变 6 篇；"网络" 从 3 篇变 7 篇。
    # 长查询（"C++ 智能指针"这类）本来召回就满，重复不影响。
    _vec_query = (query_text + " " + query_text).strip() if query_text else ""

    if query_text:
        # 只在【第一轮】走向量近路。
        #
        # 为什么必须加这道闸：下面的早返回会直接产出 tool_call，而
        # should_continue 看到 tool_calls 就路由到 check_query → run_query →
        # 又回到本节点。run_query 成功时 db_ready=ok 也回到本节点，于是
        # 第二轮又命中同一条向量、又早返回同一条 SQL —— 图永远走不到
        # compose_answer（那个节点才是让 should_continue 收到"无 tool_calls"
        # 从而刹车的终点），表现为无限循环。
        #
        # 判据：消息里已经出现过 sql_db_query 的 ToolMessage（即上一轮在
        # list_tables 的 sql_db_list_tables 之后真的查过库）→ 这一轮交给下面
        # 的 LLM 链路收尾。降级时 ToolMessage 的正文是 DB_UNAVAILABLE_REPLY。
        # 不再查第二次向量，也省掉一次 embedding API 调用。
        _already_queried = any(
            isinstance(msg, ToolMessage)
            and (
                (getattr(msg, "name", None) or "") == "sql_db_query"
                or str(msg.content or "").startswith(DB_UNAVAILABLE_REPLY)
            )
            for msg in (state.get("messages") or [])
        )
        if _already_queried:
            print("[recommend] 本轮已查过库，跳过向量召回，交给 LLM 链路收尾")
        try:
            hits = None if _already_queried else retrieval.search_ids(
                _vec_query, k=state.get("blog_count") or retrieval.RECALL_TOP_K
            )
        except Exception as exc:  # noqa: BLE001 召回只是加分项，不许把推荐拖垮
            print(f"[recommend] 向量召回异常，回落 SQL：{_safe_err(exc)}")
            hits = None
        if hits:
            conds = ["id IN (%s)" % ", ".join(str(i) for i, _ in hits)]
            if state.get("word_count_min"):
                conds.append("word_count >= %d" % int(state["word_count_min"]))
            if state.get("word_count_max"):
                conds.append("word_count <= %d" % int(state["word_count_max"]))
            sql = (
                "SELECT title, category, word_count, difficulty, blog_url "
                "FROM blog WHERE " + " AND ".join(conds)
            )
            print(f"[recommend] 向量召回 {len(hits)} 篇 → {sql}")
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "sql_db_query",
                                "args": {"query": sql},
                                "id": "vec-" + uuid.uuid4().hex[:8],
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }

    generate_query_system_prompt = """
    你是一个只负责取数的 SQL 代理。
    给定一个输入问题，创建一个语法正确的{dialect}查询来运行。
    需要根据rows from table的示例设置真实查询的值。
    除非用户指定了他们希望获得的特定数量的示例，否则始终将查询限制为最多{top_k}个结果。
    你可以按相关列对结果排序，以返回最感兴趣的结果。不要查询特定表中的所有列，只查询给定问题的相关列。
    不要对数据库做任何DML语句（INSERT， UPDATE， DELETE， DROP等)。

    【取数列约定（必须严格遵守）】
    查询 blog 表时，SELECT 列表必须恰好是下面这五列，顺序也不能变：
        title, category, word_count, difficulty, blog_url
    不要加 id、intro、tags 等其它列 —— 下游要靠列顺序解析结果。

    【查不到就空着，绝对不要擅自放宽】
    如果按用户给出的主题 / 关键词筛选之后一行都没有，就让它空着。
    不要把 WHERE 里的主题条件删掉、换一批不相干的文章顶上。
    「这个方向现在没有」是一个合格答案，后面的节点会如实告诉用户；
    用不相干的文章冒充结果，才是这里最严重的错误。
    用户明确给出的主题词，在整轮查询里都不许丢。

    【重要】你不要替用户组织语言、不要写推荐语、不要解释结果。
    取完数就收手（不再调用工具），由后面的节点把结果说给用户听。
            """
    try:
        dialect = get_db().dialect
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 生成查询前连库失败，降级：{_safe_err(exc)}")
        return _degraded(state)
    system_prompt = generate_query_system_prompt.format(
        dialect=dialect, top_k=state.get("blog_count", 5)
    )
    system_message = SystemMessage(content=system_prompt)
    # 取数要的是稳定，不是文采 —— 固定用低温度的那个模型。
    llm_with_tools = model_deterministic.bind_tools([_tool("sql_db_query")])
    try:
        response = llm_with_tools.invoke([system_message] + state["messages"])
    except Exception as exc:  # noqa: BLE001
        # 调不动模型（网络 / 限流 / 上游报错）也走降级：用户该看到那句人设话，
        # 而不是一坨 traceback。_degraded 会顺带把在飞的 tool_call 应答掉，
        # 消息序列不会因此坏掉。
        print(f"[recommend] 生成查询失败，降级：{_safe_err(exc)}")
        return _degraded(state)
    return {"messages": [response]}

    # QuerySQLDatabaseTool
    # tool_call: {
    #     "name": sql_db_query  # 运行sql
    #     "args": {"query": "select ....."}              # sql
    #     "id":
    # }


def check_query(state: RecommendState):
    # 走到这个节点，上一个节点必然是generate_query，并且最新的消息必然是AIMessage(tool_call)

    check_query_system_prompt = """
你是一个非常注重细节的SQL专家。仔细检查{dialect}查询中的常见错误，包括：
-使用NULL值的NOT IN
-在应该使用UNION ALL时使用UNION
-使用BETWEEN表示独占范围
-谓词中的数据类型不匹配
-正确引用标识符
-使用正确数量的函数参数
-转换为正确的数据类型
-使用合适的列进行连接
另外必须保留 SELECT 的五列约定：title, category, word_count, difficulty, blog_url（顺序不变）。
如果存在上述任何错误，请重写查询。如果没有错误，只需复制原始查询即可。
在运行此检查之后，您将调用适当的工具来执行查询。
        """.format(dialect=get_db().dialect)
    system_message = SystemMessage(content=check_query_system_prompt)
    # 将SQL当作用户消息传入进行检查
    tool_call = state["messages"][-1].tool_calls[0]
    # tool_call["args"]["query"] 就能拿到SQL
    # tool_call: {
    #     "name": sql_db_query  # 运行sql
    #     "args": {"query": "select ....."}              # sql
    #     "id":
    # }
    user_message = HumanMessage(content=tool_call["args"]["query"])
    llm_with_tools = model_deterministic.bind_tools(
        [_tool("sql_db_query")], tool_choice="any"
    )
    response = llm_with_tools.invoke([system_message,user_message])
    # 带tool_calls的AIMessage
    # 目前最新的一个消息是AI(t)，现在又生成了一个AI(t)，则可以将两个合成一个
    response.id = state["messages"][-1].id
    return {"messages": [response]}


# ============================================================================
# 节点：把人设 + 查询结果说成爱弥斯的话
# ----------------------------------------------------------------------------
# 为什么要有这个节点：在它之前，用户看到的推荐是 generate_query 直接产出的 ——
# 那句系统提示词写的是「你是一个与 SQL 数据库交互的代理」，于是答复必然是
# 客服腔，没有半点爱弥斯。SQL 应该只取数；说话是这里的事。
#
# 硬约束（这个节点存在的全部理由）：
#   1. 篇数 / 标题 / 链接**逐字来自查询结果**，一个字都不许自己编；
#   2. 链接只给站内相对路径 /posts/.../ —— 库里存的是带域名的完整 URL，
#      这里必须把域名剥掉；
#   3. 不许 markdown 表格、不许加粗、不许出现「您」；
#   4. 说「共 N 篇」就必须**正好列 N 条**，校验不过就退到模板兜底。
#      上一轮实测的 bug 就是「说 4 篇、列了 5 行，还编了个理由」，所以必须校验。
# ============================================================================

_URL_RE = re.compile(r"/posts/[A-Za-z0-9_\-./]*")
_COUNT_RE = re.compile(r"(\d+)\s*篇")


def _to_relative(url) -> str:
    """库里存的是 https://<域名>/posts/.../ ，气泡里只能给站内相对路径。"""
    if not url:
        return ""
    path = re.sub(r"^https?://[^/]+", "", str(url).strip())
    if not path.startswith("/"):
        path = "/" + path
    return path


def _parse_rows(raw) -> Optional[list]:
    """把 SQL 工具返回的原始文本解析成行字典；解析不了返回 None（不猜）。

    注意空串：SQLDatabase.run 在「语句合法但零行」时返回的是空字符串。
    那代表**查到了，只是没有匹配**，不是「连不上」，更不是「解析失败」——
    这两种结果对用户说的话完全不一样，不能混。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text == "":
        return []
    if not text.startswith("["):
        return None
    try:
        data = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(data, (list, tuple)):
        return None
    rows = []
    for item in data:
        if isinstance(item, dict):
            rows.append(item)
        elif isinstance(item, (list, tuple)):
            rows.append(dict(zip(RECO_COLUMNS, item)))
        else:
            return None
    return rows


def _collect_posts(state: RecommendState) -> Optional[list]:
    """取最后一次 SQL 查询的结果，规整成文章列表。

    返回 None = 没拿到可解析的查询结果；返回 [] = 拿到了但一条都不匹配。
    这两种情况必须分开说 —— 前者不能说成「站里没有」。
    """
    tool_msgs = filter_messages(state["messages"], include_types=["tool"])
    if not tool_msgs:
        return None
    rows = _parse_rows(tool_msgs[-1].content)
    if rows is None:
        return None
    posts = []
    for row in rows:
        title = row.get("title")
        url = _to_relative(row.get("blog_url"))
        if not title or not url:
            continue
        posts.append(
            {
                "title": str(title),
                "category": str(row.get("category") or ""),
                "word_count": row.get("word_count"),
                "difficulty": row.get("difficulty"),
                "blog_url": url,
            }
        )
    return posts


def _validate(rendered: str, posts: list) -> list:
    """校验答复。返回问题清单，空列表 = 通过。"""
    problems = []
    if re.search(r"^\s*\|.*\|\s*$", rendered, re.M):
        problems.append("出现了 markdown 表格")
    if "**" in rendered:
        problems.append("出现了加粗星号")
    if re.search(r"^\s*#{1,6}\s", rendered, re.M):
        problems.append("出现了 markdown 标题")
    if re.search(r"https?://", rendered, re.I):
        problems.append("出现了带域名的链接")
    if "您" in rendered:
        problems.append("出现了「您」")
    if re.search(r"\[[^\]]+\]\([^)]+\)", rendered):
        problems.append("出现了 markdown 链接语法")

    allowed = [post["blog_url"] for post in posts]
    used = []
    for url in _URL_RE.findall(rendered):
        url = url.rstrip("）)。,，、")
        if url and url not in used:
            used.append(url)
    for url in used:
        if url not in allowed:
            problems.append(f"链接不在查询结果里: {url}")
    if not used:
        problems.append("一条链接都没给")

    # 每篇都必须标题 + 链接成对出现（防止标题和链接错配、或漏掉某条）
    for post in posts:
        if post["title"] not in rendered:
            problems.append(f"漏了标题: {post['title']}")
        elif post["blog_url"] not in rendered:
            problems.append(f"漏了链接: {post['blog_url']}")

    if len(used) != len(posts):
        problems.append(f"列了 {len(used)} 条，查询结果是 {len(posts)} 条")

    # 「N 篇」必须等于实际列出的条数（133 是站内总数常量，不算本轮计数）
    for match in _COUNT_RE.finditer(rendered):
        n = int(match.group(1))
        if n == LIB_TOTAL:
            continue
        if n != len(posts):
            problems.append(f"说「{n} 篇」但查询结果是 {len(posts)} 条")
            break
    return problems


def _template_reply(posts: list) -> str:
    """兜底模板：不经过 LLM，所以篇数 / 标题 / 链接一定是对的。"""
    lines = [f"给你翻到 {len(posts)} 篇，就这几篇："]
    for post in posts:
        bits = [f"《{post['title']}》"]
        if post["category"]:
            bits.append(post["category"])
        if post["word_count"]:
            bits.append(f"约 {post['word_count']} 字")
        lines.append(" · ".join(bits))
        lines.append(post["blog_url"])
    return "\n".join(lines)


def _noisy_sql_summary(state: RecommendState):
    """找到 SQL 代理自己写的那句总结 —— 我们要把它从消息流里删掉。

    位置：最后一条 tool 消息之后，第一条「有正文、没有 tool_calls」的 AIMessage。
    它长的样子是「已取数完成」或者一张 markdown 表格 —— SQL 只该取数，
    这些打字不该出现在用户眼前（表格更是人设明令禁止的）。
    没有就返回 None，不要硬找。
    """
    messages = state["messages"]
    last_tool_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage):
            last_tool_idx = i
    for msg in messages[last_tool_idx + 1:]:
        if (
            isinstance(msg, AIMessage)
            and not msg.tool_calls
            and str(msg.content or "").strip()
        ):
            return msg
    return None


def _reply(state: RecommendState, text: str) -> dict:
    """统一出口：先删掉 SQL 代理的总结，再放上爱弥斯说的话。"""
    outgoing: list = []
    noisy = _noisy_sql_summary(state)
    if noisy is not None and getattr(noisy, "id", None):
        outgoing.append(RemoveMessage(id=noisy.id))
    outgoing.append(AIMessage(content=text))
    return {"messages": outgoing}


_EMPTY_TASK = (
    "这一轮你做的是「库里没有」：检索结果是 0 条，站里确实没有他要的那个方向。\n"
    "用你自己的口气把这件事告诉他。要求：\n"
    "- 直说没有。不含糊，不道歉，也不要解释成「我找不到」。\n"
    "- 一条文章都不许提：**不许出现任何标题（《》）**，**不许出现任何链接（/posts/）**。\n"
    "- 不许说「我记得好像有一篇」这种话。\n"
    "- 2~3 句。最后给一个替代方向（换个说法 / 放宽条件 / 站里相近的主题）。\n"
    "- 不要 markdown，不要加粗，不要用「您」。"
)

# 兜底：不经过 LLM，所以绝不可能编出标题或链接。
_FALLBACK_EMPTY = (
    f"没有诶。{LIB_TOTAL} 篇我全翻了一遍，这个方向一条都没有。\n"
    "这个我不编——编了你更亏。\n"
    "……换个说法？或者把范围放宽一点，我再翻一遍。"
)


def _empty_reply() -> str:
    """空结果也要说人话：走人设，但用校验把「编造」这条路彻底堵死。

    空结果是最容易出事的地方（模型很想"给点什么"），所以宁可退模板：
    只要回答里冒出标题、链接、markdown 或「您」，一律丢弃。
    """
    text = ""
    try:
        response = model.invoke(
            [
                SystemMessage(content=with_persona(_EMPTY_TASK)),
                HumanMessage(content="检索结果：0 条。"),
            ]
        )
        text = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 空结果答复生成失败，退模板：{_safe_err(exc)}")

    problems = []
    if not text.strip():
        problems.append("空回复")
    if "/posts/" in text:
        problems.append("出现了链接")
    if "《" in text or "》" in text:
        problems.append("出现了文章标题")
    if "您" in text:
        problems.append("出现了「您」")
    if "**" in text or re.search(r"^\s*\|.*\|\s*$", text, re.M):
        problems.append("出现了 markdown")
    if problems:
        print(f"[recommend] 空结果答复未通过校验，退模板：{problems}")
        return _FALLBACK_EMPTY
    return text


def compose_answer(state: RecommendState):
    """节点：只负责「说话」——把人设 + 查询结果说成爱弥斯的话。"""
    if state.get("recommend_failed"):
        return _reply(state, DB_UNAVAILABLE_REPLY)

    tool_msgs = filter_messages(state["messages"], include_types=["tool"])
    if not tool_msgs:
        # 没走到取数就收手了 —— 不许编，如实说，并留一个接口。
        return _reply(
            state,
            "我还没拿到结果就停住了。你再问我一次，"
            "或者把方向说具体一点——比如「讲 Linux 进程的，短一点的」。",
        )

    rows = _parse_rows(tool_msgs[-1].content)
    posts = _collect_posts(state)

    if rows is None or posts is None:
        # 结果解析不了 —— 别假装「133 篇里没有」，那是另一回事。
        return _reply(state, DB_UNAVAILABLE_REPLY)

    if not rows:
        # 空结果也走人设，但用 _empty_reply 的校验兜住"编造"
        return _reply(state, _empty_reply())

    if not posts:
        return _reply(
            state,
            "查到了一些条目，但没取到链接。我不敢凭标题给你拼路径——"
            "你稍后再问我一次。",
        )

    data_block = "\n".join(
        "{i}. 标题={t} | 分类={c} | 字数={w} | 难度={d} | 链接={u}".format(
            i=i,
            t=post["title"],
            c=post["category"] or "未标",
            w=post["word_count"],
            d=post["difficulty"],
            u=post["blog_url"],
        )
        for i, post in enumerate(posts, 1)
    )
    task = (
        "这一轮你做的是「推荐文章」。下面是检索结果，共 {n} 条，"
        "每一条都必须出现在你的回答里：\n{data}\n\n"
        "怎么写：\n"
        "- 先用一句你自己的话起头，不要「以下是推荐结果」这种腔。\n"
        "- 然后按上面的顺序逐条写：一行《标题》· 分类 · 约 X 字，"
        "下一行给链接（就用上面给的链接，原样复制，它已经是不带域名的站内路径）。\n"
        "- 每条可以补一句你自己的判断：这篇适合谁、要不要留出时间。\n"
        "- 说「{n} 篇」就必须正好 {n} 条，不许增、不许减、不许编。\n"
        "- 不要 markdown 表格，不要加粗，不要写域名，不要用「您」。\n"
        "- 不要在回答里提站内文章总数，只讲这一轮的 {n} 条。\n"
        "- 长度：引子一句 + 条目 + 收尾一句，别写成一篇文章。"
    ).format(n=len(posts), data=data_block)

    rendered = ""
    try:
        response = model.invoke(
            [
                SystemMessage(content=with_persona(task)),
                HumanMessage(content="上面是这一轮的检索结果。按规则说给他听。"),
            ]
        )
        rendered = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 人设化答复生成失败，退模板：{_safe_err(exc)}")

    problems = _validate(rendered, posts)
    if problems:
        # 校验不过就退模板：宁可少一点人味，也不能给出错的篇数 / 标题 / 链接。
        print(f"[recommend] 人设化答复未通过校验，退模板：{problems}")
        rendered = _template_reply(posts)

    # 顺手把候选写进共享状态：父图跑完推荐后要拿它问「要不要我讲讲其中一篇」。
    # posts 已经在手上，不需要额外查询。
    return dict(_reply(state, rendered), recommended_posts=posts)
