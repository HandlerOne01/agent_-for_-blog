# -*- coding: utf-8 -*-
"""向量召回：用 LangChain 的 InMemoryVectorStore。

索引由 scripts/build_index.py 生成，落在 data/blog_index.json。

对外只有 search_ids() 一个函数：给它一句话，返回最像的几篇的 **id**。
调用方（node/recommend.py 的 generate_query）拿这些 id 拼一条只含 id 条件的
SQL，剩下的交给原来的 check_query / run_query / compose_answer —— 那条链路
一行都不用改。

分数语义 = 余弦相似度，跟标定门槛时的语义一致：
    langchain_core/vectorstores/in_memory.py:314 调的就是 cosine_similarity
    line 317 用 argsort()[::-1] 排成降序（分数越大越像）

返回值三种情况，调用方必须分清：
    [(id, 分数), ...]  挑到了，按分数降序
    []                 索引没问题，但一篇都不够像 —— "库里没这个方向"
    None               索引没建 / embedding 接口不可用 —— 调用方该回落 SQL
"""
import itertools
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import OpenAIEmbeddings

load_dotenv()

# 用 Qwen3-Embedding-8B（纯文本版，4096 维）。
# 注意别选成 Qwen3-VL-Embedding-8B —— 那个是多模态模型，实测在本语料上
# 相关/不相关的分数区间完全重叠，设不出门槛（详见下面 RECALL_SCORE_MIN）。
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("GuiJiLiuDong_API_KEY")

# src/agent/common/retrieval.py -> 往上四层才是项目根
INDEX_PATH = Path(__file__).resolve().parents[3] / "data" / "blog_index.json"

# ---------------------------------------------------------------------------
# 召回参数。【实测标定】出来的，换模型 / 换语料必须重新标。
#
# 标定方法：15 个站内真有的方向 + 20 个无关问题（红烧肉、奥运会、房贷利率…），
# 拿 raw 余弦比一遍，看两组的 top1 能不能分开。
#
# Qwen/Qwen3-Embedding-8B 实测（文档文本 = "标题。摘要。分类。标签"，查询 = 裸文本）：
#     相关查询   top1 0.522 ~ 0.775
#     不相关查询 top1 0.289 ~ 0.457
#   → 门槛 0.50：相关全过，不相关全拦。可分离区间 [0.457, 0.522] 不宽，
#     所以这一条门槛是这个模型 + 这份语料 + 这个文本格式特有的，换任何一个都必须重标。
#
# 为什么从 0.60 下调到 0.50：0.60 是旧文本格式（"标题。摘要"）下标出来的。
# 把分类/标签拼进文档文本后，无关查询的 top1 反而**下降**（0.328~0.394 →
# 0.289~0.457），相关查询整体上移，可分离区间整体下移。若仍用 0.60，
# 「八股文」（0.563）、「推荐几篇讲 Linux 的博客」（0.531）、「项目实战经验」
# （0.522）这类查询会被误拦 —— 它们都指向站内真实存在的方向。
# 实测取舍（15 相关 / 20 无关）：
#     门槛 0.60 -> 相关过 12/15，无关误判 0/20
#     门槛 0.55 -> 相关过 13/15，无关误判 0/20
#     门槛 0.50 -> 相关过 15/15，无关误判 0/20   ← 选它
#     门槛 0.45 -> 相关过 15/15，无关误判 1/20
# 0.50 落在可分离区间内，且对最高无关（0.457）留 0.043 余量。
#
# 【反面教材】Qwen/Qwen3-VL-Embedding-8B（多模态版）在同一批查询上：
#     相关 top1 0.587~0.821，不相关 top1 0.331~0.740 —— 区间重叠，
#     绝对值 / top1-中位数 / z-score 三种判据都分不开，别用它做门槛。
#
# 为什么必须有这道门：没有它，「库里没这个方向」会退化成「硬塞 K 篇不相干的」，
# 那就把 generate_query 提示词里最强调的「查不到就空着，绝不放宽」毁了。
# 漏了不亏 —— 漏了就回落 SQL 链路，等于加向量之前的行为。
#
# RECALL_ITEM_MIN 过了门之后逐条收的下限
# RECALL_SPREAD   下限还随 top1 浮动：真正的下限是 max(ITEM_MIN, top1 - SPREAD)
#                 防的是"门内尾巴"——问「内存泄漏」时最好的那篇被字数条件筛掉，
#                 0.45 档的尾巴里混进一篇不相干的，答复节点只能硬圆成"也相关"。
#
# 【已知边界】"只报分类名"的查询，向量只能召回很少几篇，这是文本稀释导致的，
# 调闸门救不回来。实测「八股文」（category 有 12 篇）：
#     裸分数  0.5628(64) 0.4484(66) 0.4428(67) 0.4333(75) 0.4278(73) …
#     第二到第十二篇全在 0.33~0.45，过不了 score_min=0.50
#     把 SPREAD 从 0.15 一路放到 1.00，召回数**始终是 1 篇**（说明卡的是门槛不是尾巴）
#     而把 score_min 降到 0.45 就会放进"房贷利率"这类无关查询 —— 所以不能降
# 原因：分类词在文档文本里只出现两次（title/intro 之外的锚点），被正文语义稀释了。
# 对照：说人话的「推荐几篇关于八股的文章」top1=0.66，能召回 9 篇。
# 结论：**按分类/标签找文章，可靠的是 SQL 链路的 category/tags LIKE**，
#       向量只擅长"意思对得上"的那种查询。不要为了这个案例再调闸门。
# ---------------------------------------------------------------------------
RECALL_TOP_K = 10
RECALL_SCORE_MIN = 0.50
RECALL_ITEM_MIN = 0.45
RECALL_SPREAD = 0.15

_embeddings: Optional[OpenAIEmbeddings] = None
# 懒加载的索引；False 表示加载失败过，不再重试。
_store: object = None


def get_embeddings() -> OpenAIEmbeddings:
    """单例的 embedding 客户端。缺 key 直接抛，由调用方决定怎么降级。"""
    global _embeddings
    if _embeddings is None:
        if not EMBEDDING_API_KEY:
            raise RuntimeError(
                "未配置 embedding 的 key：请在 .env 里加 EMBEDDING_API_KEY"
                "（或沿用 GuiJiLiuDong_API_KEY）"
            )
        _embeddings = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=EMBEDDING_BASE_URL,
            api_key=EMBEDDING_API_KEY,
            # 关掉 tiktoken 预切分：那是 OpenAI 的口径，对中文和第三方端点会误判。
            check_embedding_ctx_length=False,
        )
    return _embeddings


def get_store():
    """懒加载索引。文件不存在 / 读坏 —— 返回 None 并出声，之后不再重试。"""
    global _store
    if _store is None:
        try:
            _store = InMemoryVectorStore.load(str(INDEX_PATH), get_embeddings())
        except FileNotFoundError:
            print(
                f"[recall] 没找到索引 {INDEX_PATH}，回落 SQL 链路"
                "（先跑 scripts/build_index.py）"
            )
            _store = False
        except Exception as exc:  # noqa: BLE001 索引坏了不该拖垮推荐
            print(f"[recall] 索引读取失败（{type(exc).__name__}: {exc}），回落 SQL 链路")
            _store = False
    if _store is False:
        return None
    return _store


# ---------------------------------------------------------------------------
# 分类词锚点（词法粗筛）
# ---------------------------------------------------------------------------
# 为什么在纯向量之外还要这一层：向量对**分类词**不可靠，实测（库 133 篇）：
#     "八股文"     词法 12 篇全对 / 向量只召回 5 篇（漏一半）
#     "C++"        词法 36 篇太宽 / 向量 10 篇，混进一批语言笔记
#     "C++ 八股文" 词法 0 篇      / 向量 1 篇 0.826（精准）
# 两条路各有死角，所以分工：
#     词法只回答"这几个词确实存在于库中吗、命中面有多大"，用它把范围收住；
#     向量负责在这个范围**内**排序。交集为空就说明这个词法锚点跟语义无关，作废。
#
# 改这段之前先看上面 RECALL_SCORE_MIN 那段注释：那里早已写下结论
# "按分类/标签找文章，可靠的是 SQL 链路的 category/tags LIKE"。
ANCHOR_MIN_HITS = 3
ANCHOR_MAX_HITS = 15
ANCHOR_FIELDS = ("title", "topic", "category", "tags")
ANCHOR_SPLIT = re.compile(r"[\s,，、/|;；]+")

# ---------------------------------------------------------------------------
# 用户词 → 库里真值的别名
# ---------------------------------------------------------------------------
# 为什么需要：库里"八股文"只出现在 tags，而 tags 存的是 ",八股文," 这种带逗号的
# 形态；真正把这个系列串起来的列是 **blog_type='面试总结'**（12 篇）。
# 用户说"八股文"时，词法链路必须在 blog_type 那一维上认出来。
#
# 实测这 12 篇的主题分布（用 title 对出来的）：
#     id=65 操作系统、66 C++、67 计算机网络、69 linux服务器、
#     74 数据结构及算法、75 数据库 —— 其余 6 篇是系列说明/杂项
# 也就是说它们**本来就是** C/C++、Linux、算法、MySQL 这些主题的面试速记版，
# 加上别名后每个"主题 × 八股文"的组合都能精准收到 1 篇（实测）：
#     Linux ∩ 八股文 → 69   C++ ∩ 八股文 → 66   数据库 ∩ 八股文 → 75
#     网络 ∩ 八股文 → 67    算法 ∩ 八股文 → 74   操作系统 ∩ 八股文 → 65
#
# 两组别名的**生效范围**不同，这是刻意的：
#   体裁词（八股文/面试）只在 blog_type / tags 里生效 —— 那两列才表达"哪一套"；
#     若让它落到 topic/category，会把一篇 topic 恰好叫"面试总结"的无关文章捞进来。
#   主题词的等价写法（数据库=MySQL、网络=Linux 网络）是"讲什么"的同义，
#     在所有列上生效。
ALIAS_FORMAT = {
    "八股文": ("面试总结",),
    "八股": ("面试总结",),
    "面经": ("面试总结",),
    "面试": ("面试总结",),
}
ALIAS_FORMAT_FIELDS = ("blog_type", "tags")
ALIAS_SUBJECT = {
    "数据库": ("MySQL",),
    "网络": ("Linux 网络",),
    "操作系统": ("Linux 系统",),
    # 已知缺口，故意不修："Linux" 没有可用的窄别名。
    #   字面命中 41 篇（爆量）；"Linux 系统" 27 篇、"Linux 网络" 13 篇都不是
    #   字面集合的真子集，在 "Linux 的八股文" 里会交出 13 篇网络文章（丢掉
    #   "八股文"这个限定）；能精确收窄的只有 id=69 自己的 title
    #   "linux服务器"，但只命中 1 篇、够不到 min_hits。
    #   现状："Linux 的八股文" 仍锚到全部 12 篇八股文，由向量把 id=69 排到第一
    #   —— 不精准但不静默出错（那 12 篇确实都是八股文）。
    #   要根治得动数据（给每篇八股文行补上它对应的主题标签），不在本轮范围。
}
# 兼容旧名（测试与本模块历史都用过 ANCHOR_ALIASES 这个名字）
ANCHOR_ALIASES = ALIAS_FORMAT
ANCHOR_ALIAS_FIELDS = ALIAS_FORMAT_FIELDS

__all__ = [
    "search_ids",
    "anchor_ids",
    "rank_within",
    "topic_terms",
    "get_store",
    "get_embeddings",
]


def _alias_scopes(term: str):
    """给一个词，返回 [(别名, 允许匹配的列), ...]。"""
    key = (term or "").strip()
    out = [(a, ALIAS_FORMAT_FIELDS) for a in ALIAS_FORMAT.get(key, ())]
    out += [(a, ANCHOR_FIELDS) for a in ALIAS_SUBJECT.get(key, ())]
    return out


def _matches(phrase: str, row: dict) -> bool:
    """phrase（含别名）是否出现在这篇文章的任一可检索文本列里（不分大小写）。"""
    low = phrase.lower()
    if any(low in str(row.get(f) or "").lower() for f in ANCHOR_FIELDS):
        return True
    for alias, fields in _alias_scopes(phrase):
        alow = alias.lower()
        if any(alow in str(row.get(f) or "").lower() for f in fields):
            return True
    return False


def topic_terms(phrases) -> list:
    """把若干短语切成去重的词表（空格/逗号/顿号都算分隔符）。"""
    if isinstance(phrases, str) or phrases is None:
        phrases = [phrases]
    out = []
    for phrase in phrases:
        for term in ANCHOR_SPLIT.split(str(phrase or "")):
            term = term.strip()
            if term and term not in out:
                out.append(term)
    return out


def _variants(phrase: str, rows) -> list:
    """一个词的**所有解读**（字面一种、每个别名各一种），返回 [(ids, 是否标题级命中)]。

    标题级命中（只靠 title 就匹配上）比分类列命中精确得多：title 是一个具体的
    文章名，而 topic/category 是分类名。所以标题级命中的门槛可以放到 1 篇
    —— "linux服务器" 只命中 1 篇，但它就是那一篇，不该被 min_hits=3 否掉。

    为什么不在这里取并集：`"linux"` 的别名是（"Linux 系统", "Linux 网络"）共 27+13 篇，
    并起来 41 篇就爆量了、整条锚点被跳过；分开看则 `"Linux 网络"`（13 篇）
    正好落在 [3,15] 里，能拿它去收窄。用户打字习惯和库里的列值常常对不上
    （linux vs "Linux 系统"），要的是"哪种解读最可用"，不是"全部合起来"。

    合并发生在**上层**：`_anchor_options` 会把字面解读跟它的别名解读**并起来**
    也当成一个候选。为什么两边都要：真库里"数据库"字面只命中 2 篇（id=75 + 一篇
    MySQL 基础，够不到 min_hits），别名的 MySQL 6 篇里又**不含** id=75 ——
    只有"字面 ∪ 别名"这个并集才同时有足够的命中面和正确的那一篇。
    """
    seen, out = set(), []

    for phrase_text, fields in [(phrase, ANCHOR_FIELDS)] + _alias_scopes(phrase):
        low = (phrase_text or "").lower()
        if not low:
            continue
        ids = sorted(
            int(r["id"]) for r in rows
            if any(low in str(r.get(f) or "").lower() for f in fields)
        )
        if not ids:
            continue
        # 标题级：全部命中都来自 title（没有靠分类列多捞）
        by_title = sorted(
            int(r["id"]) for r in rows
            if low in str(r.get("title") or "").lower()
        )
        tight = bool(by_title) and set(by_title) == set(ids)
        key = (tuple(ids), tight)
        if key not in seen:
            seen.add(key)
            out.append((ids, tight))
    return out


def _hits(phrase: str, rows) -> list:
    """一个词命中的全部 id（各解读的并集）。给测试与调试用。"""
    merged, seen = [], set()
    for ids, _tight in _variants(phrase, rows):
        for i in ids:
            if i not in seen:
                seen.add(i)
                merged.append(i)
    return merged


_KEEP_INTACT = frozenset({"目的", "的了", "为了", "除了", "的了", "别的", "似的", "而已"})


def _prune(term: str) -> str:
    """去掉词**尾**的助词（"八股的" → "八股"）。

    用户/模型给的词常带语气词，"八股的" 这种整串在库里查不到，
    但把"的"去掉就是那个真正的分类词。

    两条约束，都是踩过的坑：
      1. 只剥词尾、循环剥（"八股文的" → "八股文"）。早先写成
         `re.sub(r"[的了呢吧啊嘛呀]|...", "", term)` 会把词**中间**的字也删掉，
         "的目的" 被削成 "目"。
      2. 本来就以"的/了"结尾的词要原样保留（"目的"、"为了"）——
         否则 "目的" 会被削成 "目"，反而更糟。
    """
    out = (term or "").strip()
    if out in _KEEP_INTACT:
        return out
    while len(out) > 1 and out[-1] in "的了呢吧啊嘛呀":
        out = out[:-1].strip()
    return out


def _anchor_options(term: str, rows, min_hits: int, max_hits: int) -> list:
    """给一个词，返回它所有**可用解读**（每个是命中 id 列表，按候选数升序）。

    逐长度层级处理（同一长度上的多个子串要**一起**看，别拿一个就返回，
    否则 "推荐几篇关于八股的" 在第 8 层抓到"关于"就收工，永远退不到"八股"）：
      本层有落在区间内的解读 → 返回**全部**合格解读，让调用方挑（见 anchor_ids：
                              选择必须由交集驱动 —— "数据库"的字面解读命中
                              {75,121}、别名解读命中 {116,118,121}，各自都"可用"，
                              只有字面那个能跟"八股文"求交出 {75}）
      本层只有 <min_hits 的弱命中 → 不够格，继续往短找（"关于"命中 1 篇时
                                   不能一票否决，后面还有"八股"）
      本层有一大堆爆量命中      → 这词太泛、没有收窄能力，放弃
      本层一个都没命中          → 继续往短找
    一直退到底都没有合格层 → []，本次查询里这个词没有发言权。

    两个容易搞混的"下限"：
      min_hits 是**命中篇数**的门槛，不是子串长度 —— 2 个字的"八股"完全可以是
               好锚点（实测命中 12 篇），所以子串长度必须一路退到 2。
      1 个字的子串一律不当锚点：它太脆。"八股文 Linux" 里的"文"命中 5 篇，
               但那只是"八股文"的一个碎片，拿它当锚点会把无关文章捞进来。
    """
    term = _prune(term)
    if not term:
        return []
    for size in range(len(term), 1, -1):
        ranked, weak, broad = [], [], []
        for start in range(0, len(term) - size + 1):
            sub = term[start:start + size]
            # 弱解读层只收**整词**的解读，不收子串的。
            # 踩过的坑：本来想用"子串贴着词头/词尾"当判据，但对 2 字词恒真
            # （"量子计算" 按 2 字切时，"计算" 正好到词尾）—— 于是 "计算" 因为
            # 在 "计算机网络" 里命中 2 篇被当成弱锚点。
            # 真要表达的是：**用户给的那个完整词**在库里命中面小（"操作系统"
            # 字面 2 篇）值得留着参与求交；而**碎片**命中面小只是噪声。
            is_full_term = size == len(term)
            variants = _variants(sub, rows)
            if not variants:
                continue
            merged = sorted({i for ids, _t in variants for i in ids})
            pool = variants + ([(merged, False)] if len(variants) > 1 else [])
            for ids, _tight in pool:
                # "这个解读有没有收窄"（是不是并集的真子集）—— 真收窄的排前面，
                # 否则它会被更大的并集解读盖掉（"Linux" 41 篇 vs 别名 1 篇）。
                narrowed = 0 if (len(ids) < len(merged) and set(ids) < set(merged)) else 1
                entry = (narrowed, len(ids), ids)
                if len(ids) > max_hits:
                    # 命中面太大（"Linux" 41 篇）：单独用没收窄能力，求交才有意义
                    # （"Linux" ∩ "八股文" = id=69），垫最后。
                    broad.append(entry)
                elif len(ids) >= min_hits:
                    ranked.append(entry)
                elif is_full_term:
                    # 整词命中面小（"操作系统" 字面 2 篇）：单独用可能是巧合，
                    # 求交能让它发挥作用，垫在够格的后面。
                    weak.append(entry)
        if ranked or weak or broad:
            ranked.sort(key=lambda x: (x[0], x[1]))
            weak.sort(key=lambda x: (x[0], x[1]))
            broad.sort(key=lambda x: (x[0], x[1]))
            out, seen = [], set()
            for _, _, ids in ranked + weak + broad:
                key = tuple(sorted(ids))
                if key not in seen:
                    seen.add(key)
                    out.append(list(key))
            return out
    return []


def _best_anchor(term: str, rows, min_hits: int, max_hits: int) -> Optional[list]:
    """最紧的那一个解读（保留给测试/调试用；真正的选择在 anchor_ids）。"""
    options = _anchor_options(term, rows, min_hits, max_hits)
    return options[0] if options else None


def anchor_ids(
    phrases,
    rows,
    *,
    min_hits: int = ANCHOR_MIN_HITS,
    max_hits: int = ANCHOR_MAX_HITS,
) -> Optional[list]:
    """把用户说的分类词锚成一批候选 id。

    phrases: 一个短语，或**按可信度排序**的若干短语（优先用 topic 字段拼的那串，
             再退回把 blog_type/tags 也算进来的那串 —— 实测模型有时会把
             "八股文"塞进 tags 而不是 topic）。
             逐个试，**第一个试出结果的短语就是锚点**，后面的不再参与：
             逐个求交会把两次不同的猜测交叉成空集，那是错的。
    rows:    调用方查好，结构 [{"id","title","topic","category","tags"}, ...]
             —— 本函数不查库：本模块不该依赖 db，纯函数也才好测。

    单个短语内部：
      1. 按空格/逗号切词（"C++ 八股文" 是两个词）
      2. 每个词取它所有解读（字面一种 + 各别名一种；含"太宽/太窄"的解读）
      3. 穷举「每个词挑哪种解读 × 丢掉哪些词」的所有组合，求交
      4. 候选按三层判据排序，取第一个：
         a. **交集比组合里任何一个词都小** —— 这才叫真的收窄了
            （"操作系统" 2 篇 ∩ "八股文" 12 篇 = 1 篇，比两边都小 → 要）
         b. 交集大小
         c. 丢掉的词数（越少越好 —— 丢词等于无视用户的限定）
    返回 None = 所有短语都没锚住 → 调用方应当让向量在全库上直出。

    第 3/4 步为什么必须这么绕：解读的取舍**只能由交集决定**。
    实测"数据库 八股文"——"数据库"字面命中 {75,121}、别名(MySQL)命中 {116,118,121}，
    两个解读各自都"可用"，但只有字面那个能跟"八股文"的 12 篇交交出 {75}。
    单纯"取最紧的解读"会选中别名那个，交集为空，白丢一次收窄机会。

    判据 a 为什么必须有：库里一堆词是"太宽"的（"Linux" 41 篇、"算法" 27 篇），
    留着它们参与求交才能把 "Linux 的八股文" 收到 id=69；但如果不要求
    "交完必须更紧"，单独一个宽词也会被当成锚点（"推荐几篇 C++ 的文章"
    会锚成 36 篇，等于没筛）。加上判据 a，宽词只在**配角**位置起作用。
    """
    if isinstance(phrases, str) or phrases is None:
        phrases = [phrases]

    for phrase in phrases:
        terms = [t.strip() for t in ANCHOR_SPLIT.split(str(phrase or "")) if t.strip()]
        if not terms:
            continue
        options = [_anchor_options(t, rows, min_hits, max_hits) for t in terms]
        if not any(options):
            continue
        # 各词的第 k 紧解读**不必配套**，所以做笛卡尔积而不是按位取同一个 k。
        order = sorted(
            range(len(terms)),
            key=lambda i: len(options[i][0]) if options[i] else 10 ** 9,
        )
        combos = []
        single = len([o for o in options if o]) == 1
        for drop in range(len(order)):
            picks = [options[i] for i in order[drop:] if options[i]]
            if not picks:
                continue
            for combo in itertools.product(*picks):
                got = set(combo[0])
                for other in combo[1:]:
                    got &= set(other)
                if not got:
                    continue
                widest = max(len(c) for c in combo)   # 组合里最宽的那个词
                combos.append({
                    "ids": sorted(got),
                    "n": len(got),
                    "drop": drop,
                    # "真的收窄了"：交集比组合里任何一个词都小
                    "narrowed": len(got) < widest,
                    "in_range": min_hits <= len(got) <= max_hits,
                    # 只有一个词能锚时，它必须自己就落在命中区间内。
                    # 弱解读层（不够 min_hits 也留着）只服务**多词求交** ——
                    # 求交能验证它，单独用则不能。
                    "usable": (min_hits <= len(got) <= max_hits) if single else True,
                })
        combos = [c for c in combos if c["usable"]]
        if combos:
            combos.sort(key=lambda c: (
                0 if c["narrowed"] else 1,          # 优先真的收窄了的组合
                0 if c["in_range"] else 1,
                c["n"],                             # 再比候选数（越少越精确）
                c["drop"],                          # 最后比丢词数
            ))
            return combos[0]["ids"]
    return None


def rank_within(candidates, hits) -> Optional[list]:
    """用向量分数给**候选内**的文章排序，候选外的一律丢弃。

    交集为空 = 这个词法锚点跟语义对不上（或索引没建/挂了）→ 返回 None，
    调用方应当放弃这次收窄、退回全库向量召回，别拿词法顺序硬撑。
    """
    allowed = set(int(i) for i in (candidates or []))
    if not allowed:
        return None
    ranked = [int(i) for i, _ in (hits or []) if int(i) in allowed]
    return ranked or None


def search_ids(
    query: str,
    k: int = RECALL_TOP_K,
    score_min: float = RECALL_SCORE_MIN,
) -> Optional[list]:
    """按意思挑文章，返回 [(id, 分数), ...]。

    两道门槛，缺一不可（原因见文件上方那段实测数据）：
      1. top1 得 >= score_min，否则整个查询判定为"站里没这个方向"，返回 []
      2. 逐条只收 >= max(RECALL_ITEM_MIN, top1 - RECALL_SPREAD) 的
    """
    store = get_store()
    if store is None:
        return None
    if not query or not str(query).strip():
        return []

    try:
        hits = store.similarity_search_with_score(str(query).strip(), k=max(int(k), 1))
    except Exception as exc:  # noqa: BLE001 网络/限流/欠费都走同一条回落路径
        print(f"[recall] 检索失败（{type(exc).__name__}: {exc}），回落 SQL 链路")
        return None

    if not hits:
        return []
    top1 = float(hits[0][1])
    if top1 < float(score_min):
        print(f"[recall] 最像的一篇才 {top1:.3f}（门槛 {score_min}），当作站里没有")
        return []

    # 过了门之后，尾巴收到哪：跟着 top1 浮动，别把弱相关的也捞进来
    floor = max(float(RECALL_ITEM_MIN), top1 - RECALL_SPREAD)

    out = []
    for doc, score in hits:
        if float(score) < floor:
            break  # 已按分数降序，后面只会更低
        doc_id = (doc.metadata or {}).get("id")
        if doc_id is None:
            continue
        out.append((int(doc_id), float(score)))
    return out
