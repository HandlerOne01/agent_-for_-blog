# -*- coding: utf-8 -*-
"""合规校验：确认人设文件里**没有任何官方台词原文**。

做法：把能拿到的官方/游戏内文本（剧情台词 + 中文语音全文）拉下来，
逐条与人设做**最长公共子串**检查；只要出现 >= 阈值长度的重合就报出来。

阈值取 8 个汉字：常用套话（「我觉得」「不好意思」）本来就会撞，
8 字以上的连续重合才说明是照抄。

只读，不写人设；输出只给"重合片段"与来源，不整段转贴语料。
"""
import io
import os
import re
import sys

import requests

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONA = os.path.join(PROJ, "src", "agent", "common", "persona.txt")

ZH_API = "https://wutheringwaves.fandom.com/zh/api.php"
EN_API = "https://wutheringwaves.fandom.com/api.php"
UA = {"User-Agent": "Mozilla/5.0 (compliance check)"}
THRESHOLD = 8


def get_wikitext(api, page):
    r = requests.get(
        api,
        params={"action": "parse", "page": page, "prop": "wikitext",
                "format": "json", "formatversion": "2"},
        headers=UA, timeout=60,
    )
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        return None
    return d["parse"]["wikitext"]


def story_lines():
    out = []
    for page in ("远航星", "昨夜群星", "我们选择天空"):
        try:
            wt = get_wikitext(ZH_API, page)
        except Exception:
            continue
        if not wt:
            continue
        for raw in wt.splitlines():
            s = re.sub(r"^[\*;:#]+\s*", "", raw.strip())
            m = re.match(r"^(?:<[^>]+>)?\s*爱弥斯\s*[:：]\s*(.+)$", s)
            if m and m.group(1).strip():
                out.append(m.group(1).strip())
    return out


def voicelines():
    """中文语音全文（含简繁），抽成句子列表。"""
    try:
        wt = get_wikitext(EN_API, "Aemeath/Voicelines/Chinese")
    except Exception:
        return []
    if not wt:
        return []
    txt = re.sub(r"\{\{[^{}]*\}\}", " ", wt)
    txt = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("'''", "").replace("''", "")
    parts = re.split(r"[\n。！？!?；;]+", txt)
    return [p.strip() for p in parts if 6 <= len(p.strip()) <= 120]


def normalize(s):
    """只留汉字与常用标点，去掉空白与拉丁字符，避免排版差异造成假阴性。"""
    return re.sub(r"[^\u4e00-\u9fff]", "", s)


def longest_common_substring_len(a, b, cap=40):
    """a、b 的最长公共子串长度（a 是短的人设片段，b 是官方句子）。"""
    if not a or not b:
        return 0
    best = 0
    # 以 a 的每个位置起头，往 b 里找
    for i in range(len(a)):
        if len(a) - i <= best:
            break
        for j in range(len(b)):
            k = 0
            while (i + k < len(a) and j + k < len(b)
                   and a[i + k] == b[j + k] and k < cap):
                k += 1
            if k > best:
                best = k
    return best


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    with io.open(PERSONA, encoding="utf-8") as f:
        persona_raw = f.read()
    persona = normalize(persona_raw)
    print("人设: %s（%d 字节，规范化后 %d 汉字）"
          % (os.path.basename(PERSONA), len(persona_raw.encode("utf-8")), len(persona)))

    corpus = []
    sl = story_lines()
    print("语料: 剧情台词 %d 句" % len(sl))
    corpus.extend(("剧情", x) for x in sl)
    vl = voicelines()
    print("语料: 中文语音句 %d 句" % len(vl))
    corpus.extend(("语音", x) for x in vl)

    if not corpus:
        print("!! 一句语料都没抓到 —— 这项校验无法完成，不能宣称'已确认无重合'。")
        return 2

    hits = []
    for kind, line in corpus:
        n = normalize(line)
        if len(n) < THRESHOLD:
            continue
        L = longest_common_substring_len(n, persona, cap=THRESHOLD + 12)
        if L >= THRESHOLD:
            # 找出这段重合
            frag = None
            for i in range(len(n) - L + 1):
                cand = n[i:i + L]
                if cand in persona:
                    frag = cand
                    break
            hits.append((L, kind, frag, line[:24]))

    print()
    print("=" * 70)
    print("阈值：连续重合 >= %d 个汉字即视为可疑照抄" % THRESHOLD)
    print("=" * 70)
    if not hits:
        print("结果：**没有任何 >= %d 字的连续重合**。" % THRESHOLD)
        print("      -> 人设里不存在官方台词原文。")
        return 0

    hits.sort(reverse=True)
    print("结果：发现 %d 处 >= %d 字重合：" % (len(hits), THRESHOLD))
    for L, kind, frag, head in hits[:20]:
        print("  [%2d 字][%s] 重合片段=%s   （官方句开头：%s…）"
              % (L, kind, frag, head))
    return 1


if __name__ == "__main__":
    sys.exit(main())
