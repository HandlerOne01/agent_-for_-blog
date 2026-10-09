"""topic 锚点召回：分类词用词法粗筛，向量只在粗筛结果内排序。

为什么需要它（实测数据，库共 133 篇）：

  | 用户词        | 词法 topic/category/tags/title LIKE | 向量（重复串） |
  |---------------|--------------------------------------|----------------|
  | 八股文        | 12 篇（全对）                        | 5 篇（漏一半） |
  | C++           | 36 篇（太宽：只是 topic=C/C++ 的子串）| 10 篇          |
  | C++ 八股文    | 0 篇（没有哪一列含这个整串）          | 1 篇 0.826 ✅  |
  | Linux         | 41 篇（太宽）                        | 6 篇（偏）     |

  → 单靠词法：宽的爆量、整串 0 篇；单靠向量：分类词漏召回。
  → 正确分工：**词法负责"这几个词确实存在于库中"并收窄范围，
     向量负责在这个范围内排序。**
"""
from src.agent.common import retrieval


ROWS = [
    # (id, title, topic, category, tags, blog_type)
    (64, "八股文面试总结 · 写在前面", "八股文", "八股文", ",八股文,", "面试总结"),
    (65, "操作系统", "八股文", "八股文", ",八股文,", "面试总结"),
    (66, "C++", "八股文", "八股文", ",八股文,", "面试总结"),
    (67, "计算机网络", "八股文", "八股文", ",八股文,", "面试总结"),
    (68, "leetcode刷题", "八股文", "八股文", ",八股文,", "面试总结"),
    (30, "AVL树", "C/C++", "C/C++", ",C++," "数据结构,", "语言笔记"),
    (34, "C++基础语法", "C/C++", "C/C++", ",C++," "语法,", "语言笔记"),
    (42, "继承", "C/C++", "C/C++", ",C++," "面向对象,", "语言笔记"),
    (76, "编译流程与库机制", "Linux 系统", "Linux 系统", ",Linux," "编译,", "系统笔记"),
    (115, "HTTP", "计算机网络", "计算机网络", ",网络," "HTTP,", "网络笔记"),
]
BA_GU = [64, 65, 66, 67, 68]
C_CPP = [30, 34, 42, 66]


def _rows():
    return [
        {"id": i, "title": t, "topic": tp, "category": c, "tags": g, "blog_type": bt}
        for i, t, tp, c, g, bt in ROWS
    ]


# --------------------------------------------------------------------------
# 最长子串锚点
# --------------------------------------------------------------------------
def test_full_category_word_is_the_anchor():
    assert retrieval.anchor_ids("八股文", _rows()) == BA_GU


def test_partial_word_still_finds_the_anchor():
    """"推荐几篇关于八股的" 里没有"八股文"，但"八股"能命中 —— 这正是
    LLM 把整个问句塞进 topic 时的常见形态，必须能兜住。"""
    assert retrieval.anchor_ids("推荐几篇关于八股的", _rows()) == BA_GU


def test_medium_anchor_is_rejected_in_favor_of_vector():
    """"计算机网络" 字面只命中 2 篇（<min_hits）→ 退回向量，别拿它当锚点。

    真实库里"计算机网络"只在 id=67 的标题里，字面 1 篇；能当锚点的粒度是
    "网络"（14 篇）。这里夹具是 2 篇，同属"太窄"，结论一样。
    """
    assert retrieval.anchor_ids("计算机网络", _rows()) is None
    assert retrieval.anchor_ids("量子计算", _rows()) is None


def test_broad_phrase_is_rejected_when_over_the_line():
    """真实库里 "Linux" 只是 topic="Linux 系统" 的子串，命中 41 篇 > 15 的爆量线。
    这里把线压到 3 篇复现同一情形：C++ 的 4 篇就算爆量 → 不作数，交回向量。"""
    assert retrieval.anchor_ids("C++", _rows(), max_hits=3) is None


def test_phrase_absent_from_db_yields_no_anchor():
    assert retrieval.anchor_ids("量子计算", _rows()) is None


def test_blank_topic_yields_no_anchor():
    assert retrieval.anchor_ids("", _rows()) is None
    assert retrieval.anchor_ids(None, _rows()) is None
    assert retrieval.anchor_ids("   ", _rows()) is None


def test_extra_anchor_narrows_further():
    """"C++ 八股文" 取交集：样本里同时属于两边的只有 66（《C++》那篇）。

    实测这条正是用户的原话场景：库里的《C++》一文
    topic/category 都是"八股文"，所以它必须留在候选里。
    """
    assert retrieval.anchor_ids("C++ 八股文", _rows()) == [66]


def test_terms_without_enough_hits_do_not_narrow_the_anchor():
    """"Linux" 在这个样本里只命中 1 篇，够不到 min_hits → 它没有发言权，
    由八股文独自锚住，不要因为多了个弱词就把候选收空。"""
    assert retrieval.anchor_ids("八股文 Linux", _rows()) == BA_GU


# --------------------------------------------------------------------------
# 多候选短语
# --------------------------------------------------------------------------
# anchor_ids 本身只管"给一串短语，算它一个锚点"：多串时返回**第一个能锚住的**。
# "多串之间取最紧的"这一步在 generate_query 里（见 test_recommend_anchor_wiring.py
# 的 test_tightest_anchor_wins_across_candidates），因为那需要结合"哪串更可信"。
def test_broad_topic_phrase_is_taken_when_it_is_all_we_have():
    """只有 topic="C++" 时就用它 —— 4 篇 C/C++ 语言笔记，聊胜于无。"""
    assert retrieval.anchor_ids(["C++"], _rows()) == C_CPP


def test_one_phrase_intersects_all_its_terms():
    """同一个短语里的多个词要**求交**，不是各取一个。

    "C++ 八股文" = C++ 的 4 篇 ∩ 八股文的 5 篇 = 1 篇，正是用户要的那篇《C++》。
    """
    assert retrieval.anchor_ids(["C++ 八股文"], _rows()) == [66]
    assert retrieval.anchor_ids(["C++"], _rows()) == C_CPP
    assert retrieval.anchor_ids(["八股文"], _rows()) == BA_GU


def test_multi_candidate_returns_the_first_phrase_that_anchors():
    """多串时 anchor_ids 返回第一个能锚住的；选哪一串由调用方决定。"""
    assert retrieval.anchor_ids(["八股文", "C++"], _rows()) == BA_GU
    assert retrieval.anchor_ids(["C++", "八股文"], _rows()) == C_CPP
    # 第一串锚不住（"量子计算" 库里没有）→ 退到第二串
    assert retrieval.anchor_ids(["量子计算", "八股文"], _rows()) == BA_GU


def test_all_phrases_failing_yields_none():
    assert retrieval.anchor_ids(["量子计算", "绝对不存在的词"], _rows()) is None
    assert retrieval.anchor_ids(["", None, "   "], _rows()) is None


def test_blank_phrases_are_skipped_not_fatal():
    """空串混在候选里不该把整个锚点弄坏。"""
    assert retrieval.anchor_ids(["", "八股文"], _rows()) == BA_GU


# --------------------------------------------------------------------------
# 别名：用户词 → 库里真值
# --------------------------------------------------------------------------
# 夹具设计的关键：必须让**别名成为唯一能命中的路径**。
# 最初这几条测试用的是主夹具（tags 里已经含 ",八股文,"），于是字面解读自己
# 就命中了，把 ALIAS_FORMAT 整个删掉测试照样全绿 —— 是变异测试抓出来的。
# 真实库里那 12 篇的 tags 确实就是 ",八股文,"，所以这里刻意改成**不含**该 tags、
# 只留 blog_type='面试总结'，别名才成为唯一通路。
def _interview_only_rows():
    return [
        {"id": i, "title": t, "topic": "面试速记", "category": "面试速记",
         "tags": ",面试,", "blog_type": "面试总结"}
        for i, t in [(11, "操作系统"), (12, "C++"), (13, "计算机网络"),
                     (14, "leetcode刷题"), (15, "数据库")]
    ]


def test_bagu_wen_reaches_the_interview_series_through_its_alias():
    """"八股文" 必须能锚住 blog_type='面试总结' —— 靠别名，不靠 tags 字面。

    夹具里 tags 是 ",面试,"（没有"八股文"），topic 是"面试速记"，
    所以这条断言**只有** ALIAS_FORMAT 成立时才过。
    """
    rows = _interview_only_rows()
    assert retrieval.anchor_ids(["八股文"], rows) == [11, 12, 13, 14, 15]
    # 对照：这正是别名带来的命中（字面一个都没有）
    assert retrieval._hits("八股文", rows) == [11, 12, 13, 14, 15]


def test_bagu_alias_does_not_extend_to_unrelated_interview_posts():
    """别名只认 blog_type/tags 里的精确值，不会把所有"面试"相关的东西都捞进来。"""
    rows = _interview_only_rows() + [
        {"id": 99, "title": "面试穿搭", "topic": "职场", "category": "职场",
         "tags": ",穿搭,", "blog_type": "随笔"}
    ]
    assert 99 not in (retrieval.anchor_ids(["八股文"], rows) or [])


def test_alias_applies_to_blog_type_and_tags_only():
    """别名不许越界到 topic/category —— 那两列是"讲什么"，不能被体裁词顶替。

    构造一篇 topic 恰好叫"面试总结"的文章（真实库里不存在这种 topic 值）：
    用户说"八股文"不该把它锚进来。
    """
    rows = _interview_only_rows() + [
        {"id": 999, "title": "别的", "topic": "面试总结", "category": "面试总结",
         "tags": ",x,", "blog_type": "随笔"}
    ]
    assert 999 not in (retrieval.anchor_ids(["八股文"], rows) or [])


def test_subject_alias_reaches_mysql_from_the_word_database():
    """用户说"数据库"，库里那一列的值叫 "MySQL" —— 别名是唯一通路。

    夹具里没有任何一列含"数据库"三个字，所以只有 ALIAS_SUBJECT 成立才过。
    """
    rows = [
        {"id": 201 + i, "title": "mysql 笔记 %d" % i, "topic": "MySQL",
         "category": "MySQL", "tags": ",MySQL,", "blog_type": "数据库笔记"}
        for i in range(5)
    ]
    assert retrieval.anchor_ids(["数据库"], rows) == [201, 202, 203, 204, 205]


def test_subject_alias_intersects_with_the_format_word():
    """主题词 × 体裁词要求交 —— 用户原话"C++ 的八股文"的机制。

    夹具里没有任何一列含"八股文"，只能靠别名走到 blog_type='面试总结'；
    而 "C++" 只出现在其中一行的标题里，所以交集只剩那一行。
    注意别把 "C++" 放进基础行的 tags —— 那样 5 行都命中，交集就不再是 1 篇
    （这是写这条测试时踩过的坑）。
    """
    rows = [
        {"id": 11, "title": "操作系统", "topic": "面试速记", "category": "面试速记",
         "tags": ",面试,", "blog_type": "面试总结"},
        {"id": 12, "title": "C++", "topic": "面试速记", "category": "面试速记",
         "tags": ",面试,", "blog_type": "面试总结"},
        {"id": 13, "title": "计算机网络", "topic": "面试速记", "category": "面试速记",
         "tags": ",面试,", "blog_type": "面试总结"},
    ]
    assert retrieval.anchor_ids(["C++ 八股文"], rows) == [12]


def test_single_hit_interpretation_is_not_adopted():
    """只命中 1 篇的解读不作为**首选**（首选必须落在 min_hits..max_hits 内）。

    试过给"标题级命中"开后门（1 篇也算首选）：能救 "Linux 的八股文"（收到 id=69），
    但会把 "面试" 缩成 1 篇 —— 真实库里那 12 篇中恰好 id=64 的标题含"面试"，
    于是 12 篇的正确答案被砍成 1 篇。一个案例的收益换不来这种损失。
    """
    rows = [
        {"id": 1, "title": "flask", "topic": "Web", "category": "Web",
         "tags": ",flask,", "blog_type": "语言笔记"},
    ] + [
        {"id": 10 + i, "title": "其他 %d" % i, "topic": "Web", "category": "Web",
         "tags": ",flask,", "blog_type": "语言笔记"}
        for i in range(4)
    ]
    # 够格的解读排第一
    assert retrieval._anchor_options("flask", rows, 3, 15)[0] == [1, 10, 11, 12, 13]
    # 换一个只命中 1 篇的词 → 它排在够格的解读后面（这里没有够格的，只剩它）
    rows[0]["tags"] = ",flask,单独标签,"
    opts = retrieval._anchor_options("单独标签", rows, 3, 15)
    assert opts == [[1]], "1 篇的解读可以垫后参与求交，但不能被当成首选"


def test_middle_fragment_never_becomes_an_anchor():
    """词的**中间**片段不能当锚点（只有前后缀才算"用户还没说完"）。

    "量子计算" 的中间片段 "计算" 恰好出现在"计算机网络"里，命中 2 篇；
    如果不挡，它会被当成弱解读凑出一个假锚点。而 "推荐几篇关于八股的" 退到
    前缀 "八股" 是有意义的（用户就是这么说的）。
    """
    rows = _rows()
    # 中间片段：不算
    assert retrieval.anchor_ids(["量子计算"], rows) is None
    # 前后缀式退让：算（"八股" 是 "八股的" 的前缀）
    assert retrieval.anchor_ids(["八股的"], rows) == BA_GU
    # 直接验证守卫本身
    assert retrieval._anchor_options("量子计算", rows, 3, 15) == []


def test_single_character_substring_never_becomes_an_anchor():
    """"计算机 网络" 拆出来的单字/短碎片不许凑锚点。

    真实库里 "算" 命中 5 篇（topic=算法），"网" 命中 14 篇 —— 都是噪声。
    """
    rows = _rows() + [
        {"id": 400 + i, "title": "algo%d" % i, "topic": "算法", "category": "算法",
         "tags": ",算法,", "blog_type": "算法笔记"}
        for i in range(5)
    ]
    assert retrieval._anchor_options("算", rows, 3, 15) == []
    # "计算机 网络" 里根本没这个词 → 整串不该凑出任何锚点
    assert retrieval.anchor_ids(["量子算"], rows) is None


def test_subject_alias_does_not_leak_into_format_words():
    """"网络"是主题同义（网络 → Linux 网络），不是体裁词，别混进同一个范围表。"""
    assert retrieval.ALIAS_SUBJECT["网络"] == ("Linux 网络",)
    assert "网络" not in retrieval.ALIAS_FORMAT


def test_unknown_alias_source_is_harmless():
    """别名表里没有的词照常按字面匹配（两者各自锚到该锚的）。"""
    assert retrieval.anchor_ids(["八股文"], _rows()) == BA_GU
    assert retrieval.anchor_ids(["C++"], _rows()) == C_CPP


# --------------------------------------------------------------------------
# 词尾助词
# --------------------------------------------------------------------------
def test_prune_strips_trailing_particles():
    """"八股的" 这种带语气词的写法要能退回真正的分类词。

    实测起因：LLM 把整个问句塞进 topic 时会出现 "推荐几篇关于八股的"，
    退到 "八股" 才能命中。
    """
    assert retrieval._prune("八股的") == "八股"
    assert retrieval._prune("八股文的") == "八股文"
    assert retrieval._prune("C++ 的了") == "C++"


def test_prune_never_touches_the_middle_of_a_word():
    """只剥词尾 —— 早先的写法会把词中间的字也删掉。

    `re.sub(r"[的了呢吧啊嘛呀]|...", "", term)` 这种写法把 "的目的" 削成 "目"，
    等于把用户的词改坏了。中间的字是词的一部分，不能动。
    """
    assert retrieval._prune("的目的") != "目"
    assert retrieval._prune("的目的").endswith("目")


def test_prune_keeps_words_that_legitimately_end_in_particle():
    """"目的"、"为了" 本来就以"的/了"结尾，不能削。"""
    assert retrieval._prune("目的") == "目的"
    assert retrieval._prune("为了") == "为了"
    assert retrieval._prune("除了") == "除了"


def test_prune_leaves_normal_terms_alone():
    for t in ["Linux 系统", "AI 应用", "C/C++", "C++11", "项目实战", "面试"]:
        assert retrieval._prune(t) == t


# --------------------------------------------------------------------------
# 含空格的主题名不能被拆错
# --------------------------------------------------------------------------
def test_library_topic_with_space_still_anchors():
    """库里有 "Linux 系统"、"AI 应用" 这种带空格的主题名。

    anchor_ids 会按空格切词，切完每个词单独取解读再求交 ——
    对 "Linux 系统" 来说第一个词 "Linux" 也能锚到那批文章，所以结论不变。
    这条测试钉住"带空格的主题名照样能锚住"。
    """
    rows = _rows() + [
        {"id": 300 + i, "title": "sys%d" % i, "topic": "Linux 系统",
         "category": "Linux 系统", "tags": ",Linux,", "blog_type": "系统笔记"}
        for i in range(4)
    ]
    got = retrieval.anchor_ids(["Linux 系统"], rows)
    assert got, "带空格的主题名应当能锚住"
    assert set(got) >= {300, 301, 302, 303, 76}


def test_slash_topic_splits_like_a_separator():
    """"C/C++" 里的 "/" 是分隔符，切成 C 和 C++ 两个词。"""
    assert retrieval.topic_terms(["C/C++"]) == ["C", "C++"]


# --------------------------------------------------------------------------
# 排序：向量只在候选内排序
# --------------------------------------------------------------------------
def test_vector_ranking_is_restricted_to_candidates():
    ranked = retrieval.rank_within(
        BA_GU, [(66, 0.83), (30, 0.80), (64, 0.67), (115, 0.6)]
    )
    assert ranked == [66, 64]


def test_no_overlap_means_the_anchor_was_wrong():
    """向量在候选内一篇都挑不出来 → 说明这个词法锚点跟语义无关，作废。"""
    assert retrieval.rank_within(BA_GU, [(30, 0.9), (34, 0.8)]) is None
    assert retrieval.rank_within(BA_GU, []) is None
    assert retrieval.rank_within(BA_GU, None) is None


def test_rank_without_candidates_is_none():
    assert retrieval.rank_within([], [(64, 0.9)]) is None
