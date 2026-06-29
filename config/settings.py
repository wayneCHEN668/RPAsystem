"""
config/settings.py
-------------------
从环境变量 / .env 文件读取运行时配置。
不依赖 pydantic-settings，用标准库 os + dotenv 实现，避免额外安装负担。
"""
from __future__ import annotations

import os
from pathlib import Path

# 自动加载项目根目录的 .env（如果存在）
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass   # dotenv 未安装时静默跳过


class Settings:
    # ── AI ──────────────────────────────────────
    @property
    def api_key(self) -> str:
        """DashScope API Key（从 DASHSCOPE_API_KEY 环境变量读取）"""
        return os.environ.get("DASHSCOPE_API_KEY", "")

    @property
    def anthropic_api_key(self) -> str:
        """已废弃 — 请改用 api_key"""
        return self.api_key

    @property
    def dashscope_base_url(self) -> str:
        """DashScope 自定义端点（从 DASHSCOPE_BASE_URL 环境变量读取）"""
        return os.environ.get("DASHSCOPE_BASE_URL", "")

    # ── 目标系统 ─────────────────────────────────
    @property
    def base_url(self) -> str:
        return os.environ.get("DASHSCOPE_BASE_URL", "")
    
    @property
    def llm_model(self) -> str:
        return os.environ.get("DASHSCOPE_MODEL", "")

    @property
    def sys_username(self) -> str:
        return os.environ.get("SYS_USERNAME", "")

    @property
    def sys_password(self) -> str:
        return os.environ.get("SYS_PASSWORD", "")

    # ── 执行配置 ─────────────────────────────────
    @property
    def headless(self) -> bool:
        return os.environ.get("HEADLESS", "false").lower() == "true"

    @property
    def slow_mo(self) -> int:
        return int(os.environ.get("SLOW_MO", "50"))

    @property
    def screenshot_on_fail(self) -> bool:
        return os.environ.get("SCREENSHOT_ON_FAIL", "true").lower() == "true"

    @property
    def output_dir(self) -> Path:
        return Path(os.environ.get("OUTPUT_DIR", "./scripts"))

    @property
    def frames_dir(self) -> Path:
        return Path(os.environ.get("FRAMES_DIR", "./frames"))

    # ── pipeline 调参 ────────────────────────────
    @property
    def similarity_threshold(self) -> float:
        return float(os.environ.get("SIMILARITY_THRESHOLD", "0.92"))

    @property
    def sample_interval_sec(self) -> float:
        return float(os.environ.get("SAMPLE_INTERVAL_SEC", "0.2"))

    @property
    def analyzer_model(self) -> str:
        return os.environ.get("ANALYZER_MODEL", "qwen-vl-max-latest")

    @property
    def analyzer_concurrency(self) -> int:
        return int(os.environ.get("ANALYZER_CONCURRENCY", "3"))


settings = Settings()