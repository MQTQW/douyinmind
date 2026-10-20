"""
应用配置模块

基于 Pydantic Settings 的环境变量管理。
所有配置项从 .env 文件和环境变量读取，提供类型验证和默认值。
"""
from pathlib import Path
from typing import Literal
from pydantic import Field

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    应用全局配置

    配置优先级：环境变量 > .env 文件 > 默认值
    """

    # ===== API Key =====
    deepseek_api_key: str = ""
    """DeepSeek API Key，用于 LLM 对话"""

    dashscope_api_key: str = ""
    """DashScope API Key，用于 ASR 语音转写和 Embedding 向量化"""

    qwen_omni_api_key: str = ""
    qwen_omni_model: str = "qwen3.8-omni-flash"
    qwen_omni_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_omni_timeout: int = Field(default=180, ge=10, le=600)
    qwen_omni_max_tokens: int = Field(default=8192, ge=1024, le=32768)
    knowledge_analysis_mode: Literal["audio", "audio_visual"] = "audio"
    video_segment_seconds: int = Field(default=60, ge=5, le=120)
    audio_transcription_concurrency: int = Field(default=3, ge=1, le=4)
    audio_boundary_review_enabled: bool = True
    audio_boundary_review_max_windows: int = Field(default=4, ge=0, le=4)
    audio_boundary_review_radius_seconds: int = Field(default=8, ge=4, le=12)
    audio_boundary_review_timeout_seconds: float = Field(default=20, ge=5, le=30)
    audio_boundary_review_budget_seconds: float = Field(default=60, ge=10, le=120)
    audio_boundary_batch_enabled: bool = True
    audio_boundary_batch_radius_seconds: int = Field(default=5, ge=4, le=8)
    audio_boundary_batch_max_windows: int = Field(default=12, ge=0, le=12)
    video_cache_dir: str = "app/storage/video_cache"
    ingestion_metrics_dir: str = "app/storage/ingestion_runs"
    long_video_test_keep_audio: bool = False

    # ===== LLM 配置 =====
    llm_model: str = "deepseek-chat"
    """LLM 模型名称"""

    llm_base_url: str = "https://api.deepseek.com"
    """LLM API 地址，兼容 OpenAI 协议"""

    llm_thinking_enabled: bool = False
    """DeepSeek 是否启用深度思考；默认关闭以更快开始显示回答"""

    # ===== Embedding 配置 =====
    embedding_model: str = "text-embedding-v4"
    """DashScope Embedding 模型名称"""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    query_embedding_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30)
    """Query-only transport timeout; ingestion keeps its existing batch policy."""

    # ===== ASR 配置 =====
    asr_model: str = "paraformer-v2"
    """DashScope 语音识别模型"""

    # ===== 检索参数 =====
    retrieval_top_k: int = 8
    """最终返回的检索结果数量"""

    retrieval_mmr_fetch_k: int = 32
    """MMR 检索的候选池大小"""

    retrieval_mmr_lambda: float = 0.55
    """MMR 多样性系数（0=最大多样性，1=最大相关性）"""

    retrieval_hybrid_enabled: bool = True
    """Combine semantic and local lexical retrieval without generating evidence."""

    retrieval_candidate_k: int = Field(default=32, ge=8, le=128)
    """Semantic candidate pool before lexical fusion and coverage selection."""

    retrieval_neighbor_chars: int = Field(default=350, ge=0, le=1000)
    """Maximum original characters added to restore neighboring sentence boundaries."""

    rag_context_count: int = 8
    """注入 LLM 的上下文 chunk 数量"""

    rag_prompt_max_context_chars: int = 8000
    """注入 LLM 的上下文最大字符数，超出截断"""

    # ===== 切块参数 =====
    chunk_size: int = 1000
    """文本切块大小（字符数）"""

    chunk_overlap: int = 200
    """相邻文本块重叠字符数"""

    # ===== 对话 =====
    chat_history_window: int = 6
    """注入上下文的历史消息数量（最近 N 条）"""

    chat_max_content_chars: int = 2000
    """单条历史消息最大字符数"""

    # ===== 数据库 =====
    database_url: str = "sqlite:///app/storage/douyinrag.db"
    """SQLite 数据库连接 URL"""

    chroma_persist_dir: str = "app/storage/chroma"
    """ChromaDB 向量库持久化目录"""

    # ===== 音频缓存 =====
    audio_cache_dir: str = "app/storage/audio_cache"
    """音频下载缓存目录"""

    # ===== Playwright / 抖音采集 =====
    playwright_headless: bool = False
    """Playwright 是否无头模式（登录时需要可见浏览器扫码）"""

    playwright_user_data_dir: str = "app/storage/playwright_user_data"
    """Playwright 持久化用户数据目录（保存登录状态）"""

    playwright_browsers_path: str = "app/storage/playwright_browsers"
    """Playwright 浏览器安装路径"""

    playwright_browser_channel: str = "chromium"
    """Playwright 浏览器渠道（chromium / msedge）"""

    douyin_home_url: str = "https://www.douyin.com"
    """抖音首页 URL"""

    douyin_favorites_url: str = "https://www.douyin.com/user/self?from_login=1"
    """抖音收藏夹页面 URL"""

    # ===== 项目路径 =====
    @property
    def project_root(self) -> Path:
        """
        获取项目根目录（backend/ 的上级目录）

        :return: 项目根目录的 Path 对象
        """
        return Path(__file__).resolve().parent.parent

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# 全局单例
settings = Settings()
