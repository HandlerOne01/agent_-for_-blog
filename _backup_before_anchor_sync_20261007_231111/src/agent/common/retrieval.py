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
import os
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
