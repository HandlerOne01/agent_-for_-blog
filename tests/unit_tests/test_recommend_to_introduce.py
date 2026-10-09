"""推荐跑完后追问「要不要我讲讲其中一篇」这条支路。

四个坑都在这里有回归网：
  1. 推荐子图必须把候选写进**共享状态**（只在父图或只在子图声明都不够）；
  2. 条件边函数的**参数类型**必须是父图 State —— 用私有状态会被 LangGraph 收窄字段，
     看不到预填的篇名，于是永远返回 END（图根本不进介绍子图）；
  3. 预填篇名的**键名**必须与介绍子图 IntroduceState 的同名字段一致，
     否则子图读不到，还会拿用户的序号"1"去抽篇名；
  4. 降级 / 无候选时不该问。
"""
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from src.agent.state.main import State


def _annotations() -> dict:
    out: dict = {}
    for klass in reversed(State.__mro__):
        out.update(getattr(klass, "__annotations__", {}) or {})
    return out


def test_parent_state_declares_branch_keys():
    ann = _annotations()
    assert "recommended_posts" in ann, "父图要收得到候选清单"
    # 键名必须与 IntroduceState.target_query 一致，父子图才能贯通
    assert "target_query" in ann, "父图要能预填用户挑中的篇名"


def test_collect_keys_and_private_state_are_gone():
    """收藏已下架：它的私有状态与路由节点都不该再存在。"""
    import src.agent.graph as g

    assert not hasattr(g, "should_collect"), "should_collect 应已删除"
    assert not hasattr(g, "need_collect"), "need_collect 应已删除"
    from src.agent.state import main as st

    assert not hasattr(st, "NeedCollectOutput"), "NeedCollectOutput 应已删除"


def test_free_text_answer_is_treated_as_decline():
    """回「不用」「算了」这类识别不出序号的话 → 当作用户拒绝，直接收场。

    不能像 _ask_which 那样重问 —— 这里是"顺便问一句"，不是必经步骤。
    """
    from src.agent.node.main import _pick_recommended_post

    posts = [{"id": 42, "title": "继承"}]
    assert _pick_recommended_post("不用", posts) is None
    assert _pick_recommended_post("算了", posts) is None
    assert _pick_recommended_post("", posts) is None


def test_numeric_answer_picks_the_article():
    from src.agent.node.main import _pick_recommended_post

    posts = [{"id": 42, "title": "继承"}, {"id": 66, "title": "C++"}]
    assert _pick_recommended_post("1", posts)["id"] == 42
    assert _pick_recommended_post(" 2 ", posts)["id"] == 66
    assert _pick_recommended_post("3", posts) is None, "越界序号当拒绝"


def test_introduce_target_is_not_re_extracted():
    """父图预填了篇名时，extract_title 必须早退 —— 否则 "1" 会被当成篇名。"""
    from src.agent.node.introduce import extract_title

    with patch("src.agent.node.introduce.model_deterministic") as m:
        out = extract_title({"messages": [], "target_query": "继承"})
    assert out["target_query"] == "继承"
    m.with_structured_output.assert_not_called()


def test_after_recommend_asks_introduce_when_posts_exist():
    from src.agent.graph import after_recommend

    assert after_recommend({"recommended_posts": [{"id": 42}]}) == "need_introduce"


def test_after_recommend_skips_when_degraded_or_empty():
    """降级 / 一条都没推荐出来 → 不问。"""
    from src.agent.graph import after_recommend

    assert after_recommend({"recommend_failed": True, "recommended_posts": [{"id": 42}]}) == "__end__"
    assert after_recommend({"recommended_posts": []}) == "__end__"
    assert after_recommend({}) == "__end__"


def test_compose_answer_exposes_recommended_posts():
    """compose_answer 必须把这一轮的候选写进状态。

    父图靠它列出"想听哪篇"。注意它只有在**真的走到"说话"那一步**才会写 ——
    降级 / 没有 tool 消息 / 空结果都会早退，那时也确实没有候选可讲。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    from src.agent.node.recommend import compose_answer

    posts = [{"id": 42, "title": "继承", "category": "C/C++", "word_count": 21242,
              "difficulty": 2, "blog_url": "https://x/posts/cpp/jicheng/"}]
    state = {
        "messages": [
            HumanMessage(content="推荐几篇讲继承的"),
            AIMessage(content="", tool_calls=[
                {"name": "sql_db_query", "args": {"query": "select 1"},
                 "id": "c1", "type": "tool_call"}
            ]),
            ToolMessage(content="[('继承', 21242)]", tool_call_id="c1", name="sql_db_query"),
        ]
    }
    with patch("src.agent.node.recommend._collect_posts", return_value=posts), patch(
        "src.agent.node.recommend.model"
    ) as gen:
        # 让"说话"这步退模板（模型抛错）→ 渲染结果是确定的模板文本
        gen.invoke.side_effect = RuntimeError("mock")
        out = compose_answer(state)
    assert out.get("recommended_posts") == posts


def test_full_chain_recommend_then_introduce():
    """**跑真图**：推荐 → 追问（答序号）→ 进介绍子图 → 产出这篇的介绍。

    这条是整条支路的总回归网。它刻意**不手工注入任何中间状态** ——
    上一版测试手工喂了 target_query，于是把两件真 bug 放过去了：
      ① 条件边参数写成私有状态 → 永远返回 END，子图从不执行；
      ② 父图写的键叫 introduce_target、子图读的却是 target_query → 子图收到空篇名。
    这里改为真的 invoke 主图 + resume。

    这条**不跑整图**，而是直接钉住那两个真 bug 的接缝 —— 它们各自都能独立证伪：

      Bug ①（条件边参数类型）：`should_introduce` 若把参数注成私有状态
        `NeedIntroduceOutput`，LangGraph 会按签名收窄传入的字段，于是读不到预填篇名、
        永远返回 END，介绍子图从不执行。这里钉住：
          - 它的入参注解必须是父图 State（能读到 target_query）
          - 给它一个"只有 target_query"的完整状态时，必须返回 introduce_graph
      Bug ②（键名不一致）：`need_introduce` 写 `introduce_target` 而子图读
        `target_query`，于是子图拿用户的"1"去抽篇名。这里钉住：
          - need_introduce 的返回值里必须有 target_query
          - 这个键必须出现在父图 State 的声明里（否则会被 LangGraph 丢弃）
          - extract_title 见到它必须早退、不去调模型

    为什么不从零 invoke 整图：那必须先骗过入口节点的 store 注入、推荐子图的
    SQLDatabaseToolkit、以及四处真模型调用，mock 面积大到测试本身就成了负担
    （试过三版，都是在跟基建搏斗而不是在验行为）。
    """
    import inspect

    import src.agent.node.main as M
    from src.agent.graph import should_introduce
    from src.agent.node.introduce import extract_title, locate_article

    post = {"id": 42, "title": "继承", "category": "C/C++",
            "word_count": 21242, "difficulty": 2,
            "blog_url": "https://imiss.example.com/posts/cpp/jicheng/"}

    # ── Bug ① ──────────────────────────────────────────────────────────
    ann = inspect.signature(should_introduce).parameters["state"].annotation
    assert ann is State or ann == "State", (
        "should_introduce 的入参注解必须是父图 State（当前 %r）—— 写成私有状态会让 "
        "LangGraph 收窄字段，看不到 target_query，条件边永远返回 END" % (ann,)
    )
    assert should_introduce({"target_query": "继承"}) == "introduce_graph"
    assert should_introduce({}) == "__end__"
    assert should_introduce({"target_query": ""}) == "__end__"

    # ── Bug ② ──────────────────────────────────────────────────────────
    with patch.object(M, "interrupt", return_value="1"):
        asked = M.need_introduce({"recommended_posts": [post]})
    # 预填的是**精确标识**（blog_url），不是篇名 —— 篇名会撞名：
    # 实测 target_query='C++' 时 find_by_title 一次命中 4 篇，于是反问用户
    # "你说的是哪一篇"，可父图本来就精确知道是哪一篇。
    assert asked.get("target_query") == post["blog_url"], (
        "need_introduce 必须把 blog_url 写进 target_query（精确、唯一），"
        "实际返回 %r" % (asked,)
    )
    assert "target_query" in _annotations(), (
        "target_query 必须声明在父图 State 上，否则这个键会被 LangGraph 丢弃"
    )

    # 这个预填值必须能被介绍子图**唯一**解析出来（否则等于换了个撞名法）
    with (
        patch("src.agent.node.introduce.find_by_url", return_value=[post]) as fbu,
        patch("src.agent.node.introduce.find_by_title") as fbt,
        patch("src.agent.node.introduce.search_ids", return_value=[]) as sid,
    ):
        located = locate_article({"messages": [HumanMessage(content="1")],
                                  "target_query": asked["target_query"]})
    assert located.get("target_post") == post, (
        "预填 %r 应当被唯一解析成 %r，实际 %r（fbu.called=%s fbt.called=%s）"
        % (asked["target_query"], post, located.get("target_post"),
           fbu.called, fbt.called)
    )
    assert fbu.called, "预填的是 URL，就应当走 URL 精确查找"
    assert not fbt.called, "URL 命中就不该再去模糊查标题（那正是撞名的来源）"
    assert not sid.called

    # 父图预填了标识 → 子图不再抽，直接用它
    with patch("src.agent.node.introduce.model_deterministic") as m:
        out = extract_title({"messages": [HumanMessage(content="1")],
                             "target_query": asked["target_query"]})
    assert out["target_query"] == post["blog_url"]
    m.with_structured_output.assert_not_called()
