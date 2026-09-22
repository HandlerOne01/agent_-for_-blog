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
    temperature=0,
)
