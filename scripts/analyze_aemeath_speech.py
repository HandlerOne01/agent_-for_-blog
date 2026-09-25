# -*- coding: utf-8 -*-
"""独立复核：直接抓 fandom zh wiki 的 wikitext，自己数一遍爱弥斯台词的语用特征。

为什么不用 web_fetch：本机 DNS 把域名解析到非公网 IP，web_fetch 全灭；
但 PowerShell/requests 走得通，且 /wiki/ 页面 403 而 api.php 200。
所以这里走 MediaWiki API。

这个脚本只做**统计**，不把语料写进人设；输出的是计数与分布，
不是台词本身（台词原文只用于本地计数，不落盘）。
"""
import io
import json
import re
import sys

import requests

API = "https://wutheringwaves.fandom.com/zh/api.php"
UA = {"User-Agent": "Mozilla/5.0 (research; persona style analysis)"}

PAGES = ["远航星", "昨夜群星", "我们选择天空"]


def fetch_wikitext(page):
    r = requests.get(
        API,
        params={
            "action": "parse",
            "page": page,
            "prop": "wikitext",
            "format": "json",
            "formatversion": "2",
        },
        headers=UA,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        return None, data["error"].get("info", "unknown")
    return data["parse"]["wikitext"], None


def extract_lines(wikitext):
    """抽出「爱弥斯：...」形式的台词行。

    对话页常见格式：* 爱弥斯：xxx  或  ;爱弥斯：xxx  或  {{对话|爱弥斯|xxx}}
    这里把几种都覆盖，宁可少收也不要串行。
    """
    lines = []
    for raw in wikitext.splitlines():
        s = raw.strip()
        # 去掉列表符号
        s = re.sub(r"^[\*;:#]+\s*", "", s)
        m = re.match(r"^(?:<[^>]+>)?\s*爱弥斯\s*[:：]\s*(.+)$", s)
        if m:
            txt = m.group(1).strip()
            if txt and not txt.startswith("["):
                lines.append(txt)
            continue
        m2 = re.match(r"^\{\{\s*对话\s*\|\s*爱弥斯\s*\|\s*(.+?)\s*\}\}", s)
        if m2:
            lines.append(m2.group(1).strip())
    return lines


def analyze(lines):
    joined = "\n".join(lines)
    n = len(lines)
    lens = [len(x) for x in lines]
    particles = ["吧", "呢", "嘛", "哦", "啦", "啊", "呀", "诶", "唉"]
    res = {}
    res["句数"] = n
    res["自称我"] = sum(1 for x in lines if "我" in x)
    res["自称人家/本姑娘/咱/本人"] = sum(
        1 for x in lines if re.search(r"人家|本姑娘|咱|本人", x)
    )
    res["语气词(行级命中)"] = {p: sum(1 for x in lines if p in x) for p in particles}
    res["半角~句数"] = sum(1 for x in lines if "~" in x)
    res["全角～句数"] = sum(1 for x in lines if "～" in x)
    res["省略号句数"] = sum(1 for x in lines if "……" in x or "..." in x)
    res["破折号句数"] = sum(1 for x in lines if "——" in x)
    res["哈哈"] = len(re.findall(r"哈哈", joined))
    res["哈哈(叠3+)"] = len(re.findall(r"哈哈{2,}", joined))
    res["平均字数"] = round(sum(lens) / n, 1) if n else 0
    res["字数<=10"] = sum(1 for L in lens if L <= 10)
    res["字数11-20"] = sum(1 for L in lens if 11 <= L <= 20)
    res["字数21-30"] = sum(1 for L in lens if 21 <= L <= 30)
    res["字数>30"] = sum(1 for L in lens if L > 30)
    res["固定说法:看，"] = len(re.findall(r"看，", joined))
    res["固定说法:好玩"] = len(re.findall(r"好玩", joined))
    res["固定说法:超级"] = len(re.findall(r"超级", joined))
    res["固定说法:挺不错"] = len(re.findall(r"挺不错", joined))
    res["对漂泊者称呼:漂泊者"] = len(re.findall(r"漂泊者", joined))
    res["对漂泊者称呼:同桌"] = len(re.findall(r"同桌", joined))
    res["对漂泊者称呼:救世主"] = len(re.findall(r"救世主", joined))
    res["叠词:对吧对吧|我懂我懂"] = len(re.findall(r"对吧对吧|我懂我懂", joined))
    res["顿号拆字"] = len(re.findall(r"[^\s、]{1}、[^\s、]{1}、[^\s、]{1}", joined))
    return res


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    all_lines = []
    for page in PAGES:
        try:
            wt, err = fetch_wikitext(page)
        except Exception as exc:  # noqa: BLE001
            print("  [%s] 抓取失败: %s: %s" % (page, type(exc).__name__, exc))
            continue
        if wt is None:
            print("  [%s] API 报错: %s" % (page, err))
            continue
        lines = extract_lines(wt)
        print("  [%s] wikitext %d 字符 -> 爱弥斯台词 %d 句" % (page, len(wt), len(lines)))
        all_lines.extend(lines)

    print()
    print("=" * 70)
    print("独立复核结果（%d 句）" % len(all_lines))
    print("=" * 70)
    if not all_lines:
        print("没抓到任何台词 —— 复核失败，不能采信任何统计结论。")
        return 1
    res = analyze(all_lines)
    for k, v in res.items():
        print("  %-28s %s" % (k, v))

    # 抽查：极短句（<=8 字）是不是"去掉语气词的真心话"这一型
    short = [x for x in all_lines if len(x) <= 8]
    print()
    print("  <=8 字的极短句共 %d 句；其中不含任何语气词的 %d 句"
          % (len(short), sum(1 for x in short
                             if not re.search(r"[吧呢嘛哦啦啊呀诶唉]", x))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
