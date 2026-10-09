"""抽取槽位时，主题词必须进 topic，不能进 blog_type。

背景：用户说「推荐几篇八股文」，模型把它抽成了 blog_type='八股文'（主题当体裁）。
后果是双重的：
  - SQL 链路：blog_type='八股文' 在库里一篇都匹配不到（库里的 blog_type 是"面试总结"）→ 0 篇
  - 向量链路：topic 为空，向量对弱锚点给出的是一批跟八股文无关的文章
  → 用户拿到 5 篇不对的文章

blog_type 只放**体裁**（教程/实战/原理剖析/随笔/踩坑记录/工具介绍），
主题一律进 topic。
"""
from src.agent.node.recommend import UserInfo


def test_topic_description_covers_subject_matter():
    """topic 的描述要排除"体裁"这个歧义，并给出主题类例子（八股文这种）。

    注意：描述里不必写"主题"二字 —— 实测**规则陈述打不过具体映射示例**，
    真正让模型把"八股文"归对位置的是提示词里那段映射示例
    （见 test_extraction_prompt_gives_mapping_examples）。
    """
    d = UserInfo.model_fields["topic"].description
    assert "八股" in d, "topic 描述应包含'八股文'这类主题例子：%r" % d


def test_blog_type_description_is_limited_to_genre():
    """blog_type 的描述必须点明它只放"体裁"，避免主题词被塞进来。"""
    d = UserInfo.model_fields["blog_type"].description
    assert "体裁" in d, "blog_type 描述里应点明它只管体裁：%r" % d
    assert "不要" in d or "不放" in d or "而非" in d, (
        "blog_type 描述应明确排除主题类词：%r" % d
    )


def test_extraction_prompt_routes_subject_to_topic():
    """抽取提示词要明确"主题进 topic"，否则模型仍会把八股文归到 blog_type。"""
    import inspect

    src = inspect.getsource(
        __import__("src.agent.node.recommend", fromlist=["x"]).collect_user_info
    )
    assert "topic" in src and "blog_type" in src, "提示词里应点名这两个字段"
    assert ("主题" in src and "体裁" in src), (
        "提示词应说清 主题→topic、体裁→blog_type 的分工"
    )


def test_extraction_prompt_gives_mapping_examples():
    """必须有 few-shot 映射示例。

    实测（4 个变体 × 4 句用户原话）：
      - 只改字段描述：'推荐几篇 C++ 的八股文' → topic='C++'，八股文丢了
      - 只把规则放最前：→ tags='八股文'
      - 规则放在字段描述后面：→ topic='C++、八股文'
      - **加上映射示例**：→ topic='八股文', tags='C++' ✅ 四句全对
    """
    import inspect

    src = inspect.getsource(
        __import__("src.agent.node.recommend", fromlist=["x"]).collect_user_info
    )
    assert "映射示例" in src, "提示词里应有映射示例这一节"
    assert "C++ 的八股文" in src, (
        "示例要覆盖用户的原话形态（'推荐几篇 C++ 的八股文'）：%s" % src[-600:]
    )
