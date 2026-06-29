"""
pipeline/generator.py
----------------------
将 analyzer 输出的操作意图序列转换为可执行的 Playwright TypeScript 脚本。

主要职责：
  1. 把每个 ActionInfo 映射为对应的 Playwright 语句
  2. 按页面 URL / page_context 自动分组为多个 test() 块
  3. 识别登录步骤，提取到 beforeEach
  4. 为关键操作插入 expect() 断言
  5. 输出完整的 .spec.ts 文件

上游：pipeline/analyzer.py → AnalyzeResult / list[ActionInfo]
下游：scripts/*.spec.ts（可直接被 Playwright 执行）

无 API 依赖，纯 Python 字符串模板生成。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.analyzer import ActionInfo, AnalyzeResult


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class ScriptStatement:
    """单条 Playwright 语句"""
    code: str                    # TypeScript 代码行（含缩进）
    comment: str = ""            # 可选的行注释（来自 description）
    assertion: str = ""          # 可选的 expect() 断言行


@dataclass
class DataRef:
    """数据引用 — 每条 fill/select 操作映射到一个 (group, field) 对"""
    group: str
    field: str
    value: str          # 默认值（LLM 识别的用户输入，作为模板填充）


@dataclass
class TestBlock:
    """对应一个 test('...', async ({ page }) => { ... }) 块"""
    name: str
    statements: list[ScriptStatement] = field(default_factory=list)


@dataclass
class GenerateResult:
    """generate_script 的返回值"""
    script: str                  # 完整 TypeScript 脚本内容
    output_path: str             # 写入的文件路径
    total_actions: int = 0
    skipped_actions: int = 0     # unknown / 低置信度被跳过的数量
    test_blocks: int = 0
    elapsed_sec: float = 0.0
    data_json_path: str = ""     # 输出的 .data.json 路径
    data_entries: dict = field(default_factory=dict)   # 数据条目

    def summary(self) -> str:
        return (
            f"生成脚本: {self.output_path}  "
            f"操作 {self.total_actions - self.skipped_actions}/{self.total_actions}  "
            f"test块 {self.test_blocks}  "
            f"耗时 {self.elapsed_sec:.2f}s"
        )


# ─────────────────────────────────────────────
# Playwright 语句映射规则
# ─────────────────────────────────────────────

# action_type → 断言模板（在操作后插入）
_ASSERTIONS: dict[str, str] = {
    "navigate": "await expect(page).not.toHaveURL('about:blank');",
    "click":    "",   # click 后的断言因场景而异，默认不加
    "fill":     "",
    "select":   "",
    "scroll":   "",
    "hover":    "",
    "wait":     "await page.waitForLoadState('networkidle');",
}

# 登录相关关键词（用于识别登录步骤，提取到 beforeEach）
_LOGIN_KEYWORDS = {"登录", "login", "sign in", "signin", "用户名", "密码", "password", "username"}

# 低置信度阈值：低于此值的操作会加警告注释但仍保留
_LOW_CONFIDENCE_THRESHOLD = 0.6

# 极低置信度阈值：低于此值直接跳过
_SKIP_CONFIDENCE_THRESHOLD = 0.3


# ─────────────────────────────────────────────
# 核心生成器
# ─────────────────────────────────────────────

class PlaywrightGenerator:
    """
    将 AnalyzeResult 转换为 Playwright TypeScript 测试脚本。

    用法：
        gen = PlaywrightGenerator(base_url="https://sys.com", suite_name="工单业务流程")
        result = gen.generate(analyze_result, output_path="scripts/test_order.spec.ts")
    """

    def __init__(
        self,
        base_url: str = "",
        suite_name: str = "自动化业务流程",
        timeout: int = 10_000,
        min_confidence: float = _SKIP_CONFIDENCE_THRESHOLD,
    ):
        self.base_url = base_url
        self.suite_name = suite_name
        self.timeout    = timeout          # 元素等待超时（ms）
        self.min_confidence = min_confidence

    # ── 数据注入 ──────────────────────────────

    @staticmethod
    def _group_name(action: ActionInfo) -> str:
        """从 action.page_context 提取数据组名，清理特殊字符"""
        raw = action.page_context.strip() if action.page_context else "默认步骤"
        # 移除不可见字符和常见标点，中文标点保留
        cleaned = re.sub(r'[\r\n\t]', '', raw)
        cleaned = cleaned.strip()
        return cleaned or "默认步骤"

    @staticmethod
    def _field_name(action: ActionInfo) -> str:
        """从 element_hint 提取数据字段名（优先级: label > placeholder > text）"""
        hint = action.element_hint
        return (hint.label or hint.placeholder or hint.text or "字段").strip()

    def _build_data_entries(
        self, actions: list[ActionInfo]
    ) -> tuple[dict[str, dict[str, str]], dict[int, DataRef]]:
        """
        从 actions 提取 fill/select 操作，按 page_context 分组构建 data dict。
        返回 (data_entries, action_id_to_ref).
        """
        data: dict[str, dict[str, str]] = {}
        refs: dict[int, DataRef] = {}
        group_counter: dict[str, int] = {}     # 追踪同名组出现次数

        for action in actions:
            if action.action_type not in ("fill", "select"):
                continue

            group = self._group_name(action)
            field = self._field_name(action)
            value = action.input_value

            # 同组名去重：首次不加后缀，第二次加 _2，第三次 _3 ...
            group_counter[group] = group_counter.get(group, 0) + 1
            unique_group = group if group_counter[group] == 1 else f"{group}_{group_counter[group]}"

            if unique_group not in data:
                data[unique_group] = {}
            else:
                # 同组内字段名去重
                original = field
                n = 1
                while field in data[unique_group]:
                    n += 1
                    field = f"{original}_{n}"

            data[unique_group][field] = value
            refs[id(action)] = DataRef(group=unique_group, field=field, value=value)

        return data, refs

    @staticmethod
    def _render_data_json(data: dict[str, dict[str, str]]) -> str:
        """将 data dict 渲染为格式化的 JSON 字符串"""
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    # ── 公开接口 ──────────────────────────────

    def generate(
        self,
        source: AnalyzeResult | list[ActionInfo],
        output_path: str = "scripts/output.spec.ts",
    ) -> GenerateResult:
        """主入口：接收分析结果，写出 .spec.ts 文件，返回 GenerateResult"""
        t_start = time.perf_counter()
        actions = source.actions if isinstance(source, AnalyzeResult) else source

        result = GenerateResult(
            script="",
            output_path=output_path,
            total_actions=len(actions),
        )

        # 1. 过滤 + 分组
        valid, skipped = self._filter_actions(actions)
        result.skipped_actions = skipped

        login_actions, flow_actions = self._split_login(valid)
        test_blocks   = self._group_into_blocks(flow_actions)
        result.test_blocks = len(test_blocks)

        # 2. 渲染脚本
        script = self._render_script(login_actions, test_blocks)
        result.script = script

        # 3. 写文件
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script, encoding="utf-8")

        result.elapsed_sec = round(time.perf_counter() - t_start, 4)
        return result

    # ── 过滤 ──────────────────────────────────

    def _filter_actions(self, actions: list[ActionInfo]) -> tuple[list[ActionInfo], int]:
        """过滤掉 unknown 类型和极低置信度的操作，返回 (有效列表, 跳过数量)"""
        valid, skipped = [], 0
        for a in actions:
            if a.action_type == "unknown" or a.confidence < self.min_confidence:
                skipped += 1
            else:
                valid.append(a)
        return valid, skipped

    # ── 登录识别 ──────────────────────────────

    def _split_login(
        self, actions: list[ActionInfo]
    ) -> tuple[list[ActionInfo], list[ActionInfo]]:
        """
        从操作序列头部识别登录步骤，分离到 beforeEach。
        策略：前 N 步中凡包含登录关键词的连续操作视为登录流程。
        """
        login, flow = [], []
        in_login_phase = True

        for action in actions:
            if not in_login_phase:
                flow.append(action)
                continue

            is_login = any(
                kw in (action.description + action.element_hint.text +
                       action.element_hint.placeholder + action.element_hint.label).lower()
                for kw in _LOGIN_KEYWORDS
            )

            # 遇到 navigate 后的非登录操作，认为登录阶段结束
            if action.action_type == "navigate" and login:
                in_login_phase = False
                flow.append(action)
            elif is_login or action.action_type == "navigate":
                login.append(action)
            else:
                in_login_phase = False
                flow.append(action)

        return login, flow

    # ── 分组 ──────────────────────────────────

    def _group_into_blocks(self, actions: list[ActionInfo]) -> list[TestBlock]:
        """
        按 page_context（页面/功能模块）将操作分组为多个 test() 块。
        相邻 navigate 操作触发新块的创建。
        """
        if not actions:
            return [TestBlock(name=self.suite_name, statements=[])]

        blocks: list[TestBlock] = []
        current_name = self._infer_block_name(actions[0])
        current_stmts: list[ScriptStatement] = []

        for action in actions:
            # navigate 到新页面时开启新块（但第一个 navigate 不切块）
            if action.action_type == "navigate" and current_stmts:
                blocks.append(TestBlock(name=current_name, statements=current_stmts))
                current_name  = self._infer_block_name(action)
                current_stmts = []

            stmt = self._action_to_statement(action)
            current_stmts.append(stmt)

        if current_stmts:
            blocks.append(TestBlock(name=current_name, statements=current_stmts))

        # 如果只有一个块，用 suite_name 命名
        if len(blocks) == 1:
            blocks[0].name = self.suite_name

        return blocks

    def _infer_block_name(self, action: ActionInfo) -> str:
        """从操作的 page_context / description 推断 test 块名称"""
        if action.page_context:
            return action.page_context
        if action.description:
            # 截取前 20 个字作为名称
            return action.description[:20]
        return self.suite_name

    # ── 语句映射 ──────────────────────────────

    def _action_to_statement(self, action: ActionInfo) -> ScriptStatement:
        """将单个 ActionInfo 映射为 ScriptStatement"""
        code = self._render_action_code(action)
        assertion = self._render_assertion(action)

        # 低置信度加警告注释
        comment = action.description
        if action.confidence < _LOW_CONFIDENCE_THRESHOLD:
            comment = f"[低置信度 {action.confidence:.2f}，请人工确认] {comment}"

        return ScriptStatement(code=code, comment=comment, assertion=assertion)

    def _render_action_code(self, action: ActionInfo) -> str:
        """根据 action_type 和 element_hint 生成具体的 Playwright 调用语句"""
        hint = action.element_hint
        locator = self._build_locator(hint, action.action_type)

        match action.action_type:
            case "navigate":
                url = action.url or self.base_url
                return f"await page.goto('{url}');"

            case "click":
                return f"await {locator}.click();"

            case "fill":
                value = _escape_ts_string(action.input_value)
                return f"await {locator}.fill('{value}');"

            case "select":
                value = _escape_ts_string(action.input_value)
                return f"await {locator}.selectOption({{ label: '{value}' }});"

            case "scroll":
                return "await page.keyboard.press('PageDown');"

            case "hover":
                return f"await {locator}.hover();"

            case "wait":
                return f"await page.waitForLoadState('networkidle', {{ timeout: {self.timeout} }});"

            case "screenshot":
                name = re.sub(r"\W+", "_", action.description or "step")
                return f"await page.screenshot({{ path: 'screenshots/{name}.png' }});"

            case _:
                return f"// TODO: 未识别操作 — {action.description}"

    def _build_locator(self, hint, action_type: str) -> str:
        """委托给模块级函数，方便单元测试直接调用"""
        return _build_locator_standalone(hint, action_type)
    def _render_assertion(self, action: ActionInfo) -> str:
        """为操作生成后置 expect 断言（部分场景有，部分没有）"""
        match action.action_type:
            case "navigate":
                if action.url:
                    return f"await expect(page).toHaveURL(/{re.escape(_url_pattern(action.url))}/i);"
                return "await page.waitForLoadState('networkidle');"
            case "wait":
                return "await expect(page.locator('.loading')).toBeHidden();"
            case _:
                return ""

    # ── 渲染模板 ──────────────────────────────

    def _render_script(
        self,
        login_actions: list[ActionInfo],
        test_blocks: list[TestBlock],
    ) -> str:
        """拼装完整的 TypeScript 脚本"""
        parts: list[str] = []

        # 文件头注释
        parts.append(_FILE_HEADER.format(
            suite_name=self.suite_name,
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            base_url=self.base_url or "(未检测到，请手动设置)",
        ))

        # import
        parts.append(_IMPORTS)

        # test.describe 开始
        parts.append(f'\ntest.describe("{self.suite_name}", () => {{')

        # beforeEach（登录）
        if login_actions:
            parts.append(self._render_before_each(login_actions))

        # test 块
        for block in test_blocks:
            parts.append(self._render_test_block(block))

        # test.describe 结束
        parts.append("});\n")

        return "\n".join(parts)

    def _render_before_each(self, login_actions: list[ActionInfo]) -> str:
        lines = ["", "  test.beforeEach(async ({ page }) => {", "    // 登录"]
        for action in login_actions:
            stmt = self._action_to_statement(action)
            if stmt.comment:
                lines.append(f"    // {stmt.comment}")
            lines.append(f"    {stmt.code}")
            if stmt.assertion:
                lines.append(f"    {stmt.assertion}")
        lines.append("  });")
        return "\n".join(lines)

    def _render_test_block(self, block: TestBlock) -> str:
        lines = [
            "",
            f'  test("{block.name}", async ({{ page }}) => {{',
        ]
        for stmt in block.statements:
            if stmt.comment:
                lines.append(f"    // {stmt.comment}")
            lines.append(f"    {stmt.code}")
            if stmt.assertion:
                lines.append(f"    {stmt.assertion}")
        lines.append("  });")
        return "\n".join(lines)


# ─────────────────────────────────────────────
# 脚本模板常量
# ─────────────────────────────────────────────

_FILE_HEADER = """\
/**
 * {suite_name}
 * 由 RPAsystem pipeline/generator.py 自动生成
 * 生成时间: {generated_at}
 * 目标系统: {base_url}
 *
 * ⚠️  自动生成的脚本需要人工审核后才能用于生产
 *    重点检查：
 *    1. 标有 TODO 的选择器 — 需要手动补充
 *    2. 标有「低置信度」的操作 — AI 不确定，请验证
 *    3. 输入值（密码等敏感数据）— 替换为环境变量
 */"""

_IMPORTS = """\
import { test, expect } from '@playwright/test';"""


# ─────────────────────────────────────────────
# 便捷顶层函数
# ─────────────────────────────────────────────

def generate_script(
    source: AnalyzeResult | list[ActionInfo],
    output_path: str = "scripts/output.spec.ts",
    *,
    base_url: str = "",
    suite_name: str = "自动化业务流程",
    timeout: int = 10_000,
) -> GenerateResult:
    """
    顶层便捷函数，直接接收 analyzer 的输出，生成 Playwright 脚本。

    示例：
        from pipeline.analyzer import analyze_frames
        from pipeline.generator import generate_script

        analyze_result = analyze_frames(frames)
        result = generate_script(
            analyze_result,
            output_path="scripts/test_order.spec.ts",
            base_url="https://sys.example.com",
            suite_name="工单创建流程",
        )
        print(result.summary())
    """
    gen = PlaywrightGenerator(
        base_url=base_url,
        suite_name=suite_name,
        timeout=timeout,
    )
    return gen.generate(source, output_path)


# ─────────────────────────────────────────────
# 私有工具函数
# ─────────────────────────────────────────────

def _escape_ts_string(s: str) -> str:
    """转义 TypeScript 字符串中的特殊字符"""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


def _to_aria_role(role: str) -> str:
    """将 LLM 返回的 role 描述标准化为 ARIA role 字符串"""
    role = role.lower().strip()
    _mapping = {
        "btn": "button",
        "input": "textbox",
        "text": "textbox",
        "textfield": "textbox",
        "dropdown": "combobox",
        "select": "combobox",
        "nav": "navigation",
        "menu": "menuitem",
        "link": "link",
        "checkbox": "checkbox",
        "radio": "radio",
        "tab": "tab",
        "dialog": "dialog",
        "alert": "alert",
        "table": "table",
        "row": "row",
        "cell": "cell",
    }
    return _mapping.get(role, role) if role else "button"


def _url_pattern(url: str) -> str:
    """从完整 URL 提取路径部分，用于断言中的 URL 匹配模式"""
    # 去掉协议和域名，只保留路径，避免断言过于精确
    match = re.search(r"https?://[^/]+(/.*)", url)
    return match.group(1) if match else url


def _build_locator_standalone(hint, action_type: str) -> str:
    """
    按优先级选择最语义化的 Playwright locator（模块级，方便单元测试直接调用）：
      1. getByRole + name（最稳定，抗 UI 变更）
      2. 仅有 role（无 name）
      3. getByLabel（表单字段）
      4. getByPlaceholder（输入框）
      5. getByText（兜底可见文字）
      6. TODO 占位（完全无信息）
    """
    role        = hint.role.strip()
    text        = hint.text.strip()
    label       = hint.label.strip()
    placeholder = hint.placeholder.strip()

    if role and text:
        return f"page.getByRole('{_to_aria_role(role)}', {{ name: '{_escape_ts_string(text)}' }})"
    if role:
        return f"page.getByRole('{_to_aria_role(role)}')"
    if label:
        return f"page.getByLabel('{_escape_ts_string(label)}')"
    if placeholder:
        return f"page.getByPlaceholder('{_escape_ts_string(placeholder)}')"
    if text:
        return f"page.getByText('{_escape_ts_string(text)}', {{ exact: true }})"
    return "page.locator('TODO /* 无法确定选择器，请手动补充 */')"