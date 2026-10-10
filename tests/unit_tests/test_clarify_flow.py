"""用户已经把方向说清楚了，就不该再追问。

背景（用户原话）："它问我方向，我给了c++方向，但应该推荐的是c++的八股文，
或者就别问"。问题出在 `missing_info` 把「篇幅」和「方向」并列当成"没说清"：

    用户："C++ 方向"        → topic 已抽到 "C++"
    提示词却说"你还没说清：篇幅"  → 用户感觉"我问了 A，你反问 B"

现在的规则：
  - 方向（topic）= 必填，缺了才问
  - 篇幅 = 可选，没提就不筛；要问就**顺带**问一句，别摆成"你没说清"
"""
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from src.agent.node import recommend as R


class _FakeStore:
    """最小 store 桩：没有历史偏好。"""

    def search(self, namespace):
        return []

    def put(self, *a, **kw):
        return None


def _slots(**kw):
    class _Info:
        def model_dump(self, exclude_none=True):
            return dict(kw)

    return _Info()


def _run(msg, slots, answer=None):
    """跑一次 collect_user_info；answer=None 表示断言"不该有中断"。"""
    state = {"messages": [HumanMessage(content=msg)], "user_preferences": {}}
    fake_interrupt = (
        patch.object(R, "interrupt", side_effect=AssertionError(
            "用户已经给了方向，不该再中断追问")) if answer is None
        else patch.object(R, "interrupt", return_value=answer)
    )
    with fake_interrupt, patch.object(R, "model_deterministic") as m, patch.object(
        R, "get_user_id", return_value="u"
    ):
        m.with_structured_output.return_value.invoke.return_value = slots
        out = R.collect_user_info(state, runtime=None, store=_FakeStore())
    return out


# --------------------------------------------------------------------------
# 方向已给 → 不追问
# --------------------------------------------------------------------------
def test_direction_given_does_not_interrupt_for_word_count():
    """用户说"C++ 方向" → topic 到手，不该为了篇幅再拦一次。"""
    out = _run("C++ 方向", _slots(topic="C++"))
    assert out.get("topic") == "C++"


def test_direction_given_does_not_interrupt_even_with_nothing_else():
    """连篇数都没说也不该问 —— 都能给默认值。"""
    out = _run("推荐几篇八股文", _slots(topic="八股文"))
    assert out.get("topic") == "八股文"
    assert out.get("word_count_min") is None
    assert out.get("word_count_max") is None


def test_topic_and_genre_both_kept():
    """"C++ 方向的八股文" → 主题进 topic，不能丢。"""
    out = _run("C++ 方向的八股文", _slots(topic="八股文", blog_type="面试总结"))
    assert out.get("topic") == "八股文"
    assert out.get("blog_type") == "面试总结"


# --------------------------------------------------------------------------
# 方向没给 → 才追问，且问的是方向
# --------------------------------------------------------------------------
def test_no_direction_still_interrupts():
    """用户一个方向词都没给 → 必须问，否则不知道该翻哪一块。"""
    out = _run("给我推荐几篇", _slots(), answer="不提供")
    # 一次中断就够了；问到 answer 后不该再挂住
    assert out.get("blog_count") == 5


def test_interrupt_asks_for_direction_not_complains_about_word_count():
    """提示词要问"想看哪一块"，不能把篇幅摆成"你没说清"。"""
    state = {"messages": [HumanMessage(content="推荐几篇")], "user_preferences": {}}
    seen = {}

    def _capture(prompt):
        # v1.99（前端 v1.100）：`interrupt` 的载荷改成结构化 dict（多了 kind/options/default，
        # 给前端判断"该回哪个词 / 要不要替答"）—— 这句问句本身在 `value` 里，断言口径不变。
        seen["prompt"] = prompt.get("value", "") if isinstance(prompt, dict) else prompt
        return "不提供"

    with patch.object(R, "interrupt", side_effect=_capture), patch.object(
        R, "model_deterministic"
    ) as m, patch.object(R, "get_user_id", return_value="u"):
        m.with_structured_output.return_value.invoke.return_value = _slots()
        R.collect_user_info(state, runtime=None, store=_FakeStore())

    prompt = seen.get("prompt", "")
    assert "哪一块" in prompt or "方向" in prompt, (
        "问的是方向才对：%r" % prompt
    )
    assert "没说清" not in prompt, (
        "别用'你还没说清'这种口气，篇幅是可选信息：%r" % prompt
    )


def test_decline_keeps_topic_clean_when_present():
    """答「不提供」而 topic 已有值 → 不许塞"随机领域"这种占位词。

    占位词会被拼进向量查询串（query_text 取 topic/category/tags/blog_type/others），
    凭空干扰召回。
    """
    state = {"messages": [HumanMessage(content="推荐几篇")], "user_preferences": {}}
    with patch.object(R, "interrupt", return_value="不提供"), patch.object(
        R, "model_deterministic"
    ) as m, patch.object(R, "get_user_id", return_value="u"):
        # 第一轮没抽到 topic，用户答「不提供」
        m.with_structured_output.return_value.invoke.return_value = _slots()
        out = R.collect_user_info(state, runtime=None, store=_FakeStore())

    assert out.get("topic") != "随机领域", "不该再用占位领域词"
    assert out.get("word_count_min") is None
    assert out.get("word_count_max") is None
