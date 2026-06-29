# Spec: Generator 数据注入改造

**日期**: 2026-06-29
**状态**: 已确认

## 目标

改造 `pipeline/generator.py`，使生成的 `.spec.ts` 脚本不再硬编码输入值，
而是从外部 JSON 数据文件读取。同时自动生成数据模板文件。

## 数据流

```
录屏 → analyzer (LLM 识别 page_context + element_hint)
         │
         ▼
     generator
         │
         ├─→ scripts/test.spec.ts    (通过 d(group, field) 引用数据)
         └─→ scripts/test.data.json  (数据模板，用户修改后即可运行)
```

## 数据结构

### 输出: test.data.json

```json
{
  "用户登录": {
    "用户名": "admin",
    "登录密码": "password123"
  },
  "课程管理_1": {
    "课程名称": "PowerPoint2010应用与模拟测验",
    "课程代码": "C20260629101324"
  },
  "课程管理_2": {
    "测试课程": "测试课程"
  }
}
```

### 分组与字段命名规则

| 来源 | 用途 | 规则 |
|---|---|---|
| `action.page_context` | JSON 组名 | 清理特殊字符（去空格、去标点），相同组名加 `_1` `_2` 后缀 |
| `element_hint.label` > `placeholder` > `text` | JSON 字段名 | 取优先级最高的非空值，中文直接保留 |
| `action.input_value` | JSON 字段值（模板默认值） | LLM 识别到的用户输入原值 |

### 数据结构约束

- 只对 `action_type = fill | select` 的 actions 生成数据条目
- `navigate` 的 URL 保留现有逻辑（直接写入脚本，不从数据文件读取）
- `action_type = click | scroll | hover | wait | unknown` 不产生数据条目

## 生成的 .spec.ts 模板

### 新增头部

```typescript
import testData from './test.data.json';

const data = testData as Record<string, Record<string, string>>;

function d(group: string, field: string): string {
    return data[group]?.[field] ?? '';
}
```

### fill / select 语句变化

**之前：**
```typescript
await page.getByRole('textbox', { name: '请输入密码' }).fill('用户输入的密码');
await page.getByRole('combobox', { name: '课程名称' }).selectOption({ label: 'PowerPoint2010' });
```

**之后：**
```typescript
await page.getByRole('textbox', { name: '请输入密码' }).fill(d('用户登录', '登录密码'));
await page.getByRole('combobox', { name: '课程名称' }).selectOption({ label: d('课程管理_1', '课程名称') });
```

### 完整脚本输出示例

```typescript
import { test, expect } from '@playwright/test';
import testData from './test.data.json';

const data = testData as Record<string, Record<string, string>>;
function d(group: string, field: string): string {
    return data[group]?.[field] ?? '';
}

test.describe('工单流程', () => {
    test('自动化测试', async ({ page }) => {
        await page.goto('http://localhost:8085');

        // 登录
        await page.getByRole('textbox', { name: '请输入用户名' }).fill(d('用户登录', '用户名'));
        await page.getByRole('textbox', { name: '请输入密码' }).fill(d('用户登录', '登录密码'));
        await page.getByRole('button', { name: '登录' }).click();

        // 课程管理 - 创建课程
        await page.getByRole('button', { name: '创建课程' }).click();
        await page.getByRole('textbox', { name: '课程名称' }).fill(d('课程管理_1', '课程名称'));
        await page.getByRole('textbox', { name: '课程代码' }).fill(d('课程管理_1', '课程代码'));
        await page.getByRole('button', { name: '提交' }).click();
    });
});
```

## Generator 改造点

### 1. 新增方法: `_build_data_entries()`

```python
def _build_data_entries(self, actions: list[ActionInfo]) -> dict[str, dict[str, str]]:
    """
    从 actions 中提取所有 fill/select 操作，
    按 page_context 分组，构建 data.json 结构。
    """
```

- 遍历所有 actions，过滤 `action_type in ('fill', 'select')`
- 按 `page_context` 分组，特殊字符清理后作为 JSON key
- 同组名冲突时加 `_1` `_2` 后缀
- 字段名从 `element_hint` 取（优先级: label > placeholder > text）

### 2. 新增方法: `_render_data_json()`

```python
def _render_data_json(self, data_entries: dict) -> str:
    """将 data_entries 渲染为格式化的 JSON 字符串"""
```

### 3. 修改: `_render_action_code()`

`fill` 和 `select` 的渲染逻辑从直接拼接值改为生成 `d(group, field)` 调用。

### 4. 修改: `_render_imports()` / 脚本头部

新增 `import testData from './xxx.data.json'`、`const data = ...`、`function d(...)` 的模板。

### 5. 扩展: `GenerateResult`

新增字段:
```python
data_json_path: str       # 输出的 .data.json 路径
data_entries: dict         # 数据条目内容
```

### 6. 扩展: main.py generate 命令

生成 `.spec.ts` 后自动写入 `.data.json` 模板文件。

## 对现有模块的影响

| 模块 | 影响 |
|---|---|
| `pipeline/preprocessor.py` | 无 |
| `pipeline/analyzer.py` | 无 |
| `pipeline/generator.py` | **主要改造文件** |
| `pipeline/validator.py` | `HARDCODED_CREDENTIAL` 规则需更新匹配模式（从 `.fill('字面量')` 改为 `.fill(d(...))`） |
| `main.py` | generate 命令新增 .data.json 输出 |
| `data/` | 本次不改，保持独立 |
| `engine/` | 本次不改 |

## 向后兼容

- LLM 误判导致 `page_context` 或 `element_hint` 为空时：
  - group 名降级为 `"默认步骤"`
  - field 名降级为 `"字段_N"`（递增编号）
- data.json 中的默认值即 LLM 识别到的原值，用户直接使用不改也能跑

## 验证方式

1. 用现有视频 `video/test.mp4.mp4` 运行 `generate`，确认同时生成 `.spec.ts` 和 `.data.json`
2. 检查 `.spec.ts` 中所有 `.fill()` / `.selectOption()` 均使用 `d(group, field)` 调用
3. 检查 `.data.json` 结构正确，字段名和默认值合理
4. `python -m pytest tests/test_generator.py -v` 测试通过
5. 运行 validator 确认 `HARDCODED_CREDENTIAL` 不再误报
