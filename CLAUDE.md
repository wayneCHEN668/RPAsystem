# RPAsystem — Claude Code 规则文档

## 项目概述
将业务系统操作录屏转换为 Playwright 自动化脚本，并支持注入外部数据执行复杂业务流程。

## 技术栈
- Python 3.11+（后端流水线）
- Playwright（浏览器自动化）
- TypeScript（生成的测试脚本）
- Anthropic Claude API（视觉理解 + 代码生成）

## 核心约束

### 代码风格
- 所有 Python 模块使用 async/await（IO 密集型操作）
- 类型注解强制（使用 `from __future__ import annotations`）
- 日志统一使用 `loguru`，不用 `print`
- 配置只从 `config/settings.py` 读取，不要硬编码

### 模块职责边界
- `pipeline/` 只负责视频→脚本转换，不执行任何浏览器操作
- `engine/` 只负责执行，不包含业务逻辑
- `data/` 只提供数据，不知道自动化的存在
- 跨模块传递数据用 TypedDict 或 dataclass，不用裸 dict

### 生成脚本规范
- 生成的 .spec.ts 必须使用语义化 locator（getByRole/getByText）
- 禁止生成硬编码等待（time.sleep / page.waitForTimeout）
- 敏感数据（密码等）必须用 process.env 读取

### 错误处理
- 关键操作必须有 tenacity 重试（最多 3 次，指数退避）
- 每个重要步骤执行后截图存档
- 所有异常必须记录完整堆栈到日志

## 目录说明
- `frames/` 是临时目录，不提交 git，处理完自动清理
- `scripts/` 下的生成脚本需要人工审核后才能用于生产
- `config/flows/` 存放 YAML 格式的流程定义，是可以复用的业务配置

## 开发顺序建议
1. 先完善 `pipeline/` 三个核心文件
2. 再实现 `data/provider.py` 统一接口
3. 最后接 `engine/runner.py` 串联执行