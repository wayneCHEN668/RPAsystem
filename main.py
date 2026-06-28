import typer
from pathlib import Path
from rich.console import Console

app = typer.Typer(help="RPAsystem - 视频转 Playwright 自动化脚本")
console = Console()

@app.command()
def generate(
    video: Path = typer.Argument(..., help="操作录屏视频路径"),
    output: Path = typer.Option("./scripts/output.spec.ts", help="输出脚本路径"),
    threshold: float = typer.Option(0.90, help="帧差异阈值，越低提取越多帧"),
):
    """从操作录屏视频生成 Playwright 测试脚本"""
    from pipeline.preprocessor import extract_key_frames
    from pipeline.analyzer import analyze_video_frames
    from pipeline.generator import generate_playwright_script

    console.print(f"[bold blue]处理视频:[/] {video}")

    with console.status("抽取关键帧..."):
        frames = extract_key_frames(str(video), "./frames", threshold)
    console.print(f"[green]✓[/] 提取到 {len(frames)} 个关键帧")

    with console.status("AI 分析操作意图..."):
        actions = analyze_video_frames(frames)
    console.print(f"[green]✓[/] 识别出 {len(actions)} 个操作步骤")

    with console.status("生成 Playwright 脚本..."):
        generate_playwright_script(actions, str(output))
    console.print(f"[green]✓[/] 脚本已生成: {output}")

@app.command()
def run(
    script: Path = typer.Argument(..., help="要执行的 .spec.ts 脚本"),
    data_source: str = typer.Option("none", help="数据源类型: api/db/excel/none"),
):
    """执行自动化脚本"""
    console.print(f"[bold blue]执行脚本:[/] {script}")
    # TODO: 调用 engine.runner

if __name__ == "__main__":
    app()