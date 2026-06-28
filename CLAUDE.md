# RPAsystem — Claude Code 规则文档

## 项目概述

将业务系统**操作录屏**转换为可执行的 **Playwright TypeScript** 自动化测试脚本，
并支持注入外部数据执行复杂多页面业务流程。

```
录屏视频
  │
  ▼ pipeline/preprocessor.py   视频抽帧 + SSIM 筛关键帧
  │
  ▼ pipeline/analyzer.py       Claude Vision 识别操作意图
  │
  ▼ pipeline/generator.py      生成 Playwright .spec.ts 脚本
  │
  ▼ pipeline/validator.py      双层验证（规则检查 + tsc 语法）
  │
  ▼ scripts/*.spec.ts          可执行的自动化脚本
```

## 技术栈

| 层 | 语言 / 框架 |
|---|---|
| pipeline / CLI | Python 3.11+ |
| 视频处理 | opencv-python + scikit-image |
| AI 视觉分析 | Anthropic Claude Vision API |
| 脚本生成 | 纯字符串模板（无 AST 依赖） |
| 脚本验证 | 自定义规则 + tsc --noEmit |
| 生成的测试脚本 | TypeScript + Playwright |
| CLI | typer + rich |

## 目录结构

```
RPAsystem/
├── pipeline/          # 核心流水线（职责严格分离）
│   ├── preprocessor.py   抽帧，无外部 API 依赖
│   ├── analyzer.py       LLM 分析，依赖 anthropic SDK
│   ├── generator.py      代码生成，纯 Python
│   └── validator.py      验证，依赖 ts_check/ 环境
├── engine/            # 执行引擎（runner/browser/scheduler/reporter）
├── data/              # 数据层（api_client/db_client/file_reader）
├── config/
│   └── settings.py       从 .env 读取配置
├── scripts/           # 生成的 .spec.ts 脚本
├── tests/             # 所有测试（119 个，全部通过）
├── ts_check/          # TypeScript 语法检查环境
│   ├── tsconfig.json
│   └── node_modules/  # 需要提交，validator 依赖
└── main.py            # CLI 入口
```

## 核心约束

### 模块职责边界（不可越界）
- `pipeline/` 只负责 **视频→脚本** 转换，不执行任何浏览器操作
- `engine/` 只负责 **执行脚本**，不包含生成逻辑
- `data/` 只提供数据，不知道自动化的存在
- 跨模块传递数据必须用 `@dataclass`，禁止裸 `dict`

### Python 代码规范
- 所有文件顶部加 `from __future__ import annotations`
- IO 密集型操作使用 `async/await`
- 类型注解强制（函数签名必须有参数类型和返回类型）
- 日志统一用 `loguru`，禁止 `print`（测试脚本除外）
- 配置只从 `config/settings.py` 读取，禁止在业务代码里 `os.environ.get`

### 生成脚本规范（TypeScript）
- **必须**使用语义化 locator（`getByRole` > `getByLabel` > `getByPlaceholder` > `getByText`）
- **禁止**硬编码等待（`time.sleep` / `page.waitForTimeout`）
- **禁止**硬编码密码，必须用 `process.env.PASSWORD`
- 选择器无法确定时生成 `TODO` 注释，不造成语法错误

### 错误处理
- 关键操作加 `tenacity` 重试（最多 3 次，指数退避）
- `pipeline/` 中单帧分析失败**不中断**整体流程，降级为 `unknown`
- CLI 命令遇到不可恢复错误 → `raise typer.Exit(1)`，输出清晰的错误信息

### 测试规范
- 新功能必须有对应测试，覆盖正常路径 + 边界 + 异常
- 测试禁止依赖真实 API key / 真实视频，用 Mock 或合成数据
- 集成测试（需要 API key）放在测试函数末尾，用 `SKIP` 处理环境缺失

## 数据结构速查

```python
# preprocessor 输出
PreprocessResult.frames: list[FrameInfo]
FrameInfo: frame_idx, timestamp, path, ssim_score, width, height

# analyzer 输出
AnalyzeResult.actions: list[ActionInfo]
ActionInfo: frame_idx, timestamp, action_type, element_hint, input_value,
            url, page_context, description, confidence, parse_error
ElementHint: text, placeholder, label, role, location

# generator 输出
GenerateResult: script(str), output_path, total_actions, skipped_actions, test_blocks

# validator 输出
ValidateResult: issues(list[Issue]), passed(bool), tsc_checked, errors, warnings, infos
Issue: level("error"/"warning"/"info"), code, message, line
```

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 安装 TypeScript 检查环境（只需一次）
cd ts_check && npm install && cd ..

# 调试抽帧效果（不花 API 费）
python main.py inspect recording.mp4 --threshold 0.90

# 生成脚本（完整流程）
python main.py generate recording.mp4 \
  --output scripts/test_order.spec.ts \
  --suite-name "工单创建流程"

# 验证脚本
python main.py validate scripts/test_order.spec.ts

# 运行 Playwright 脚本
npx playwright test scripts/test_order.spec.ts

# 运行所有测试
python -m pytest tests/ -v

# 运行单个模块测试
python tests/test_preprocessor.py
python tests/test_analyzer.py
python tests/test_generator.py
python tests/test_validator.py
python tests/test_main.py
```

## 开发路径

当前完成状态：

```
✅ pipeline/preprocessor.py    9 tests
✅ pipeline/analyzer.py       13 tests
✅ pipeline/generator.py      36 tests
✅ pipeline/validator.py      38 tests
✅ main.py                    23 tests
⬜ engine/runner.py           待实现
⬜ engine/browser.py          待实现
⬜ engine/scheduler.py        待实现
⬜ engine/reporter.py         待实现
⬜ data/provider.py           待实现
⬜ data/api_client.py         待实现
⬜ data/db_client.py          待实现
⬜ data/file_reader.py        待实现
```

继续开发时，**先写 `engine/browser.py`**，它是执行层最底层的依赖，
其余 engine 模块都依赖它。
