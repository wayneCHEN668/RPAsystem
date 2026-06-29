"""
pipeline/validator.py
----------------------
验证 generator 输出的 Playwright TypeScript 脚本的质量。

两层验证：
  Layer 1 — 结构规则检查（纯 Python 正则，无需 Node.js）
    - 必须包含 import / test.describe / test() / async ({ page })
    - 不能有空 test 块（只有注释没有 await）
    - TODO 占位符计数与告警
    - 低置信度警告行计数
    - await 调用数量下限

  Layer 2 — tsc 语法检查（调用 Node.js tsc --noEmit）
    - 真正的 TypeScript 语法错误
    - 类型错误（错误的 Playwright API 调用）
    - 需要 ts_check/ 目录下的 node_modules 环境

上游：pipeline/generator.py → GenerateResult / script str / file path
下游：main.py（决定是否交付脚本给用户）

设计原则：
  - 验证失败不阻断流程，返回带 issues 列表的 ValidateResult
  - 每个 issue 有级别：error（必须修复）/ warning（建议检查）/ info（仅提示）
  - Layer 2 是可选的，环境不满足时降级为 Layer 1 only
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.generator import GenerateResult


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

IssueLevel = str   # "error" | "warning" | "info"


@dataclass
class Issue:
    """单条验证问题"""
    level: IssueLevel        # "error" / "warning" / "info"
    code: str                # 机器可读的问题代码，如 "MISSING_IMPORT"
    message: str             # 人类可读描述
    line: int = 0            # 0 表示未知行号

    def __str__(self) -> str:
        loc = f":{self.line}" if self.line else ""
        return f"[{self.level.upper()}] {self.code}{loc}  {self.message}"


@dataclass
class ValidateResult:
    """validate_script 的完整返回值"""
    script_path: str
    issues: list[Issue] = field(default_factory=list)
    tsc_checked: bool = False        # 是否做了 tsc 检查
    elapsed_sec: float = 0.0

    # 统计
    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "info"]

    @property
    def passed(self) -> bool:
        """没有 error 级别问题就算通过"""
        return len(self.errors) == 0

    def summary(self) -> str:
        tsc_tag = " +tsc" if self.tsc_checked else ""
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}]{tsc_tag}  "
            f"{len(self.errors)} error / {len(self.warnings)} warning / {len(self.infos)} info  "
            f"耗时 {self.elapsed_sec:.2f}s"
        )

    def report(self) -> str:
        """完整的可打印报告"""
        lines = [f"验证报告: {self.script_path}", self.summary()]
        if self.issues:
            lines.append("")
            for issue in self.issues:
                lines.append(f"  {issue}")
        return "\n".join(lines)


# ─────────────────────────────────────────────
# 规则定义
# ─────────────────────────────────────────────

# Layer 1：结构规则
# 每条规则：(code, level, description, check_fn)
# check_fn(script: str) -> list[Issue]

def _check_import(script: str) -> list[Issue]:
    """必须有 @playwright/test 的 import"""
    if "from '@playwright/test'" not in script and 'from "@playwright/test"' not in script:
        return [Issue("error", "MISSING_IMPORT",
                      "缺少 import { test, expect } from '@playwright/test'")]
    return []


def _check_describe(script: str) -> list[Issue]:
    """必须有 test.describe 块"""
    if not re.search(r"test\.describe\s*\(", script):
        return [Issue("error", "MISSING_DESCRIBE",
                      "缺少 test.describe() 块，Playwright 要求至少一个 describe")]
    return []


def _check_test_blocks(script: str) -> list[Issue]:
    """必须有至少一个 test() 调用"""
    if not re.search(r"\btest\s*\(", script):
        return [Issue("error", "NO_TEST_BLOCK",
                      "没有任何 test() 块，脚本没有可执行的测试用例")]
    return []


def _check_async_page(script: str) -> list[Issue]:
    """test 块必须有 async ({ page }) 签名"""
    if not re.search(r"async\s*\(\s*\{[^}]*page[^}]*\}\s*\)", script):
        return [Issue("error", "MISSING_ASYNC_PAGE",
                      "test() 回调缺少 async ({ page }) 签名")]
    return []


def _check_await_calls(script: str) -> list[Issue]:
    """有效 test 体内必须有足够的 await 调用"""
    issues = []
    await_count = len(re.findall(r"\bawait\b", script))
    if await_count == 0:
        issues.append(Issue("error", "NO_AWAIT",
                            "脚本中没有任何 await 调用，所有 Playwright 操作都需要 await"))
    elif await_count < 2:
        issues.append(Issue("warning", "FEW_AWAIT",
                            f"只有 {await_count} 个 await 调用，脚本可能不完整"))
    return issues


def _check_empty_test_bodies(script: str) -> list[Issue]:
    """检测空的 test 块（只有注释/空行，没有真正的 await 操作）"""
    issues = []
    # 提取每个 test() 块的体
    pattern = re.compile(
        r'test\s*\(\s*["\']([^"\']+)["\']\s*,\s*async\s*\([^)]*\)\s*=>\s*\{(.*?)\n\s*\}\s*\)',
        re.DOTALL
    )
    for m in pattern.finditer(script):
        test_name = m.group(1)
        body = m.group(2)
        # 去掉注释和空行
        meaningful = [
            ln for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("//")
        ]
        if not meaningful:
            issues.append(Issue("warning", "EMPTY_TEST_BODY",
                                f"test '{test_name}' 块内没有操作语句"))
    return issues


def _check_todo_selectors(script: str) -> list[Issue]:
    """统计 TODO 占位符，每个都是 warning"""
    issues = []
    for i, line in enumerate(script.splitlines(), 1):
        if "TODO" in line and ("locator" in line or "selector" in line.lower()):
            issues.append(Issue("warning", "TODO_SELECTOR",
                                f"第 {i} 行有 TODO 选择器，需要人工补充", line=i))
    return issues


def _check_low_confidence(script: str) -> list[Issue]:
    """统计低置信度警告行"""
    issues = []
    for i, line in enumerate(script.splitlines(), 1):
        if "低置信度" in line or "low confidence" in line.lower():
            issues.append(Issue("info", "LOW_CONFIDENCE",
                                f"第 {i} 行标记了低置信度，建议人工核实", line=i))
    return issues


def _check_hardcoded_credentials(script: str) -> list[Issue]:
    """检测疑似硬编码密码 — 排除已使用 d() 数据引用的调用"""
    issues = []
    # 匹配密码相关 locator 后紧跟 .fill('字面量值')，但不是 .fill(d(...))
    pattern = re.compile(
        r"(?:password|passwd|密码)[^;]*?"
        r"\.fill\(\s*(?!d\()['\"]([^'\"]{1,50})['\"]",
        re.IGNORECASE,
    )
    for m in pattern.finditer(script):
        line_no = script[: m.start()].count("\n") + 1
        issues.append(Issue("warning", "HARDCODED_CREDENTIAL",
                            f"第 {line_no} 行疑似硬编码密码，请改用数据文件或 process.env.PASSWORD",
                            line=line_no))
    return issues


def _check_no_assertions(script: str) -> list[Issue]:
    """脚本中缺少 expect() 断言时给出 info 提示"""
    if not re.search(r"\bexpect\s*\(", script):
        return [Issue("info", "NO_ASSERTIONS",
                      "脚本没有 expect() 断言，建议添加关键操作后的验证")]
    return []


# 所有 Layer 1 规则，按顺序执行
_LAYER1_RULES = [
    _check_import,
    _check_describe,
    _check_test_blocks,
    _check_async_page,
    _check_await_calls,
    _check_empty_test_bodies,
    _check_todo_selectors,
    _check_low_confidence,
    _check_hardcoded_credentials,
    _check_no_assertions,
]


# ─────────────────────────────────────────────
# tsc 语法检查（Layer 2）
# ─────────────────────────────────────────────

# ts_check 目录：有 tsconfig.json 和 node_modules/@playwright/test
_TS_CHECK_DIR = Path(__file__).parent.parent / "ts_check"


def _tsc_available() -> bool:
    """检查 tsc 和 @playwright/test 类型定义是否可用"""
    if not (_TS_CHECK_DIR / "node_modules" / "@playwright").exists():
        return False
    return shutil.which("npx") is not None


def _run_tsc(script_content: str) -> list[Issue]:
    """
    把脚本写到 ts_check/ 临时文件，用 tsc --noEmit 检查语法，返回 Issue 列表。
    tsc 退出码 0 → 无错误；非 0 → 有语法/类型错误。
    """
    issues: list[Issue] = []

    # 写临时文件到 ts_check 目录，tsc 才能找到 node_modules
    tmp_file = _TS_CHECK_DIR / "_validate_tmp_.spec.ts"
    try:
        tmp_file.write_text(script_content, encoding="utf-8")

        result = subprocess.run(
            ["npx", "tsc", "--noEmit", str(tmp_file.name)],
            capture_output=True,
            text=True,
            cwd=str(_TS_CHECK_DIR),
            timeout=30,
        )

        if result.returncode != 0:
            # 解析 tsc 的错误输出，格式：file(line,col): error TSxxxx: message
            for raw_line in result.stdout.splitlines():
                # 只取指向我们临时文件的错误（跳过 node_modules 里的错误）
                if "_validate_tmp_" not in raw_line and "node_modules" in raw_line:
                    continue
                m = re.match(
                    r".*?\((\d+),\d+\):\s*(error|warning)\s+(TS\d+):\s*(.+)",
                    raw_line,
                )
                if m:
                    line_no = int(m.group(1))
                    level   = m.group(2)          # "error" or "warning"
                    ts_code = m.group(3)           # "TS1135"
                    msg     = m.group(4).strip()
                    issues.append(Issue(level, f"TSC_{ts_code}", msg, line=line_no))
                elif raw_line.strip() and "_validate_tmp_" in raw_line:
                    # 无法解析格式的行，作为 error 保留
                    issues.append(Issue("error", "TSC_PARSE_ERROR", raw_line.strip()))

    except subprocess.TimeoutExpired:
        issues.append(Issue("warning", "TSC_TIMEOUT", "tsc 检查超时（30s），跳过语法验证"))
    except Exception as e:
        issues.append(Issue("warning", "TSC_FAILED", f"tsc 检查失败: {e}"))
    finally:
        if tmp_file.exists():
            tmp_file.unlink()

    return issues


# ─────────────────────────────────────────────
# 核心验证器
# ─────────────────────────────────────────────

class ScriptValidator:
    """
    对生成的 Playwright 脚本做两层验证。

    用法：
        validator = ScriptValidator()
        result = validator.validate(generate_result)
        print(result.report())
    """

    def __init__(self, enable_tsc: bool = True):
        self.enable_tsc = enable_tsc

    def validate(
        self,
        source: GenerateResult | str,
        script_path: str = "",
    ) -> ValidateResult:
        """
        主入口：接收 GenerateResult 或脚本字符串，返回 ValidateResult。

        Parameters
        ----------
        source : GenerateResult | str
            GenerateResult（来自 generator）或脚本内容字符串。
        script_path : str
            当 source 是字符串时，提供文件路径用于报告显示。
        """
        t_start = time.perf_counter()

        if isinstance(source, GenerateResult):
            script = source.script
            path   = source.output_path
        else:
            script = source
            path   = script_path

        result = ValidateResult(script_path=path)

        # Layer 1：结构规则
        for rule_fn in _LAYER1_RULES:
            result.issues.extend(rule_fn(script))

        # Layer 2：tsc（可选）
        if self.enable_tsc and _tsc_available():
            tsc_issues = _run_tsc(script)
            result.issues.extend(tsc_issues)
            result.tsc_checked = True

        result.elapsed_sec = round(time.perf_counter() - t_start, 3)
        return result


# ─────────────────────────────────────────────
# 便捷顶层函数
# ─────────────────────────────────────────────

def validate_script(
    source: GenerateResult | str,
    *,
    script_path: str = "",
    enable_tsc: bool = True,
) -> ValidateResult:
    """
    顶层便捷函数，直接接收 generator 的输出，返回验证结果。

    示例：
        from pipeline.generator import generate_script
        from pipeline.validator import validate_script

        gen_result = generate_script(analyze_result, "scripts/test.spec.ts")
        val_result = validate_script(gen_result)

        if val_result.passed:
            print("脚本验证通过，可以使用")
        else:
            print(val_result.report())
    """
    return ScriptValidator(enable_tsc=enable_tsc).validate(source, script_path)