"""Catalogue requests must not swallow questions about video contents.

These tests exercise the production decision functions without issuing model,
embedding, database or HTTP requests.
"""
import pytest

from app.services.rag_service import RagService, _is_list_query


@pytest.mark.parametrize("question", [
    "有哪些视频",
    "收藏夹里都有什么视频",
    "关于Git的视频有哪些",
    "列出所有已入库视频",
    "把视频标题列出来",
    "给我知识库的视频清单",
    "列出所有的",
    "给我一个清单",
    "有哪些讲解安装步骤的视频",
    "视频目录",
    "知识库里都有哪些？",
    "有哪些Git视频",
    "列出标题",
])
def test_explicit_video_catalogue_requests_keep_list_route(question):
    assert _is_list_query(question) is True
    assert RagService()._route(question, has_data=True) == "db_list"


@pytest.mark.parametrize("question", [
    "大模型幻觉有哪些原因",
    "视频里说了哪些面试技巧",
    "Git初始化会出现什么目录",
    "列出视频里的安装步骤",
    "总结这些视频的主要观点",
    "列出包含哪些字段的JSON示例",
    "有哪些视频讲过的操作步骤",
    "这种协作方案里有哪些角色",
    "这个工具运行后有哪些输出",
    "安装完成后应该检查哪些目录和文件",
    "请列出配置文件的必填字段",
    "视频中提到的清单有什么用途",
    "教程里要求填写的参数列表是什么",
    "有哪些方法可以减少模型幻觉",
    "把视频里提到的命令列出来",
    "有哪些Agent视频，并说明各自实现步骤",
    "列出相关视频并解释部署时的注意事项",
    "给我知识库的视频清单，并总结各自的主要观点",
    "列出教程中各章节的标题",
    "有哪些视频，并解释它们的主要区别",
    "视频里有哪些步骤？请列出每步使用的命令",
    "有哪些工具可以剪辑视频",
    "有哪些步骤可以上传视频",
    "列出视频并解释如何制作视频",
])
def test_content_and_mixed_requests_retrieve_original_passages(question):
    # A request can contain '哪些', '目录', '列表', '清单' or '列出' while
    # still needing the transcript rather than just saved video titles.
    assert _is_list_query(question) is False
    assert RagService()._route(question, has_data=True) == "vector"


@pytest.mark.parametrize("question", [
    "总结一下",
    "帮我概括",
    "回顾一下",
])
def test_unqualified_whole_library_summaries_keep_content_route(question):
    assert _is_list_query(question) is False
    assert RagService()._route(question, has_data=True) == "db_content"
