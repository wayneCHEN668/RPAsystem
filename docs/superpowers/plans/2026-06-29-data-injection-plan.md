# Generator 数据注入改造 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 改造 generator 使生成的 .spec.ts 从外部 JSON 数据文件读取输入值，不再硬编码。

**Architecture:** Generator 新增 `_build_data_entries()` 从 actions 提取 fill/select 操作构建 data dict；`_render_action_code()` 改为生成 `d(group, field)` 调用；`generate()` 同时输出 .data.json 模板文件。

**Tech Stack:** Python 3.11+, TypeScript, Playwright, JSON

## Global Constraints

- Python: `from __future__ import annotations`, async/await for IO, type annotations required, loguru for logging
- TypeScript: semantic locators only, no `waitForTimeout`, no hardcoded credentials
- No changes to preprocessor.py or analyzer.py
- No changes to data/ or engine/ modules
- Values in data.json are LLM-detected defaults — user can edit directly

---

### Task 1: DataRef dataclass + _build_data_entries + _render_data_json

**Files:**
- Modify: `pipeline/generator.py`

**Interfaces:**
- Produces: `DataRef(group, field, value)` — dataclass
- Produces: `_build_data_entries(actions: list[ActionInfo]) -> tuple[dict[str, dict[str, str]], dict[int, DataRef]]`
- Produces: `_render_data_json(data: dict[str, dict[str, str]]) -> str`
- Produces: `GenerateResult.data_json_path: str`, `GenerateResult.data_entries: dict`

- [ ] **Step 1: Add DataRef dataclass and extend GenerateResult**

After the existing `ScriptStatement` dataclass (line ~42), add:

```python
@dataclass
class DataRef:
    """数据引用 — 每条 fill/select 操作映射到一个 (group, field) 对"""
    group: str
    field: str
    value: str          # 默认值（LLM 识别的用户输入，作为模板填充）
```

Add two fields to `GenerateResult` (after `elapsed_sec`):

```python
data_json_path: str = ""     # 输出的 .data.json 路径
data_entries: dict = field(default_factory=dict)   # 数据条目
```

Add the `field` import at the top if not already present — it should already be imported from dataclasses.

- [ ] **Step 2: Write _group_name and _field_name helpers**

Add two private methods to `PlaywrightGenerator`:

```python
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
```

- [ ] **Step 3: Write _build_data_entries method**

Add to `PlaywrightGenerator`:

```python
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
```

- [ ] **Step 4: Write _render_data_json method**

Add to `PlaywrightGenerator`:

```python
@staticmethod
def _render_data_json(data: dict[str, dict[str, str]]) -> str:
    """将 data dict 渲染为格式化的 JSON 字符串"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
```

- [ ] **Step 5: Verify — run existing tests to confirm no regression**

```bash
cd /d/MyPrograms/wuzi/RPAsystem && PYTHONUTF8=1 .venv/Scripts/python.exe -c "from pipeline.generator import DataRef, GenerateResult, PlaywrightGenerator; g = PlaywrightGenerator(); print('Import OK')"
```

Expected: `Import OK`

---

### Task 2: Modify _render_action_code to generate d() calls

**Files:**
- Modify: `pipeline/generator.py`

**Interfaces:**
- Consumes: `_build_data_entries()` from Task 1
- Modifies: `_action_to_statement()` — accepts `data_ref: DataRef | None`
- Modifies: `_render_action_code()` — fill/select 分支生成 `d(group, field)` 调用
- Instance state: `self._data_refs: dict[int, DataRef]` set during `generate()`

- [ ] **Step 6: Modify _action_to_statement signature and _render_action_code**

Change `_action_to_statement` to accept an optional `data_ref` parameter:

```python
def _action_to_statement(self, action: ActionInfo, data_ref: DataRef | None = None) -> ScriptStatement:
    """将单个 ActionInfo 映射为 ScriptStatement"""
    code = self._render_action_code(action, data_ref)
    assertion = self._render_assertion(action)

    comment = action.description
    if action.confidence < _LOW_CONFIDENCE_THRESHOLD:
        comment = f"[低置信度 {action.confidence:.2f}，请人工确认] {comment}"

    return ScriptStatement(code=code, comment=comment, assertion=assertion)
```

Modify `_render_action_code` to accept `data_ref`:

```python
def _render_action_code(self, action: ActionInfo, data_ref: DataRef | None = None) -> str:
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
            if data_ref:
                return f"await {locator}.fill(d('{_escape_ts_string(data_ref.group)}', '{_escape_ts_string(data_ref.field)}'));"
            else:
                # 降级：没有 data_ref 时仍用硬编码（向后兼容）
                value = _escape_ts_string(action.input_value)
                return f"await {locator}.fill('{value}');"

        case "select":
            if data_ref:
                return f"await {locator}.selectOption({{ label: d('{_escape_ts_string(data_ref.group)}', '{_escape_ts_string(data_ref.field)}') }});"
            else:
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
```

- [ ] **Step 7: Update all callers of _action_to_statement**

In `_group_into_blocks` — pass `self._data_refs.get(id(action))`:

```python
def _group_into_blocks(self, actions: list[ActionInfo]) -> list[TestBlock]:
    if not actions:
        return [TestBlock(name=self.suite_name, statements=[])]

    blocks: list[TestBlock] = []
    current_name = self._infer_block_name(actions[0])
    current_stmts: list[ScriptStatement] = []

    for action in actions:
        if action.action_type == "navigate" and current_stmts:
            blocks.append(TestBlock(name=current_name, statements=current_stmts))
            current_name  = self._infer_block_name(action)
            current_stmts = []

        data_ref = self._data_refs.get(id(action))
        stmt = self._action_to_statement(action, data_ref)
        current_stmts.append(stmt)

    if current_stmts:
        blocks.append(TestBlock(name=current_name, statements=current_stmts))

    if len(blocks) == 1:
        blocks[0].name = self.suite_name

    return blocks
```

In `_render_before_each` — pass `self._data_refs.get(id(action))`:

```python
def _render_before_each(self, login_actions: list[ActionInfo]) -> str:
    lines = ["", "  test.beforeEach(async ({ page }) => {", "    // 登录"]
    for action in login_actions:
        data_ref = self._data_refs.get(id(action))
        stmt = self._action_to_statement(action, data_ref)
        if stmt.comment:
            lines.append(f"    // {stmt.comment}")
        lines.append(f"    {stmt.code}")
        if stmt.assertion:
            lines.append(f"    {stmt.assertion}")
    lines.append("  });")
    return "\n".join(lines)
```

---

### Task 3: Update script templates (_FILE_HEADER, _IMPORTS, d() function)

**Files:**
- Modify: `pipeline/generator.py`

- [ ] **Step 8: Update _FILE_HEADER — remove obsolete warning**

Replace line 3 of the header comment from:
```
 *    3. 输入值（密码等敏感数据）— 替换为环境变量
```
to:
```
 *    3. 输入值 — 修改同目录下的 .data.json 文件
 *    4. 密码等敏感数据请勿写入 .data.json，改用环境变量
```

- [ ] **Step 9: Add data import template constant**

After `_IMPORTS`, add:

```python
_DATA_IMPORTS = """\
import { test, expect } from '@playwright/test';
import testData from './{data_file}';

const data = testData as Record<string, Record<string, string>>;
function d(group: string, field: string): string {
    return data[group]?.[field] ?? '';
}
"""
```

- [ ] **Step 10: Modify _render_script to use _DATA_IMPORTS**

In `_render_script`, compute `data_file` from output_path stem:

```python
def _render_script(
    self,
    login_actions: list[ActionInfo],
    test_blocks: list[TestBlock],
    data_file: str = "test.data.json",
) -> str:
    """拼装完整的 TypeScript 脚本"""
    parts: list[str] = []

    # 文件头注释
    parts.append(_FILE_HEADER.format(
        suite_name=self.suite_name,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        base_url=self.base_url or "(未检测到，请手动设置)",
    ))

    # import + data 加载
    parts.append(_DATA_IMPORTS.format(data_file=data_file))

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
```

- [ ] **Step 11: Update generate() to pass data_file name**

In `generate()`, compute `data_file` from output_path and pass it:

```python
def generate(self, source, output_path="scripts/output.spec.ts") -> GenerateResult:
    t_start = time.perf_counter()
    actions = source.actions if isinstance(source, AnalyzeResult) else source

    # Compute data file name
    spec_stem = Path(output_path).stem
    data_file = f"{spec_stem}.data.json"

    result = GenerateResult(
        script="",
        output_path=output_path,
        total_actions=len(actions),
    )

    # 1. 过滤 + 分组
    valid, skipped = self._filter_actions(actions)
    result.skipped_actions = skipped

    # 2. 构建数据引用
    data_entries, self._data_refs = self._build_data_entries(valid)
    result.data_entries = data_entries

    login_actions, flow_actions = self._split_login(valid)
    test_blocks   = self._group_into_blocks(flow_actions)
    result.test_blocks = len(test_blocks)

    # 3. 渲染脚本
    script = self._render_script(login_actions, test_blocks, data_file=data_file)
    result.script = script

    # 4. 写 .spec.ts
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")

    # 5. 写 .data.json
    data_json_path = str(out.parent / data_file)
    data_json_content = self._render_data_json(data_entries)
    Path(data_json_path).write_text(data_json_content, encoding="utf-8")
    result.data_json_path = data_json_path

    result.elapsed_sec = round(time.perf_counter() - t_start, 4)
    return result
```

Add `self._data_refs: dict[int, DataRef] = {}` initializer to `__init__`:

```python
def __init__(self, ...):
    ...
    self.min_confidence = min_confidence
    self._data_refs: dict[int, DataRef] = {}   # 在 generate() 中填充
```

---

### Task 4: Update generate_script() convenience function

**Files:**
- Modify: `pipeline/generator.py`

- [ ] **Step 12: No changes needed** — generate_script delegates to gen.generate() which now writes .data.json automatically

---

### Task 5: Update validator HARDCODED_CREDENTIAL rule

**Files:**
- Modify: `pipeline/validator.py`

- [ ] **Step 13: Update _check_hardcoded_credentials regex**

The current pattern matches `.fill('xxx')` after password-related keywords. After the change, `.fill()` calls use `d('group', 'field')` — these are NOT hardcoded credentials. The pattern needs to NOT flag `d(` calls.

Replace the `_check_hardcoded_credentials` function:

```python
def _check_hardcoded_credentials(script: str) -> list[Issue]:
    """检测疑似硬编码密码 — 排除已使用 d() 数据引用的调用"""
    issues = []
    # 匹配密码相关 locator 后紧跟 .fill('字面量值')，但不是 .fill(d(...))
    pattern = re.compile(
        r"(?:password|passwd|密码|PASSWORD)[^;]*?"
        r"\.fill\(\s*(?!d\()['\"]([^'\"]{1,50})['\"]",
        re.IGNORECASE,
    )
    for m in pattern.finditer(script):
        line_no = script[: m.start()].count("\n") + 1
        issues.append(Issue("warning", "HARDCODED_CREDENTIAL",
                            f"第 {line_no} 行疑似硬编码密码，请改用数据文件或 process.env.PASSWORD",
                            line=line_no))
    return issues
```

Key change: `(?!d\()` — negative lookahead ensures `.fill(d(...))` is NOT flagged.

---

### Task 6: Update main.py console output

**Files:**
- Modify: `main.py`

- [ ] **Step 14: Print data.json path after script generation**

After the existing scripts output message (around line 234), add:

```python
if gen.data_json_path:
    console.print(f"[green]✓[/] 数据模板  [dim]{gen.data_json_path}[/]")
```

And update the final Panel summary to mention the data file:

```python
console.print(Panel.fit(
    f"[bold green]完成![/]  总耗时 {elapsed}s\n"
    f"脚本路径: [cyan]{effective_output}[/]\n"
    f"数据模板: [cyan]{gen.data_json_path}[/]\n\n"
    f"[dim]编辑数据 → 运行: npx playwright test {effective_output}[/]",
    border_style="green",
))
```

---

### Task 7: Tests

**Files:**
- Create: `tests/test_generator.py`

- [ ] **Step 15: Write tests for _build_data_entries**

```python
"""Tests for pipeline/generator data injection"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pipeline.analyzer import ActionInfo, ElementHint
from pipeline.generator import PlaywrightGenerator


def make_action(action_type: str, page_context: str = "",
                label: str = "", placeholder: str = "", text: str = "",
                input_value: str = "") -> ActionInfo:
    """Helper to build ActionInfo for tests"""
    return ActionInfo(
        frame_idx=0, timestamp=0.0, frame_path="",
        action_type=action_type,
        element_hint=ElementHint(
            text=text, placeholder=placeholder, label=label, role="textbox"
        ),
        input_value=input_value,
        page_context=page_context,
        description="",
        confidence=0.9,
    )


class TestBuildDataEntries:
    """Tests for _build_data_entries"""

    def test_fill_actions_extracted(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="登录页",
                        placeholder="请输入用户名", input_value="admin"),
            make_action("fill", page_context="登录页",
                        placeholder="请输入密码", input_value="secret"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert len(data) == 1
        assert "登录页" in data
        assert data["登录页"]["请输入用户名"] == "admin"
        assert data["登录页"]["请输入密码"] == "secret"

    def test_click_actions_skipped(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("click", page_context="首页", text="登录按钮"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert len(data) == 0

    def test_select_included(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("select", page_context="表单",
                        label="课程类型", input_value="必修"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert data["表单"]["课程类型"] == "必修"

    def test_duplicate_groups_get_suffix(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="课程管理",
                        text="课程名称", input_value="语文"),
            make_action("fill", page_context="课程管理",
                        text="课程名称", input_value="数学"),
        ]
        data, refs = gen._build_data_entries(actions)
        # 第一组无后缀，第二组加 _2
        assert "课程管理" in data
        assert "课程管理_2" in data
        assert data["课程管理"]["课程名称"] == "语文"
        assert data["课程管理_2"]["课程名称"] == "数学"

    def test_field_priority_label_over_placeholder(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", page_context="表单",
                             label="用户名", placeholder="请输入", input_value="test")
        data, _ = gen._build_data_entries([action])
        assert "用户名" in data["表单"]

    def test_fallback_group_and_field(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", page_context="", input_value="bare value")
        data, refs = gen._build_data_entries([action])
        assert "默认步骤" in data
        assert "字段" in data["默认步骤"]

    def test_same_group_field_dedup(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="表单",
                        text="字段", input_value="a"),
            make_action("fill", page_context="表单",
                        text="字段", input_value="b"),
        ]
        data, _ = gen._build_data_entries(actions)
        assert "字段" in data["表单"]
        assert "字段_2" in data["表单"]


class TestRenderActionCode:
    """Tests for _render_action_code with data refs"""

    def test_fill_uses_d_call(self):
        gen = PlaywrightGenerator()
        from pipeline.generator import DataRef
        action = make_action("fill", placeholder="请输入密码", input_value="old")
        ref = DataRef(group="登录页", field="请输入密码", value="newpass")
        code = gen._render_action_code(action, ref)
        assert "d('登录页', '请输入密码')" in code
        assert "old" not in code  # no hardcoded value

    def test_fill_fallback_without_ref(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", placeholder="搜索", input_value="hello")
        code = gen._render_action_code(action, None)
        assert ".fill('hello')" in code

    def test_click_unchanged(self):
        gen = PlaywrightGenerator()
        action = make_action("click", text="提交按钮")
        code = gen._render_action_code(action, None)
        assert ".click()" in code
        assert "d(" not in code


class TestRenderDataJson:
    """Tests for _render_data_json"""

    def test_valid_json_output(self):
        gen = PlaywrightGenerator()
        data = {"登录页": {"密码": "secret"}}
        output = gen._render_data_json(data)
        parsed = json.loads(output)
        assert parsed == data

    def test_chinese_keys_preserved(self):
        gen = PlaywrightGenerator()
        data = {"用户登录": {"请输入密码": "abc123"}}
        output = gen._render_data_json(data)
        assert "用户登录" in output
        assert "请输入密码" in output


class TestGenerateIntegration:
    """Integration tests — generate with data output"""

    def test_generate_produces_data_json(self):
        gen = PlaywrightGenerator(suite_name="测试")
        actions = [
            make_action("fill", page_context="登录", placeholder="用户名",
                        input_value="admin"),
            make_action("fill", page_context="登录", placeholder="密码",
                        input_value="pass"),
            make_action("click", page_context="登录", text="登录"),
            make_action("navigate", page_context="首页", url="http://example.com"),
            make_action("fill", page_context="表单", label="课程名",
                        input_value="数学"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = str(Path(tmp) / "test.spec.ts")
            result = gen.generate(actions, output_path=spec_path)

            assert Path(spec_path).exists()
            assert not result.script.startswith("// TODO")  # script is non-empty
            assert result.data_json_path
            assert Path(result.data_json_path).exists()

            # Check data.json content
            data_content = Path(result.data_json_path).read_text("utf-8")
            data = json.loads(data_content)
            assert "登录" in data
            assert data["登录"]["用户名"] == "admin"
            assert data["登录"]["密码"] == "pass"
            assert "表单" in data
            assert data["表单"]["课程名"] == "数学"

            # Check spec.ts uses d() calls
            assert "d('登录', '用户名')" in result.script
            assert "d('登录', '密码')" in result.script
            assert "d('表单', '课程名')" in result.script

            # Check no hardcoded fill values
            assert ".fill('admin')" not in result.script
            assert ".fill('pass')" not in result.script
            assert ".fill('数学')" not in result.script

    def test_no_data_when_no_fill_actions(self):
        gen = PlaywrightGenerator(suite_name="导航测试")
        actions = [
            make_action("navigate", url="http://example.com"),
            make_action("click", text="链接"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = str(Path(tmp) / "nav.spec.ts")
            result = gen.generate(actions, output_path=spec_path)
            # Empty data file should still be written
            assert Path(result.data_json_path).exists()
            data = json.loads(Path(result.data_json_path).read_text("utf-8"))
            assert data == {}
```

- [ ] **Step 16: Run tests**

```bash
cd /d/MyPrograms/wuzi/RPAsystem && PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_generator.py -v
```

Expected: all tests pass.

---

### Task 8: End-to-end smoke test

- [ ] **Step 17: Run generate with real video**

```bash
cd /d/MyPrograms/wuzi/RPAsystem && PYTHONUTF8=1 .venv/Scripts/python.exe main.py generate video/test.mp4.mp4 --output scripts/test_data.spec.ts --suite-name "测试数据注入"
```

Check:
- `scripts/test_data.spec.ts` contains `import testData from './test_data.data.json'`
- `scripts/test_data.spec.ts` contains `function d(group: string, field: string)`
- All `.fill()` calls use `d(` syntax
- `scripts/test_data.data.json` exists with valid JSON
- Console output shows both file paths

- [ ] **Step 18: Run validator on generated script**

```bash
cd /d/MyPrograms/wuzi/RPAsystem && PYTHONUTF8=1 .venv/Scripts/python.exe main.py validate scripts/test_data.spec.ts
```

Expected: no HARDCODED_CREDENTIAL warning.

---
