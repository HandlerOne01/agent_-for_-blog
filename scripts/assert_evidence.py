# -*- coding: utf-8 -*-
"""对存档的逐字证据做机器校验（任务 4 的硬指标）。

校验对象：verify_v3_reco.txt 里每条问答「最后一条 AI 消息」（=爱弥斯真正说出口的话）。
只读存档，不发任何请求。
"""
import io
import os
import re
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(PROJ, "verify_v3_reco.txt")


def split_cases(text):
    """按 '=====' 分隔符切出每条问答，返回 [(question, [lines])]。"""
    blocks = re.split(r"\n={78}\n", text)
    cases = []
    for b in blocks:
        m = re.search(r"^USER> (.*)$", b, re.M)
        if not m:
            continue
        cases.append((m.group(1).strip(), b.splitlines()))
    return cases


def final_ai(lines):
    """取最后一条「[ai] 或 [ai] 续行」组成的正文（跳过 [user] / [tool..] / [INTERRUPT]）。"""
    idx = None
    for i, l in enumerate(lines):
        if l.startswith("[ai] "):
            idx = i
    if idx is None:
        return ""
    out = [lines[idx][len("[ai] "):]]
    for l in lines[idx + 1:]:
        if l.startswith(("[ai]", "[user]", "[tool", "[INTERRUPT]", "[RESUME]")):
            break
        out.append(l)
    return "\n".join(out).strip()


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    with io.open(PATH, encoding="utf-8") as f:
        text = f.read()
    cases = split_cases(text)
    print("存档: %s" % os.path.basename(PATH))
    print("问答条数: %d" % len(cases))
    print("=" * 78)

    all_ok = True
    for q, lines in cases:
        ans = final_ai(lines)
        checks = []
        # 1) 不得出现「您」
        checks.append(("无「您」", "您" not in ans))
        # 2) 不得出现 markdown 表格
        checks.append(("无表格", not re.search(r"^\s*\|.*\|\s*$", ans, re.M)))
        # 3) 不得出现加粗
        checks.append(("无加粗", "**" not in ans))
        # 4) 不得出现域名
        checks.append(("无域名链接", not re.search(r"https?://", ans, re.I)))
        # 5) 若给了链接，必须是站内相对路径
        urls = re.findall(r"/posts/[A-Za-z0-9_\-./]*", ans)
        checks.append(("链接为 /posts/ 相对路径",
                       not urls or all(u.startswith("/posts/") for u in urls)))
        # 6) 不得出现客服腔关键词
        svc = ["以下是您的", "为您", "希望对您", "还有其他问题", "请稍等",
               "感谢您的", "很高兴为您服务", "欢迎光临"]
        checks.append(("无客服腔", not any(s in ans for s in svc)))
        # 7) 说「共 N 篇」时，条目数必须等于 N（133 是站内总数，不算）
        #    只认**计数断言**，不认「最短的一篇」「剩下三篇」这种叙述性用量。
        #    （前两版校验器就是在这里翻车的：先漏了中文数字，又把「一篇」当成了断言。）
        NUM = r"(\d+|[一两二三四五六七八九十]+)"
        claim_res = [
            r"共\s*" + NUM + r"\s*篇",
            r"一共\s*" + NUM + r"\s*篇",
            r"这\s*" + NUM + r"\s*篇",
            r"那\s*" + NUM + r"\s*篇",
            r"找到\s*" + NUM + r"\s*篇",
            r"有\s*" + NUM + r"\s*篇",
            NUM + r"\s*篇\s*(?:都|全|均|是)",
        ]

        def to_int(tok):
            if tok.isdigit():
                return int(tok)
            unit = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9}
            if tok == "十":
                return 10
            if tok.startswith("十"):
                return 10 + unit.get(tok[1:], 0)
            if tok.endswith("十"):
                return unit.get(tok[:-1], 0) * 10
            if "十" in tok:
                a, _, b = tok.partition("十")
                return unit.get(a, 0) * 10 + unit.get(b, 0)
            return unit.get(tok, 0)

        claims, phrases = [], []
        for pat in claim_res:
            for m in re.finditer(pat, ans):
                val = to_int(m.group(1))
                if val == 133:
                    continue          # 站内总数，不是本轮计数
                claims.append(val)
                phrases.append(m.group(0))
        n_items = len(set(urls))
        if claims:
            checks.append(
                ("计数断言 %s == 条目数 %d" % (sorted(set(claims)), n_items),
                 all(c == n_items for c in claims))
            )
        else:
            checks.append(("本轮无计数断言", True))

        ok = all(v for _, v in checks)
        all_ok = all_ok and ok
        print()
        print("Q: %s" % q)
        print("  最终答复:")
        for l in ans.splitlines():
            print("    | " + l)
        print("  校验:")
        for name, v in checks:
            print("    [%s] %s" % ("PASS" if v else "FAIL", name))

    print()
    print("=" * 78)
    print("总体: %s" % ("全部通过" if all_ok else "存在失败项"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
