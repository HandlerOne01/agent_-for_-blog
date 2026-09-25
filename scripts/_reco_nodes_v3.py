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
        return _degraded()

    # 整合结果
    response = AIMessage(content=f"可用的表:{tool_message.content}")
    return {"messages": [tool_call_message, tool_message, response]}


# 节点：绑定工具（获取表信息），让LLM将来必定执行工具节点
def call_get_schema(state: RecommendState):
    try:
        get_schema_tool = _tool("sql_db_schema")
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 取表结构工具不可用，降级：{_safe_err(exc)}")
        return _degraded()
    llm_with_tools = model.bind_tools([get_schema_tool], tool_choice="any")
    # llm 根据历史消息筛选表，并获取需要的表信息
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


# 节点：生成SQL
# 注意：这个节点**只负责取数**，不负责「说给用户听」。
# 答复由 compose_answer 用人设重新组织 —— 见下面的说明。
def generate_query(state: RecommendState):
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

    【重要】你不要替用户组织语言、不要写推荐语、不要解释结果。
    取完数就收手（不再调用工具），由后面的节点把结果说给用户听。
            """
    try:
        dialect = get_db().dialect
    except Exception as exc:  # noqa: BLE001
        print(f"[recommend] 生成查询前连库失败，降级：{_safe_err(exc)}")
        return _degraded()
    system_prompt = generate_query_system_prompt.format(
        dialect=dialect, top_k=state.get("blog_count", 5)
    )
    system_message = SystemMessage(content=system_prompt)
    # 取数要的是稳定，不是文采 —— 固定用低温度的那个模型。
    llm_with_tools = model_deterministic.bind_tools([_tool("sql_db_query")])
    return {"messages": [llm_with_tools.invoke([system_message] + state["messages"])]}

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
    """把 SQL 工具返回的原始文本解析成行字典；解析不了返回 None（不猜）。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
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


def compose_answer(state: RecommendState):
    """节点：只负责「说话」——把人设 + 查询结果说成爱弥斯的话。"""
    if state.get("recommend_failed"):
        return {"messages": [AIMessage(content=DB_UNAVAILABLE_REPLY)]}

    tool_msgs = filter_messages(state["messages"], include_types=["tool"])
    if not tool_msgs:
        # 没走到取数就收手了 —— 不许编，如实说，并留一个接口。
        return {
            "messages": [
                AIMessage(
                    content="我还没拿到结果就停住了。你再问我一次，"
                    "或者把方向说具体一点——比如「讲 Linux 进程的，短一点的」。"
                )
            ]
        }

    rows = _parse_rows(tool_msgs[-1].content)
    posts = _collect_posts(state)

    if rows is None or posts is None:
        # 结果解析不了 —— 别假装「133 篇里没有」，那是另一回事。
        return {"messages": [AIMessage(content=DB_UNAVAILABLE_REPLY)]}

    if not rows:
        return {
            "messages": [
                AIMessage(
                    content=(
                        f"站里没有讲这个的。{LIB_TOTAL} 篇我翻了一遍，一条都没对上。\n"
                        "要不换个说法，或者把范围放宽一点？"
                    )
                )
            ]
        }

    if not posts:
        return {
            "messages": [
                AIMessage(
                    content="查到了一些条目，但没取到链接。我不敢凭标题给你拼路径——"
                    "你稍后再问我一次。"
                )
            ]
        }

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

    return {"messages": [AIMessage(content=rendered)]}
