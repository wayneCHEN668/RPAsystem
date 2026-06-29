"""
main.py
--------
RPAsystem CLI 入口。

命令：
  generate   视频 → Playwright 脚本（完整 pipeline）
  validate   验证已有脚本
  inspect    仅做视频预处理，输出关键帧列表（调试用）

用法示例：
  python main.py generate recording.mp4
  python main.py generate recording.mp4 --output scripts/test_order.spec.ts --suite-name "工单流程"
  python main.py validate scripts/test_order.spec.ts
  python main.py inspect recording.mp4 --threshold 0.90
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich import box

app    = typer.Typer(
    name="rpasystem",
    help="将操作录屏视频转换为 Playwright 自动化测试脚本",
    add_completion=False,
)
console = Console()


# ─────────────────────────────────────────────
# generate 命令
# ─────────────────────────────────────────────

@app.command()
def generate(
    video: Path = typer.Argument(..., help="操作录屏视频路径（mp4 / avi / mov）"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="输出脚本路径，默认 scripts/<视频名>.spec.ts",
    ),
    suite_name: str = typer.Option(
        "", "--suite-name", "-s",
        help="test.describe 的名称，默认使用视频文件名",
    ),
    base_url: str = typer.Option(
        "", "--base-url", "-u",
        help="目标系统 URL，覆盖 .env BASE_URL",
    ),
    threshold: float = typer.Option(
        0.0, "--threshold", "-t",
        help="帧差异阈值 0~1，越高提取越多帧（默认读 .env SIMILARITY_THRESHOLD=0.92）",
    ),
    no_validate: bool = typer.Option(
        False, "--no-validate",
        help="跳过生成后的脚本验证",
    ),
    keep_frames: bool = typer.Option(
        False, "--keep-frames",
        help="保留临时关键帧文件（默认处理完自动删除）",
    ),
    save_actions: bool = typer.Option(
        False, "--save-actions",
        help="将 AI 识别的操作序列保存为 actions.json（调试用）",
    ),
    api_key: str = typer.Option(
        "", "--api-key",
        help="DashScope API Key，覆盖环境变量 DASHSCOPE_API_KEY",
    ),
):
    """
    【主命令】从操作录屏视频生成 Playwright 测试脚本。

    完整流程：视频抽帧 → AI 分析 → 生成脚本 → 验证报告
    """
    from config.settings import settings

    # ── 参数解析 ──────────────────────────────
    if not video.exists():
        console.print(f"[red]✗ 视频文件不存在:[/] {video}")
        raise typer.Exit(1)

    effective_output = output or (
        Path(settings.output_dir) / f"{video.stem}.spec.ts"
    )
    effective_suite = suite_name or video.stem.replace("_", " ").replace("-", " ").title()
    effective_url   = base_url or settings.base_url
    effective_thr   = threshold if threshold > 0 else settings.similarity_threshold
    effective_key   = api_key or settings.api_key
    frames_dir      = str(settings.frames_dir)

    if not effective_key:
        console.print("[red]✗ 未设置 DASHSCOPE_API_KEY[/]，请在 .env 中配置或用 --api-key 传入")
        raise typer.Exit(1)

    # ── 打印运行参数 ──────────────────────────
    console.print(Panel.fit(
        f"[bold]RPAsystem — 视频转脚本[/]\n"
        f"视频: [cyan]{video}[/]\n"
        f"输出: [cyan]{effective_output}[/]\n"
        f"流程: [cyan]{effective_suite}[/]\n"
        f"阈值: [cyan]{effective_thr}[/]",
        border_style="blue",
    ))

    t_total = time.perf_counter()

    # ── Step 1: 视频预处理 ────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("抽取关键帧...", total=None)

        from pipeline.preprocessor import extract_key_frames
        try:
            pre = extract_key_frames(
                str(video),
                frames_dir,
                similarity_threshold=effective_thr,
                clean_output_dir=True,
            )
        except Exception as e:
            console.print(f"[red]✗ 视频预处理失败:[/] {e}")
            raise typer.Exit(1)

        progress.update(task, completed=1, total=1)

    console.print(
        f"[green]✓[/] 抽帧完成  "
        f"[dim]{pre.total_frames} 帧 → {pre.count} 个关键帧  "
        f"{pre.duration_sec:.1f}s 视频  耗时 {pre.elapsed_sec:.1f}s[/]"
    )

    if pre.count == 0:
        console.print("[red]✗ 未提取到任何关键帧，视频可能为空或格式不支持[/]")
        raise typer.Exit(1)

    # ── Step 2: AI 分析 ───────────────────────
    console.print(f"\n[bold]分析操作意图[/] [dim]({pre.count} 帧，模型: {settings.analyzer_model})[/]")

    from pipeline.analyzer import FrameAnalyzer
    analyzer = FrameAnalyzer(
        api_key=effective_key,
        model=settings.analyzer_model,
        max_concurrency=settings.analyzer_concurrency,
        base_url=settings.dashscope_base_url or None,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("AI 分析帧...", total=pre.count)

        # 逐帧更新进度（通过 patch analyze_single）
        analyzed_count = [0]
        original_analyze = analyzer._analyze_single

        async def _tracked_analyze(frame, prev_desc):
            result = await original_analyze(frame, prev_desc)
            analyzed_count[0] += 1
            progress.update(task, completed=analyzed_count[0])
            return result

        analyzer._analyze_single = _tracked_analyze

        import asyncio
        try:
            ana = asyncio.run(analyzer.analyze_async(pre))
        except Exception as e:
            console.print(f"\n[red]✗ AI 分析失败:[/] {e}")
            _cleanup_frames(frames_dir, keep_frames)
            raise typer.Exit(1)

    console.print(
        f"[green]✓[/] 分析完成  "
        f"[dim]{ana.success_count} 成功 / {ana.failed_count} 失败  "
        f"耗时 {ana.elapsed_sec:.1f}s[/]"
    )

    if ana.success_count == 0:
        console.print("[red]✗ 所有帧分析失败，请检查 DASHSCOPE_API_KEY 和网络[/]")
        _cleanup_frames(frames_dir, keep_frames)
        raise typer.Exit(1)

    # 保存 actions.json（调试用）
    if save_actions:
        actions_path = effective_output.parent / f"{video.stem}_actions.json"
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        actions_path.write_text(
            json.dumps([a.to_dict() for a in ana.actions], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"[dim]  操作序列已保存: {actions_path}[/]")

    # ── Step 3: 生成脚本 ──────────────────────
    console.print("\n[bold]生成 Playwright 脚本...[/]")

    from pipeline.generator import generate_script
    try:
        gen = generate_script(
            ana,
            output_path=str(effective_output),
            base_url=effective_url,
            suite_name=effective_suite,
        )
    except Exception as e:
        console.print(f"[red]✗ 脚本生成失败:[/] {e}")
        _cleanup_frames(frames_dir, keep_frames)
        raise typer.Exit(1)

    console.print(
        f"[green]✓[/] 脚本生成  "
        f"[dim]{gen.total_actions - gen.skipped_actions}/{gen.total_actions} 操作  "
        f"{gen.test_blocks} 个 test 块  耗时 {gen.elapsed_sec:.3f}s[/]"
    )
    if gen.data_json_path:
        console.print(f"[green]✓[/] 数据模板  [dim]{gen.data_json_path}[/]")

    # ── Step 4: 验证 ──────────────────────────
    if not no_validate:
        console.print("\n[bold]验证脚本...[/]")

        from pipeline.validator import validate_script
        val = validate_script(gen)

        _print_validate_result(val)

    # ── 清理 + 总结 ───────────────────────────
    _cleanup_frames(frames_dir, keep_frames)

    elapsed = round(time.perf_counter() - t_total, 1)
    panel_lines = [
        f"[bold green]完成![/]  总耗时 {elapsed}s",
        f"脚本路径: [cyan]{effective_output}[/]",
    ]
    if gen.data_json_path:
        panel_lines.append(f"数据模板: [cyan]{gen.data_json_path}[/]")
    panel_lines.append("")
    panel_lines.append(f"[dim]编辑数据 → 运行: npx playwright test {effective_output}[/]")
    console.print(Panel.fit("\n".join(panel_lines), border_style="green"))


# ─────────────────────────────────────────────
# validate 命令
# ─────────────────────────────────────────────

@app.command()
def validate(
    script: Path = typer.Argument(..., help="要验证的 .spec.ts 脚本路径"),
    no_tsc: bool = typer.Option(False, "--no-tsc", help="跳过 TypeScript 语法检查"),
):
    """
    验证已有的 Playwright 脚本（结构规则 + tsc 语法检查）。
    """
    if not script.exists():
        console.print(f"[red]✗ 脚本文件不存在:[/] {script}")
        raise typer.Exit(1)

    content = script.read_text(encoding="utf-8")
    console.print(f"[bold]验证脚本:[/] [cyan]{script}[/]  ({len(content.splitlines())} 行)")

    from pipeline.validator import validate_script
    with console.status("检查中..."):
        val = validate_script(content, script_path=str(script), enable_tsc=not no_tsc)

    _print_validate_result(val)

    if not val.passed:
        raise typer.Exit(1)


# ─────────────────────────────────────────────
# inspect 命令
# ─────────────────────────────────────────────

@app.command()
def inspect(
    video: Path = typer.Argument(..., help="视频路径"),
    threshold: float = typer.Option(0.92, "--threshold", "-t", help="帧差异阈值"),
    interval: float = typer.Option(0.2,  "--interval",  "-i", help="采样间隔（秒）"),
    save_json: bool = typer.Option(False, "--json",          help="输出 frames.json 到当前目录"),
):
    """
    【调试命令】仅执行视频预处理，展示关键帧列表（不调用 AI，不计费）。

    用于在正式 generate 前确认抽帧效果是否合理。
    """
    if not video.exists():
        console.print(f"[red]✗ 视频文件不存在:[/] {video}")
        raise typer.Exit(1)

    with console.status(f"分析视频: {video}..."):
        from pipeline.preprocessor import extract_key_frames
        try:
            pre = extract_key_frames(
                str(video),
                "./frames_inspect",
                similarity_threshold=threshold,
                sample_interval_sec=interval,
                clean_output_dir=True,
            )
        except Exception as e:
            console.print(f"[red]✗ 预处理失败:[/] {e}")
            raise typer.Exit(1)

    # 视频信息
    console.print(Panel.fit(
        f"[bold]视频信息[/]\n"
        f"时长:    {pre.duration_sec:.1f}s\n"
        f"帧率:    {pre.fps:.1f} fps\n"
        f"分辨率:  {pre.video_width} × {pre.video_height}\n"
        f"总帧数:  {pre.total_frames}",
        border_style="dim",
    ))

    # 关键帧表格
    table = Table(
        "序号", "时间", "帧索引", "SSIM 差异", "文件",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
    )
    for i, f in enumerate(pre.frames, 1):
        ssim_color = "green" if f.ssim_score < 0.7 else "yellow" if f.ssim_score < 0.9 else "dim"
        table.add_row(
            str(i),
            f"{f.timestamp:.2f}s",
            str(f.frame_idx),
            f"[{ssim_color}]{f.ssim_score:.3f}[/]",
            Path(f.path).name,
        )

    console.print(table)
    console.print(
        f"共 [bold]{pre.count}[/] 个关键帧  "
        f"[dim]（阈值={threshold}，间隔={interval}s，耗时 {pre.elapsed_sec:.2f}s）[/]\n"
        f"[dim]提示：帧数过多 → 提高阈值；帧数过少 → 降低阈值[/]"
    )

    if save_json:
        out = Path("frames_inspect.json")
        out.write_text(
            json.dumps([f.to_dict() for f in pre.frames], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"[dim]已保存: {out}[/]")

    # 清理临时帧
    import shutil
    shutil.rmtree("./frames_inspect", ignore_errors=True)


# ─────────────────────────────────────────────
# 私有辅助函数
# ─────────────────────────────────────────────

def _print_validate_result(val) -> None:
    """用 Rich 格式化打印 ValidateResult"""
    from pipeline.validator import ValidateResult

    tsc_tag = " [dim]+tsc[/]" if val.tsc_checked else ""
    if val.passed:
        console.print(f"[green]✓ 验证通过{tsc_tag}[/]  "
                      f"[dim]{len(val.warnings)} warning / {len(val.infos)} info[/]")
    else:
        console.print(f"[red]✗ 验证失败{tsc_tag}[/]  "
                      f"[dim]{len(val.errors)} error / {len(val.warnings)} warning[/]")

    if not val.issues:
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("级别",  width=8)
    table.add_column("代码",  width=24)
    table.add_column("行号",  width=5, justify="right")
    table.add_column("描述")

    level_colors = {"error": "red", "warning": "yellow", "info": "cyan"}
    for issue in val.issues:
        color = level_colors.get(issue.level, "white")
        table.add_row(
            f"[{color}]{issue.level.upper()}[/]",
            issue.code,
            str(issue.line) if issue.line else "—",
            issue.message,
        )

    console.print(table)


def _cleanup_frames(frames_dir: str, keep: bool) -> None:
    """处理完成后清理临时帧目录"""
    if keep:
        console.print(f"[dim]临时帧已保留: {frames_dir}[/]")
        return
    import shutil
    shutil.rmtree(frames_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app()