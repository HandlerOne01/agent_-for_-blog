# -*- coding: utf-8 -*-
"""DB 降级验证（原子操作，绝不留下被改过的 .env）。

做法：把 .env 里的 DB_NAME 临时指向一个**不存在**的库，
启动服务 → 验证服务仍能起来 / 闲聊可用 / 推荐给人设化"暂时不可用"提示，
然后在 finally 里把 .env **按原字节**写回并校验 sha256。

为什么不用 --env-file：langgraph CLI 没有这个开关；langgraph.json 的 "env"
指向 .env，CLI 读它会盖掉进程里的同名环境变量（实测 DB_NAME=... 不生效）。
所以只能临时改 .env —— 但全程在一个进程内完成，原字节不出内存，
不落任何 .env 副本，也就不会多出一份带密钥的文件。

生产库一个字节都不动：改的只是"连哪个库"这个名字，且指向的是不存在的库。
"""
import hashlib
import io
import os
import re
import subprocess
import sys
import time

import requests

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(PROJ, ".env")
BAD_DB = "my_blog_nonexistent_v3test"
BASE = "http://127.0.0.1:2024"


def redact(text):
    """把 .env 里的密钥值从输出里抹掉。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for line in original_env.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if key.strip() in ("DB_HOST", "DB_PORT", "DB_NAME", "DEEPSEEK_MODEL",
                           "DEEPSEEK_BASE_URL", "LANGSMITH_PROJECT",
                           "LANGSMITH_TRACING"):
            continue
        if len(val) >= 6:
            text = text.replace(val, "***REDACTED***")
    return text


with open(ENV, "rb") as f:
    original_bytes = f.read()
original_env = original_bytes.decode("utf-8")
original_sha = hashlib.sha256(original_bytes).hexdigest().upper()

patched = re.sub(
    r"(?m)^(\s*DB_NAME\s*=).*$", r"\g<1>" + BAD_DB, original_env
)
assert patched != original_env, "DB_NAME 行没被改到，检查 .env 格式"
patched_bytes = patched.encode("utf-8")

proc = None
rc = 0
try:
    with open(ENV, "wb") as f:
        f.write(patched_bytes)
    print("[1] .env 已临时改写：DB_NAME -> %s" % BAD_DB)
    print("    .env sha256 (原始) = %s" % original_sha)
    print("    .env sha256 (临时) = %s"
          % hashlib.sha256(patched_bytes).hexdigest().upper())

    print("[2] 启动服务（--no-reload）...")
    # 注意：日志必须落到文件，不能用 subprocess.PIPE —— 没人读的管道
    # 会在缓冲区写满时把子进程卡死（第一次跑就是这么"起不来"的）。
    log_path = os.path.join(PROJ, "db_degrade_server.log")
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [os.path.join(PROJ, ".venv", "Scripts", "langgraph.exe"),
         "dev", "--port", "2024", "--host", "127.0.0.1",
         "--no-browser", "--no-reload"],
        cwd=PROJ,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    ok = False
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            print("    !! 进程已退出，exit=%s" % proc.returncode)
            break
        try:
            r = requests.get(BASE + "/ok", timeout=5)
            if r.status_code == 200:
                print("[3] GET /ok -> %d %s" % (r.status_code, r.text))
                ok = True
                break
        except Exception:
            pass
        time.sleep(1.5)

    if not ok:
        print("    !! 服务未在 180s 内就绪 —— 降级失败")
        try:
            log_fh.flush()
            with open(log_path, "rb") as f:
                tail = f.read().decode("utf-8", "replace").splitlines()[-50:]
            print("    ---- 服务日志尾部 ----")
            for line in tail:
                print("    | " + redact(line))
            print("    ----------------------")
        except OSError:
            pass
        rc = 1
    else:
        print("[4] 闲聊（应正常，证明图加载成功、闲聊不受数据库影响）")
        r = requests.post(
            BASE + "/runs/wait",
            json={
                "assistant_id": "blog_agent",
                "input": {"messages": [{"role": "user", "content": "在吗"}]},
                "context": {"user_id": "degrade-test"},
            },
            timeout=300,
        )
        data = r.json()
        for m in data.get("messages") or []:
            t = m.get("type")
            if t == "ai" and str(m.get("content") or "").strip():
                print("    [ai] %s" % m["content"])

        print("[5] 推荐（库不可达：应给出人设化的「暂时不可用」，不许编文章）")
        tid = requests.post(BASE + "/threads", json={}, timeout=30).json()["thread_id"]
        r = requests.post(
            BASE + "/threads/%s/runs/wait" % tid,
            json={
                "assistant_id": "blog_agent",
                "input": {
                    "messages": [{"role": "user", "content": "推荐几篇讲 Linux 的博客"}]
                },
                "context": {"user_id": "degrade-test"},
            },
            timeout=300,
        )
        data = r.json()
        for _ in range(2):
            intr = data.get("__interrupt__")
            if not intr:
                break
            val = intr[0].get("value") if isinstance(intr[0], dict) else intr[0]
            print("    [INTERRUPT] %s" % val)
            r = requests.post(
                BASE + "/threads/%s/runs/wait" % tid,
                json={"assistant_id": "blog_agent", "command": {"resume": "不提供"}},
                timeout=300,
            )
            data = r.json()
        final = None
        for m in data.get("messages") or []:
            if m.get("type") == "ai" and str(m.get("content") or "").strip():
                final = m["content"]
        print("    [最终答复] %s" % final)
        if final and "连不上" in final:
            print("    -> 降级文案命中（人设化「暂时不可用」）")
        else:
            print("    -> !! 未命中降级文案")
            rc = 1
        if final and "/posts/" in final:
            print("    -> !! 答复里出现了文章链接，可能是编的")
            rc = 1
finally:
    if proc is not None and proc.poll() is None:
        print("[6] 停止服务 ...")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        print("    服务已停，exit=%s" % proc.returncode)
    with open(ENV, "wb") as f:
        f.write(original_bytes)
    restored_sha = hashlib.sha256(open(ENV, "rb").read()).hexdigest().upper()
    print("[7] .env 已还原，sha256 = %s" % restored_sha)
    print("    与原始一致: %s" % (restored_sha == original_sha))
    if restored_sha != original_sha:
        rc = 1

sys.exit(rc)
