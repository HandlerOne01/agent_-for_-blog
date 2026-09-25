# -*- coding: utf-8 -*-
"""check_no_verbatim.py 的「代理取数」版：直连被墙时，用可达代理拉**同一份** wikitext，
再调用 check_no_verbatim 里**原封不动**的抽取与判定逻辑（只替换取数这一步）。

为什么需要它
------------
本机到 `wutheringwaves.fandom.com` 直连超时（ConnectTimeout，fandom 在国内不可达）。
而 check_no_verbatim.py 的 get_wikitext 里 `except Exception: continue` 会把
网络失败**静默吞掉**，最终输出「语料 0 句 → 这项校验无法完成」并 return 2。
静默失败比失败更危险：它极容易被误读成"跑过了、没问题"。

所以这个脚本做三件事，把不确定性挤干净：
  1. 显式取数：直连优先，失败则走 `/raw?url=` 代理（内容仍是 fandom 的原始 JSON）。
  2. **核对语料规模**：剧情 267 句 + 语音 380 句 —— 与 v4 那次校验用的语料**同规模**
     才继续；不一致就明确报出来（wiki 可能被改过），不冒充"同一份语料"。
  3. 把取数函数替换成"读缓存"，然后直接跑 check_no_verbatim.main()：
     阈值（>=8 汉字）、normalize、最长公共子串算法**全部是原脚本的代码**。

语料只落在临时目录，**不进版本库、不转贴**（沿用 check_no_verbatim 的自我约束：
输出只给重合片段与来源，不整段贴官方文本）。
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import check_no_verbatim as cnv  # noqa: E402  （同目录脚本，直接复用它的逻辑）

ZH_PAGES = ("远航星", "昨夜群星", "我们选择天空")
EN_PAGE = "Aemeath/Voicelines/Chinese"

# v4 那次校验记录的语料规模（见 agent/persona-v4-notes-20260925.md）
EXPECT_STORY = 267
EXPECT_VOICE = 380

PROXY = "https://api.allorigins.win/raw?url="
PROXY_GET = "https://api.allorigins.win/get?url="
UA = {"User-Agent": "Mozilla/5.0 (compliance check)"}


def _api_url(api, page):
    from urllib.parse import quote
    return (
        "%s?action=parse&page=%s&prop=wikitext&format=json&formatversion=2"
        % (api, quote(page, safe=""))
    )


def _proxy_variants(direct):
    """代理的几种写法，任一种通就行（allorigins 的 522 是它那一侧的瞬时故障，重试多半能过）。"""
    from urllib.parse import quote
    q = quote(direct, safe="")
    return [
        ("proxy-raw", PROXY + q, False),
        ("proxy-get", PROXY_GET + q, True),  # /get 返回 {"contents": "<原文>"}
    ]


def fetch_json(api, page, cache_dir, retries=4):
    """先直连，再走代理（多种写法、多次重试）。返回 (wikitext, 来源说明)。"""
    cache = os.path.join(cache_dir, "%s.wikitext.txt" % hashlib.sha256(
        (api + "|" + page).encode("utf-8")).hexdigest()[:16])
    if os.path.exists(cache):
        with io.open(cache, encoding="utf-8") as f:
            return f.read(), "cache"

    direct = _api_url(api, page)
    attempts = [("direct", direct, False)]
    for _ in range(retries):
        attempts.extend(_proxy_variants(direct))

    last = None
    for how, url, wrapped in attempts:
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            if wrapped:
                outer = r.json()
                raw = outer.get("contents")
                if not raw:
                    last = "%s: contents 为空" % how
                    continue
                d = json.loads(raw)
            else:
                d = r.json()
            if "error" in d:
                last = "%s: api error %s" % (how, d["error"])
                continue
            wt = d["parse"]["wikitext"]
            with io.open(cache, "w", encoding="utf-8") as f:
                f.write(wt)
            return wt, how
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s %s" % (how, type(exc).__name__, str(exc)[:80])
            time.sleep(1.5)
    print("  !! 取数失败 %s / %s -> %s" % (api, page, last))
    return None, "failed"


def max_common_substring(official, persona, cap=40):
    """official/persona 都已 normalize。返回两者最长公共子串长度（上限 cap）。"""
    top = min(len(official), cap)
    for k in range(top, 0, -1):
        for i in range(len(official) - k + 1):
            if official[i:i + k] in persona:
                return k
    return 0


def out(msg):
    """自己写 UTF-8 字节，**不碰 sys.stdout 对象**。

    踩过的坑：如果这里先把 sys.stdout 换成 io.TextIOWrapper，等会儿
    check_no_verbatim.main() 又换一次，前一个 wrapper 被 GC 时会把底层 buffer
    一起关掉，于是 main() 的第一行 print 就炸 `I/O operation on closed file`。
    让 sys.__stdout__ 一直持有原始对象，就不会有人去关它。
    """
    sys.stdout.buffer.write((msg + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "aemeath_corpus"))
    args = ap.parse_args()
    os.makedirs(args.cache, exist_ok=True)

    out("== 第 1 步：取官方语料（直连 -> 代理）==")
    store = {}
    for page in ZH_PAGES:
        wt, how = fetch_json(cnv.ZH_API, page, args.cache)
        store[(cnv.ZH_API, page)] = wt
        out("  [%s] %-14s %s" % (how, page, ("%d 字符" % len(wt)) if wt else "取不到"))
    wt, how = fetch_json(cnv.EN_API, EN_PAGE, args.cache)
    store[(cnv.EN_API, EN_PAGE)] = wt
    out("  [%s] %-14s %s" % (how, EN_PAGE, ("%d 字符" % len(wt)) if wt else "取不到"))

    if any(v is None for v in store.values()):
        out("\n中止：语料没取全，本次**不能**宣称'已确认无重合'。")
        return 2

    # 把取数替换成读缓存；其余逻辑全部沿用 check_no_verbatim
    cnv.get_wikitext = lambda api, page: store.get((api, page))
    out("  （已把 check_no_verbatim.get_wikitext 替换为读缓存；阈值/算法未动）")

    out("\n== 第 2 步：核对语料规模（必须与 v4 那次同规模）==")
    sl = cnv.story_lines()
    vl = cnv.voicelines()
    out("  剧情台词 %d 句（v4 记录 %d）%s" % (len(sl), EXPECT_STORY,
                                          "OK" if len(sl) == EXPECT_STORY else "**不一致**"))
    out("  中文语音 %d 句（v4 记录 %d）%s" % (len(vl), EXPECT_VOICE,
                                          "OK" if len(vl) == EXPECT_VOICE else "**不一致**"))
    same_corpus = (len(sl) == EXPECT_STORY and len(vl) == EXPECT_VOICE)
    if not same_corpus:
        out("  !! 语料规模与 v4 记录不同 —— 下面结论只能算"
            "'对本次抓到的语料'成立，不能冒充'与 v4 同一份语料'。")

    out("\n== 第 3 步：跑 check_no_verbatim.main()（原逻辑）==")
    rc = cnv.main()
    # main() 把 sys.stdout 换成了自己的 TextIOWrapper；先把它冲干净，
    # 否则它的输出会全部堆到第 4 步后面，读起来像"结论在前、证据在后"。
    sys.stdout.flush()

    out("\n== 第 4 步：真实的最长公共子串（原脚本只在 >=8 时打印，这里给出确切数字）==")
    with io.open(cnv.PERSONA, encoding="utf-8") as f:
        persona = cnv.normalize(f.read())
    best = (0, None, None)
    for kind, line in [("剧情", x) for x in sl] + [("语音", x) for x in vl]:
        n = cnv.normalize(line)
        if not n:
            continue
        L = max_common_substring(n, persona)
        if L > best[0]:
            frag = None
            for i in range(len(n) - L + 1):
                if n[i:i + L] in persona:
                    frag = n[i:i + L]
                    break
            best = (L, kind, frag)
    out("  语料句数 %d（剧情 %d + 语音 %d）" % (len(sl) + len(vl), len(sl), len(vl)))
    out("  最长公共子串 = %d 字" % best[0])
    out("  出处类别 = %s   片段 = %s" % (best[1], best[2]))
    out("  阈值 %d：%s" % (cnv.THRESHOLD,
                        "无 >=%d 字重合" % cnv.THRESHOLD if best[0] < cnv.THRESHOLD
                        else "**有 >=%d 字重合，需人工判读**" % cnv.THRESHOLD))
    if not same_corpus:
        out("  注意：语料规模与 v4 记录不一致，本条结论的适用范围见上。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
