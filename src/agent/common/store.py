from typing import Optional

from pydantic import BaseModel, Field


class CollectedInfo(BaseModel):
    """收藏信息"""
    collect_id: str = Field(description="收藏id")
    title: str = Field(description="收藏的博客文章标题")
    author: str = Field(description="博客文章作者名")

    blog_url: Optional[str] = Field(
        default=None,
        description="收藏的博客文章链接"
    )
    word_count: Optional[int] = Field(
        default=None,
        description="收藏的博客文章字数"
    )
    intro: Optional[str] = Field(
        default=None,
        description="收藏的博客文章摘要"
    )
    topic: Optional[str] = Field(
        default=None,
        description="收藏的博客文章所属领域/分类"
    )
    tags: Optional[str] = Field(
        default=None,
        description="收藏的博客文章标签，多个标签用逗号分隔"
    )

class UserPreferences(BaseModel):
    """用户偏好数据"""

    word_count_min: Optional[int] = Field(
        default=None,
        description="用户喜欢的最低字数（阅读时长偏好），单位为字"
    )
    word_count_max: Optional[int] = Field(
        default=None,
        description="用户喜欢的最高字数（阅读时长偏好），单位为字"
    )
    collected_info: Optional[list[CollectedInfo]] = Field(
        default=None,
        description="收藏过的博客文章列表"
    )