# engine/reporter.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


@dataclass
class StepResult:
    name: str
    start_time: float = 0.0
    end_time: float = 0.0
    success: bool = False
    error: str = ""
    screenshot: str = ""

    @property
    def duration(self) -> float:
        return round(self.end_time - self.start_time, 2)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "success": self.success,
            "duration_s": self.duration,
            "error": self.error,
            "screenshot": self.screenshot,
        }


@dataclass
class FlowResult:
    flow_name: str
    start_time: float = 0.0
    end_time: float = 0.0
    success: bool = False
    aborted: bool = False
    steps: list[StepResult] = field(default_factory=list)
    report_path: str = ""

    @property
    def duration(self) -> float:
        return round(self.end_time - self.start_time, 2)

    def to_dict(self) -> dict:
        return {
            "flow_name": self.flow_name,
            "success": self.success,
            "aborted": self.aborted,
            "duration_s": self.duration,
            "steps": [s.to_dict() for s in self.steps],
        }


class Reporter:
    """生成 JSON 数据文件 + HTML 可视化报告"""

    def __init__(self, output_dir: str = "./reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, result: FlowResult) -> str:
        """生成报告，返回 HTML 报告路径"""
        ts = time.strftime("%Y%m%d_%H%M%S")
        slug = result.flow_name.replace(" ", "_").replace("/", "-")
        base = self.output_dir / f"{slug}_{ts}"

        # JSON 数据
        json_path = base.with_suffix(".json")
        json_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # HTML 报告
        html_path = base.with_suffix(".html")
        html_path.write_text(self._render_html(result), encoding="utf-8")

        logger.info(f"报告已生成: {html_path}")
        return str(html_path)

    def _render_html(self, result: FlowResult) -> str:
        status_color = "#1D9E75" if result.success else "#D85A30"
        status_text = "成功" if result.success else ("已中断" if result.aborted else "失败")

        rows = ""
        for i, step in enumerate(result.steps, 1):
            icon = "✓" if step.success else "✗"
            color = "#1D9E75" if step.success else "#D85A30"
            screenshot_html = (
                f'<a href="{step.screenshot}" target="_blank">查看截图</a>'
                if step.screenshot else "—"
            )
            rows += f"""
            <tr>
                <td>{i}</td>
                <td>{step.name}</td>
                <td style="color:{color};font-weight:500">{icon} {'通过' if step.success else '失败'}</td>
                <td>{step.duration}s</td>
                <td style="color:#D85A30;font-size:12px">{step.error or '—'}</td>
                <td>{screenshot_html}</td>
            </tr>"""

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>执行报告 — {result.flow_name}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 40px auto; padding: 0 20px; color: #333; }}
  h1 {{ font-size: 22px; font-weight: 500; }}
  .badge {{ display: inline-block; padding: 4px 12px; border-radius: 6px;
            background: {status_color}22; color: {status_color}; font-weight: 500; }}
  .meta {{ color: #888; font-size: 14px; margin: 12px 0 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 10px 12px; background: #f5f5f3; border-bottom: 1px solid #e0e0dc; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f0f0ec; vertical-align: top; }}
  tr:hover td {{ background: #fafaf8; }}
</style>
</head>
<body>
  <h1>{result.flow_name}</h1>
  <span class="badge">{status_text}</span>
  <div class="meta">
    耗时 {result.duration}s &nbsp;|&nbsp;
    {sum(1 for s in result.steps if s.success)}/{len(result.steps)} 步骤通过
  </div>
  <table>
    <thead>
      <tr><th>#</th><th>步骤</th><th>状态</th><th>耗时</th><th>错误信息</th><th>截图</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""