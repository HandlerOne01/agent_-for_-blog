"""共享的 LLM 实例。

走 DeepSeek 官方的 OpenAI 兼容接口（langchain_openai.ChatOpenAI）。
模型名 / base_url 可通过 .env 覆盖，方便换成其它 OpenAI 兼容服务。
"""

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def _env_temperature(name: str, default: float) -> float:
    """.env 里的温度值。写错了就退回默认值，不让一个笔误把服务拦在启动阶段。"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[llm] {name}={raw!r} 不是合法浮点数，改用默认 {default}")
        return default


# 面向用户的"说话"温度。默认 0.8（v3 起，原来是写死的 0）。
#
# 为什么调高：temperature=0 时模型几乎总挑概率最高的那个说法，结果是
# 每一句都"对"、但是模板味极重 —— 同一个问题问两次，答复一模一样，
# 爱弥斯就成了一个词库固定的客服机器人。人设文本再细也救不回来。
# 0.8 是"像人在说话"和"不跑偏"之间比较稳的一档。
#
# 风险（务必知情）：
#   1. 会跑偏 —— 推荐语里更可能出现措辞漂移。所以推荐路径的
#      篇数/标题/链接不靠这个温度保证，而靠 compose_answer 的确定性校验兜底。
#   2. 不利于需要复现的取数/抽取 —— SQL 生成、信息抽取、意图识别因此
#      继续用下面的 model_deterministic（默认 0），别把它们一起调高。
DEEPSEEK_TEMPERATURE = _env_temperature("DEEPSEEK_TEMPERATURE", 0.8)

# 取数与路由用的低温度模型：这些地方要的是"每次都一样"，不是文采。
DEEPSEEK_TEMPERATURE_DETERMINISTIC = _env_temperature(
    "DEEPSEEK_TEMPERATURE_DETERMINISTIC", 0.0
)

# .env 里写的是 DeepSeek_API_KEY；Windows 的环境变量名大小写不敏感，
# 这里按惯例用大写读取。
_api_key = os.getenv("DEEPSEEK_API_KEY")

if not _api_key:
    raise RuntimeError(
        "未找到 DEEPSEEK_API_KEY。请在项目根目录的 .env 中配置："
        "DEEPSEEK_API_KEY=sk-....（可参考 .env.example）"
    )

model = ChatOpenAI(
    model=DEEPSEEK_MODEL,
    api_key=_api_key,
    base_url=DEEPSEEK_BASE_URL,
    temperature=DEEPSEEK_TEMPERATURE,
)

# 取数 / 抽取 / 路由专用：低温度，保证同一输入给同一结果。
model_deterministic = ChatOpenAI(
    model=DEEPSEEK_MODEL,
    api_key=_api_key,
    base_url=DEEPSEEK_BASE_URL,
    temperature=DEEPSEEK_TEMPERATURE_DETERMINISTIC,
)
