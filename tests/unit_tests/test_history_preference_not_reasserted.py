"""历史偏好不许被当成本轮消息重新抽出来。

背景（真机日志抓到的）：
    collect_user_info 会把 store 里的历史偏好拼成一条 HumanMessage：
        "用户的历史偏好信息如下：1. 最少字数：500 2. 最多字数：5000"
    再和本轮消息一起喂给抽取模型。提示词虽然写了"只提取用户明确提到的信息"，
    但模型把这条**横幅本身**当成了用户的陈述，于是把 500-5000 原样抽出来，
    写回 state、拼进 SQL：
        ... WHERE id IN (...) AND word_count >= 500 AND word_count <= 5000
    这条链路会**每轮复现**，用户看起来就是"我明明没提字数，它老拿 500-5000 筛我"。

    更糟的是用户明确说"字数不限"时，提示词里"xx字以上→20000"的规则会把它
    变成 min=0/max=20000 —— 仍然是筛选，不是"不筛"。

正确语义：
  - 抽取**只看本轮消息**，历史偏好不参与抽取
  - 本轮没提字数、且历史偏好也没记过 → 不筛（两个字段保持 None）
  - 本轮明确说"不限" → 不筛，且要能覆盖掉历史偏好
"""
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from src.agent.node import recommend as R


class _FakeStore:
    def __init__(self, prefs=None):
        self._prefs = prefs or []

    def search(self, namespace):
        class _Item:
            def __init__(self, value):
                self.value = value
                self.key = "k1"

        return [_Item(p) for p in self._prefs]

    def put(self, *a, **kw):
        return None


def _slots(**kw):
    class _Info:
        def model_dump(self, exclude_none=True):
            return dict(kw)

    return _Info()


def _run(msg, slots, prefs=None, captured=None):
    state = {"messages": [HumanMessage(content=msg)],
             "user_preferences": prefs or {}}
    with patch.object(R, "interrupt", return_value="不提供"), patch.object(
        R, "model_deterministic"
    ) as m, patch.object(R, "get_user_id", return_value="u"):
        inst = m.with_structured_output.return_value
        if captured is not None:
            def _invoke(messages):
                captured.append(messages)
                return slots
            inst.invoke.side_effect = _invoke
        else:
            inst.invoke.return_value = slots
        return R.collect_user_info(state, runtime=None, store=_FakeStore(prefs))


# --------------------------------------------------------------------------
# 抽取输入里不许出现历史偏好
# --------------------------------------------------------------------------
def test_history_preference_is_not_fed_to_the_extractor():
    """抽取模型只能看到**本轮**用户消息，不能看到历史偏好横幅。

    这是本轮 bug 的根：横幅一进去，模型就会把旧字数当成用户说的重新抽出来。
    """
    captured = []
    _run("推荐几篇八股文", _slots(topic="八股文"), 
         prefs={"word_count_min": 500, "word_count_max": 5000},
         captured=captured)
    assert captured, "应当调用过抽取"
    texts = [
        m.content if isinstance(m.content, str) else str(m.content)
        for m in captured[0]
    ]
    joined = "\n".join(texts[1:])          # [0] 是 system prompt
    assert "500" not in joined, (
        "历史偏好里的 500 不该出现在抽取输入里：%r" % joined
    )
    assert "历史偏好" not in joined, (
        "不该把历史偏好拼成一条消息喂给抽取模型：%r" % joined
    )


def test_current_message_is_still_what_gets_extracted():
    """只看本轮 ≠ 不看内容：本轮消息必须原样传给抽取。"""
    captured = []
    _run("推荐几篇 C++ 的八股文", _slots(topic="八股文", tags="C++"),
         captured=captured)
    joined = "\n".join(
        m.content if isinstance(m.content, str) else str(m.content)
        for m in captured[0][1:]
    )
    assert "C++ 的八股文" in joined, joined


# --------------------------------------------------------------------------
# 字数语义
# --------------------------------------------------------------------------
def test_no_word_count_mentioned_stays_unset():
    """本轮没提字数 → 两个字段保持 None（不筛）。"""
    out = _run("推荐几篇八股文", _slots(topic="八股文"))
    assert out.get("word_count_min") is None
    assert out.get("word_count_max") is None


def test_explicit_unlimited_wins_over_history():
    """用户明确说"不限" → 不筛，即使历史偏好记着 500-5000。"""
    out = _run("推荐几篇八股文，字数不限", _slots(topic="八股文"),
               prefs={"word_count_min": 500, "word_count_max": 5000})
    assert out.get("word_count_min") is None, (
        "说了不限就不该还拿 500 筛：%r" % out.get("word_count_min")
    )
    assert out.get("word_count_max") is None, (
        "说了不限就不该还拿 5000 筛：%r" % out.get("word_count_max")
    )


def test_explicit_word_count_this_turn_is_kept():
    """本轮明确给了字数 → 原样保留。"""
    out = _run("推荐几篇短的，1000 字以内",
               _slots(topic="八股文", word_count_max=1000))
    assert out.get("word_count_max") == 1000


def test_history_preference_is_not_reasserted_by_the_extractor():
    """历史偏好存在时，抽取结果里没有字数 → state 里就不该有字数。

    这条是上一个 bug 的直接复现：横幅喂进去 → 模型回 500/5000 → 写回 state。
    """
    out = _run("推荐几篇八股文", _slots(topic="八股文"),
               prefs={"word_count_min": 500, "word_count_max": 5000})
    assert out.get("word_count_min") is None
    assert out.get("word_count_max") is None
