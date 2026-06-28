"""
pipeline/analyzer.py
---------------------
将关键帧序列发送给多模态 LLM，识别每帧的 UI 操作意图。

主要职责：
  1. 将帧图片编码为 base64 发给 Claude Vision
  2. 结合上下文（前一帧描述）提升识别连贯性
  3. 解析结构化 JSON 操作意图
  4. 支持批量并发（rate limit 友好的信号量控制）
  5. 对解析失败的帧提供降级处理，不中断整体流程

上游：pipeline/preprocessor.py → PreprocessResult / list[FrameInfo]
下游：pipeline/generator.py   ← list[ActionInfo]
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pipeline.preprocessor import FrameInfo, PreprocessResult


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

ActionType = Literal[
    "navigate",   # 页面跳转
    "click",      # 点击按钮 / 链接 / 菜单
    "fill",       # 输入框填写
    "select",     # 下拉框 / 单选 / 多选
    "scroll",     # 滚动页面
    "hover",      # 悬停
    "wait",       # 等待加载
    "screenshot", # 截图确认
    "unknown",    # 无法识别
]


@dataclass
class ElementHint:
    """被操作元素的定位线索，用于 generator 生成 selector"""
    text: str = ""          # 元素可见文字（按钮文字、链接文字等）
    placeholder: str = ""   # input 的 placeholder
    label: str = ""         # 关联的 <label> 文字
    role: str = ""          # ARIA role（button / textbox / combobox…）
    location: str = ""      # 大致位置描述（top-left / center / bottom-right）


@dataclass
class ActionInfo:
    """单帧识别出的操作意图（analyzer 的核心输出单元）"""
    # 来源帧信息
    frame_idx: int
    timestamp: float
    frame_path: str

    # 操作语义
    action_type: ActionType = "unknown"
    element_hint: ElementHint = field(default_factory=ElementHint)
    input_value: str = ""        # fill / select 时填写 / 选择的值
    url: str = ""                # 当前页面 URL（如可见）
    page_context: str = ""       # 页面标题 / 面包屑 / 模态框标题
    description: str = ""        # 自然语言描述，供 generator 写注释用

    # 质量标记
    confidence: float = 1.0      # 0~1，LLM 自评置信度
    parse_error: str = ""        # 非空表示 JSON 解析失败，降级处理

    def is_valid(self) -> bool:
        return self.action_type != "unknown" and not self.parse_error

    def to_dict(self) -> dict:
        return {
            "frame_idx": self.frame_idx,
            "timestamp": self.timestamp,
            "frame_path": self.frame_path,
            "action_type": self.action_type,
            "element_hint": {
                "text": self.element_hint.text,
                "placeholder": self.element_hint.placeholder,
                "label": self.element_hint.label,
                "role": self.element_hint.role,
                "location": self.element_hint.location,
            },
            "input_value": self.input_value,
            "url": self.url,
            "page_context": self.page_context,
            "description": self.description,
            "confidence": self.confidence,
            "parse_error": self.parse_error,
        }


@dataclass
class AnalyzeResult:
    """analyze_frames 的完整返回值"""
    actions: list[ActionInfo] = field(default_factory=list)
    total_frames: int = 0
    success_count: int = 0
    failed_count: int = 0
    elapsed_sec: float = 0.0

    def summary(self) -> str:
        rate = self.success_count / self.total_frames * 100 if self.total_frames else 0
        return (
            f"分析 {self.total_frames} 帧  "
            f"成功 {self.success_count} / 失败 {self.failed_count}  "
            f"成功率 {rate:.0f}%  耗时 {self.elapsed_sec:.1f}s"
        )


# ─────────────────────────────────────────────
# Prompt 模板
# ─────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是专业的 UI 自动化分析专家，擅长从操作录屏截图中识别用户的操作意图。
你的输出将被直接用于生成 Playwright 自动化测试脚本，请确保输出准确、结构化。

输出规则：
- 只输出合法 JSON，不要有任何额外文字、注释或 Markdown 代码块
- 所有字段必须存在，未知时填空字符串
- action_type 只能是以下之一：navigate / click / fill / select / scroll / hover / wait / unknown
- confidence 是你对本次识别准确性的自评分（0.0~1.0）
"""

_USER_PROMPT_TEMPLATE = """\
请分析这张操作录屏截图，识别用户正在执行的 UI 操作。

{context_section}

请以如下 JSON 格式输出（不要有任何额外文字）：
{{
  "action_type": "click|fill|select|navigate|scroll|hover|wait|unknown",
  "element_hint": {{
    "text": "元素可见文字，如按钮文字、链接文字",
    "placeholder": "输入框的 placeholder 文字（如有）",
    "label": "表单 label 文字（如有）",
    "role": "元素的 ARIA 角色，如 button/textbox/combobox/menuitem",
    "location": "元素在页面的大致位置，如 top-left/center/bottom-right"
  }},
  "input_value": "如果是 fill/select 操作，填写或选择的具体内容，否则为空字符串",
  "url": "地址栏显示的当前页面 URL，无法看到时为空字符串",
  "page_context": "页面标题、面包屑导航、或当前打开的模态框/弹窗标题",
  "description": "用一句中文描述该操作，例如：点击导航栏的「工单管理」菜单项",
  "confidence": 0.95
}}
"""

_CONTEXT_SECTION_TEMPLATE = """\
上一步操作（供参考，帮助你理解当前操作的上下文）：
{prev_description}
"""


# ─────────────────────────────────────────────
# 核心分析器
# ─────────────────────────────────────────────

class FrameAnalyzer:
    """
    调用 Claude Vision 分析帧序列，输出结构化操作意图列表。

    用法（同步）：
        analyzer = FrameAnalyzer()
        result = analyzer.analyze(preprocess_result)

    用法（异步，适合大批量）：
        result = await analyzer.analyze_async(frames, concurrency=3)
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        max_concurrency: int = 3,
        max_retries: int = 3,
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._async_client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries

    # ── 公开接口 ──────────────────────────────

    def analyze(self, source: PreprocessResult | list[FrameInfo]) -> AnalyzeResult:
        """同步入口，内部用 asyncio.run 驱动异步实现"""
        frames = source.frames if isinstance(source, PreprocessResult) else source
        return asyncio.run(self._run(frames))

    async def analyze_async(
        self, source: PreprocessResult | list[FrameInfo]
    ) -> AnalyzeResult:
        """异步入口，适合在已有事件循环的环境中调用"""
        frames = source.frames if isinstance(source, PreprocessResult) else source
        return await self._run(frames)

    # ── 内部实现 ──────────────────────────────

    async def _run(self, frames: list[FrameInfo]) -> AnalyzeResult:
        """
        执行策略：
        - prev_description 存在顺序依赖（第 N 帧需要第 N-1 帧的结果），
          因此以「批次」为单位执行：
            批次 1：帧 0（必须先跑，拿到 description 后才能并发下一批）
            批次 2：帧 1~concurrency（并发，共享同一个 prev_desc）
            批次 3：帧 concurrency+1~...
          这样在保证上下文连贯的前提下尽量并发，比纯串行快。
        - 若 max_concurrency=1，退化为完全串行。
        """
        t_start = time.perf_counter()
        result = AnalyzeResult(total_frames=len(frames))

        if not frames:
            logger.warning("analyzer: 收到空帧列表，跳过分析")
            return result

        semaphore = asyncio.Semaphore(self.max_concurrency)
        completed: list[ActionInfo] = []

        for i, frame in enumerate(frames):
            # 取上一帧的 description 作为当前帧上下文
            prev_desc = completed[-1].description if completed else ""

            async def _one(f=frame, pd=prev_desc):
                async with semaphore:
                    return await self._analyze_single(f, pd)

            # 严格顺序：每帧 await 完成后再处理下一帧
            # 保证 prev_desc 始终来自真实完成的上一帧
            action = await _one()
            completed.append(action)

            if action.parse_error:
                result.failed_count += 1
            else:
                result.success_count += 1

        result.actions = completed
        result.elapsed_sec = round(time.perf_counter() - t_start, 2)
        logger.info(f"analyzer: {result.summary()}")
        return result

    async def _analyze_single(self, frame: FrameInfo, prev_description: str) -> ActionInfo:
        """分析单帧，返回 ActionInfo（失败时返回降级结果，不抛异常）"""
        action = ActionInfo(
            frame_idx=frame.frame_idx,
            timestamp=frame.timestamp,
            frame_path=frame.path,
        )

        try:
            image_b64 = _encode_image(frame.path)
            raw_json = await self._call_llm(image_b64, prev_description)
            _fill_action_from_json(action, raw_json)
            logger.debug(
                f"  帧 {frame.frame_idx} ({frame.timestamp}s): "
                f"{action.action_type} — {action.description[:40]}"
            )
        except anthropic.RateLimitError:
            logger.warning(f"  帧 {frame.frame_idx}: Rate limit，等待后重试")
            await asyncio.sleep(5)
            return await self._analyze_single(frame, prev_description)
        except Exception as e:
            action.parse_error = str(e)
            action.action_type = "unknown"
            logger.error(f"  帧 {frame.frame_idx} 分析失败: {e}")

        return action

    @retry(
        retry=retry_if_exception_type((anthropic.APIConnectionError, anthropic.InternalServerError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _call_llm(self, image_b64: str, prev_description: str) -> str:
        """调用 Claude Vision API，返回原始 JSON 字符串"""
        context_section = (
            _CONTEXT_SECTION_TEMPLATE.format(prev_description=prev_description)
            if prev_description
            else ""
        )
        user_prompt = _USER_PROMPT_TEMPLATE.format(context_section=context_section)

        response = await self._async_client.messages.create(
            model=self.model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_prompt},
                    ],
                }
            ],
        )
        return response.content[0].text


# ─────────────────────────────────────────────
# 便捷顶层函数（与 preprocessor 风格对齐）
# ─────────────────────────────────────────────

def analyze_frames(
    source: PreprocessResult | list[FrameInfo],
    *,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_concurrency: int = 3,
) -> AnalyzeResult:
    """
    顶层便捷函数，直接接收 preprocessor 的输出，返回操作意图列表。

    示例：
        from pipeline.preprocessor import extract_key_frames
        from pipeline.analyzer import analyze_frames

        frames = extract_key_frames("recording.mp4", "./frames")
        result = analyze_frames(frames)
        for action in result.actions:
            print(action.timestamp, action.action_type, action.description)
    """
    analyzer = FrameAnalyzer(
        api_key=api_key,
        model=model,
        max_concurrency=max_concurrency,
    )
    return analyzer.analyze(source)


# ─────────────────────────────────────────────
# 私有工具函数
# ─────────────────────────────────────────────

def _encode_image(path: str) -> str:
    """将图片文件读取并编码为 base64 字符串"""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def _fill_action_from_json(action: ActionInfo, raw: str) -> None:
    """
    将 LLM 返回的 JSON 字符串解析并填入 ActionInfo。
    对格式问题做容错处理（去除 markdown 代码块等）。
    """
    # 清理 LLM 有时返回的 markdown 代码块标记
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    data: dict = json.loads(cleaned)   # 解析失败会抛 JSONDecodeError，由调用方捕获

    action.action_type = data.get("action_type", "unknown")
    action.input_value = data.get("input_value", "")
    action.url = data.get("url", "")
    action.page_context = data.get("page_context", "")
    action.description = data.get("description", "")
    action.confidence = float(data.get("confidence", 1.0))

    hint_data = data.get("element_hint", {})
    action.element_hint = ElementHint(
        text=hint_data.get("text", ""),
        placeholder=hint_data.get("placeholder", ""),
        label=hint_data.get("label", ""),
        role=hint_data.get("role", ""),
        location=hint_data.get("location", ""),
    )