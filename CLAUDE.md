# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

将业务系统**操作录屏**转换为可执行的 **Playwright TypeScript** 自动化测试脚本，
并支持注入外部数据执行复杂多页面业务流程。

```
录屏视频
  │
  ▼ pipeline/preprocessor.py   视频抽帧 + SSIM 筛关键帧
  │
  ▼ pipeline/analyzer.py       Qwen VL（DashScope）识别操作意图
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
| AI 视觉分析 | DashScope SDK → Qwen VL (qwen-vl-max-latest) |
| 脚本生成 | 纯字符串模板（无 AST 依赖） |
| 脚本验证 | 自定义规则 + tsc --noEmit |
| 生成的测试脚本 | TypeScript + Playwright |
| CLI | typer + rich |
| 日志 | loguru（禁止 print） |
| 重试 | tenacity |

## 常用命令

```bash
# 创建虚拟环境并安装依赖（必须在 venv 中）
python -m venv .venv
.venv\Scripts\activate     # Windows
pip install -r requirements.txt

# 调试抽帧效果（不调用 AI，免费）
python main.py inspect recording.mp4
python main.py inspect recording.mp4 --threshold 0.90 --interval 0.2

# 生成脚本（完整 pipeline）
python main.py generate recording.mp4 --output scripts/test_order.spec.ts --suite-name "工单流程"
python main.py generate recording.mp4 --no-validate --keep-frames --save-actions

# 验证已有脚本
python main.py validate scripts/test_order.spec.ts
python main.py validate scripts/test_order.spec.ts --no-tsc

# 运行生成的 Playwright 脚本
npx playwright install chromium
npx playwright test scripts/test_order.spec.ts

# 运行测试
python -m pytest tests/ -v
python tests/test_preprocessor.py
python tests/test_generator.py
```

## API 架构（重要）

项目通过 **DashScope**（阿里云百炼）原生调用 Qwen VL 多模态模型：

- `DASHSCOPE_API_KEY` — API 密钥（必填，从阿里云百炼控制台获取）
- `DASHSCOPE_BASE_URL` — 自定义端点（可选，默认自动选择区域）
- `ANALYZER_MODEL` — 模型标识（默认 `qwen-vl-max-latest`）

代码使用 DashScope SDK（`dashscope` 包），通过 `AioMultiModalConversation.call()` 异步调用。
`dashscope.api_key` 在 `FrameAnalyzer.__init__` 中设置（模块级别全局配置）。
`dashscope.base_http_api_url` 在传入 `base_url` 参数时设置。

## 目录结构

```
RPAsystem/
├── pipeline/          # 核心流水线 —— 视频→脚本（功能完整）
│   ├── preprocessor.py  无外部 API 依赖
│   ├── analyzer.py      依赖 dashscope SDK（Qwen VL）
│   ├── generator.py     纯 Python 字符串模板
│   └── validator.py     依赖 ts_check/ 环境
├── engine/            # 执行引擎 —— 运行生成的脚本（架构就绪，部分功能待完善）
│   ├── runner.py        流程编排
│   ├── browser.py       Playwright 浏览器封装
│   ├── scheduler.py     批量/定时调度
│   └── reporter.py      执行报告
├── data/              # 数据注入层 —— 为引擎提供外部数据（架构就绪）
│   ├── provider.py      统一接口（路由到 API/DB/文件）
│   ├── api_client.py    REST API（带重试 + 缓存）
│   ├── db_client.py     PostgreSQL / MySQL / SQLite
│   └── file_reader.py   Excel / CSV / JSON
├── config/
│   └── settings.py      从 .env 读取配置
├── scripts/           # 生成的 .spec.ts 脚本
├── tests/             # 测试套件
├── ts_check/          # TypeScript 语法检查环境（需初始化）
│   ├── tsconfig.json
│   └── node_modules/  # npm install @playwright/test typescript
├── main.py            # CLI 入口（typer）
└── requirements.txt
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
- **禁止**硬编码等待（`page.waitForTimeout`）
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

## 实现细节注意事项

### analyzer 执行模式
尽管 `FrameAnalyzer` 接受 `max_concurrency` 参数并使用 `Semaphore`，当前 `_run()` 实现是**严格顺序**的——每帧 `await` 完成后才处理下一帧，因为每帧需要上一帧的 `description` 作为上下文。这意味着 LLM 调用实际上是串行的。如果要实现真正的并发，需要改为批次模式（第一批串行建立上下文，后续批次内并发）。

### ts_check 环境初始化
validator 的 Layer 2（tsc 语法检查）依赖 `ts_check/` 目录下的 TypeScript 环境。
使用前需初始化：
```bash
cd ts_check
npm init -y
npm install @playwright/test typescript
# 创建 tsconfig.json 配置 @playwright/test 类型
```

### 依赖安装
Python 依赖必须安装在虚拟环境中（`.venv/`）。安装前激活 venv。
