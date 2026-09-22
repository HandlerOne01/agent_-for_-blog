"""角色人设（Persona）—— 助手「对用户说话」时统一使用的系统提示词。

为什么单独放一个文件，而不是：

① 硬编码在各个节点里？
   面向用户的节点有 3 个（extend_node / get_user_preferences / call_collects）。
   如果把同一份人设抄三遍，改一处忘两处，同一个角色在不同场景下说话风格就不一致，
   用户会觉得"精神分裂"。抽成公共来源才能保证一致。

② 放进数据库？
   人设是【静态配置】，不是【用户数据】：
     - 放代码/文件：有 Git 版本、能 review、能回滚、零网络延迟、不依赖数据库可用性
     - 放数据库  ：多一次查询、多一个故障点、改错了没有 diff 可看
   只有当"需要不重新部署就改人设"或"多角色可切换"时，数据库才划算。
   真正该进数据库的是【每个用户各自的记忆】（比如怎么称呼他、聊过什么），
   那属于 Store，见 common/store.py，别和人设混在一起。

③ 为什么不直接写在 .py 里？
   .txt 让非开发同学也能改文案，且不需要动代码结构。
   加载在导入期完成，运行时零开销。
"""
import os
import warnings

_PERSONA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt")


def _load_persona() -> str:
    """读取人设文件。文件缺失时降级为空串，不让人设问题拖垮整个 Agent。"""
    try:
        with open(_PERSONA_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        # 仍然降级（不让人设问题拖垮 Agent），但必须出声：
        # 之前这里完全静默，文件少个 .txt 后缀就让 Agent 忘了自己是谁，极难排查。
        warnings.warn(
            f"未找到人设文件 {_PERSONA_PATH}，人设降级为空串。"
            "Agent 将不知道自己的身份，请检查文件名是否为 persona.txt。",
            RuntimeWarning,
            stacklevel=2,
        )
        return ""


PERSONA: str = _load_persona()


def with_persona(task_prompt: str = "") -> str:
    """拼出「人设 + 本节点职责」的系统提示词。

    Args:
        task_prompt: 本节点自己的职责说明。人设在前、职责在后，
                     这样职责约束的优先级更高（离用户消息更近）。
    """
    parts = [p for p in (PERSONA, task_prompt.strip()) if p]
    return "\n\n".join(parts)
