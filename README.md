# RPAsystem

将业务系统**操作录屏**转换为可执行的 Playwright 自动化测试脚本。

录一段操作视频 → 运行一条命令 → 得到可直接执行的 `.spec.ts` 脚本。

---

## 工作原理

```
recording.mp4
     │
     ▼  1. 视频抽帧          opencv + SSIM 差异检测，过滤重复帧
     │
     ▼  2. AI 分析操作意图   Claude Vision 识别每帧的 click / fill / navigate …
     │
     ▼  3. 生成脚本          按页面分组，生成语义化 Playwright locator
     │
     ▼  4. 验证              结构规则检查 + tsc 语法检查
     │
scripts/test_order.spec.ts
```

生成的脚本示例：

```typescript
import { test, expect } from '@playwright/test';

test.describe("工单创建流程", () => {

  test.beforeEach(async ({ page }) => {
    // 打开登录页面
    await page.goto('https://your-system.com/login');
    // 填写用户名
    await page.getByPlaceholder('请输入用户名').fill(process.env.SYS_USERNAME || '');
    // 填写密码
    await page.getByPlaceholder('请输入密码').fill(process.env.SYS_PASSWORD || '');
    // 点击登录按钮
    await page.getByRole('button', { name: '登录' }).click();
  });

  test("工单管理", async ({ page }) => {
    // 进入工单管理
    await page.getByRole('menuitem', { name: '工单管理' }).click();
    // 点击新建工单
    await page.getByRole('button', { name: '新建工单' }).click();
    // 填写工单标题
    await page.getByLabel('工单标题').fill('2号热力站管道压力异常');
    // 选择站点
    await page.getByLabel('站点').selectOption({ label: '2号热力站' });
    // 提交审批
    await page.getByRole('button', { name: '提交审批' }).click();
    await expect(page.getByText('提交成功')).toBeVisible();
  });

});
```

---

## 快速开始

### 1. 安装依赖

```bash
git clone https://github.com/wayneCHEN668/RPAsystem.git
cd RPAsystem

pip install -r requirements.txt

# 安装 TypeScript 语法检查环境（只需一次）
cd ts_check && npm install && cd ..
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```bash
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx   # 必填
BASE_URL=https://your-system.com        # 目标系统入口
```

### 3. 录制操作视频

用任意录屏工具录制一段业务操作（OBS / Windows 录屏 / QuickTime），
保存为 `recording.mp4`。

录制建议：
- 分辨率 ≥ 1920×1080
- 每步操作之间留 0.5s 停顿
- 确保 URL 地址栏可见

### 4. 生成脚本

```bash
# 先调试抽帧效果（不调用 AI，免费）
python main.py inspect recording.mp4

# 生成脚本
python main.py generate recording.mp4 \
  --output scripts/test_order.spec.ts \
  --suite-name "工单创建流程"
```

### 5. 运行脚本

```bash
# 安装 Playwright 浏览器（首次）
npx playwright install chromium

# 运行
npx playwright test scripts/test_order.spec.ts
```

---

## 命令参考

### `generate` — 主命令

```
python main.py generate <video> [选项]

选项：
  --output,      -o   输出脚本路径（默认 scripts/<视频名>.spec.ts）
  --suite-name,  -s   test.describe 名称（默认用视频文件名）
  --base-url,    -u   目标系统 URL（覆盖 .env BASE_URL）
  --threshold,   -t   帧差异阈值 0~1（越高提取越多帧，默认 0.92）
  --api-key          Anthropic API Key（覆盖环境变量）
  --no-validate      跳过生成后的脚本验证
  --keep-frames      保留临时关键帧文件（默认自动删除）
  --save-actions     保存 AI 识别的操作序列到 actions.json（调试用）
```

### `inspect` — 调试抽帧

```
python main.py inspect <video> [选项]

不调用 AI，只展示抽帧结果，用于在正式 generate 前确认参数。

选项：
  --threshold, -t   帧差异阈值（默认 0.92）
  --interval,  -i   采样间隔秒数（默认 0.2）
  --json            输出 frames_inspect.json
```

### `validate` — 验证脚本

```
python main.py validate <script.spec.ts> [选项]

选项：
  --no-tsc   跳过 TypeScript 语法检查（仅做结构规则检查）
```

---

## 阈值调参指南

`--threshold` 控制抽帧灵敏度：

| 阈值 | 帧数 | 适用场景 |
|------|------|---------|
| 0.95 | 较多 | 快速点击、菜单展开等小变化 |
| 0.92 | 适中 | **默认推荐**，普通表单操作 |
| 0.88 | 较少 | 页面跳转为主的简单流程 |

帧数过多时提高阈值，帧数过少时降低阈值。先用 `inspect` 命令确认，再用 `generate`。

---

## 验证报告说明

生成脚本后自动验证，问题分三级：

| 级别 | 含义 | 是否阻断 |
|------|------|---------|
| `ERROR` | 脚本无法运行（缺少 import、语法错误） | 必须修复 |
| `WARNING` | 有风险（TODO 选择器、硬编码密码） | 建议检查 |
| `INFO` | 提示信息（无断言、低置信度操作） | 仅供参考 |

常见问题处理：

- **`TODO_SELECTOR`**：AI 无法识别元素，在脚本中找到 `TODO` 注释，手动补充 selector
- **`HARDCODED_CREDENTIAL`**：将密码改为 `process.env.PASSWORD`
- **`EMPTY_TEST_BODY`**：该 test 块没有操作，可能是分组问题，手动合并

---

## 项目结构

```
RPAsystem/
├── pipeline/               # 核心流水线
│   ├── preprocessor.py     视频抽帧（无 API 依赖）
│   ├── analyzer.py         Claude Vision 分析
│   ├── generator.py        Playwright 脚本生成
│   └── validator.py        双层脚本验证
├── engine/                 # 执行引擎（待完善）
│   ├── runner.py           流程编排
│   ├── browser.py          Playwright 封装
│   ├── scheduler.py        定时/批量调度
│   └── reporter.py         执行报告
├── data/                   # 数据注入层（待完善）
│   ├── provider.py         统一数据接口
│   ├── api_client.py       REST API
│   ├── db_client.py        数据库
│   └── file_reader.py      Excel/CSV
├── config/
│   └── settings.py         配置管理
├── scripts/                # 生成的脚本（.gitignore 可选）
├── tests/                  # 测试套件（119 个，全部通过）
├── ts_check/               # TypeScript 检查环境
├── main.py                 # CLI 入口
├── requirements.txt
├── .env.example
└── CLAUDE.md               # Claude Code 开发规范
```

---

## 运行测试

```bash
# 全套测试（119 个）
python -m pytest tests/ -v

# 或逐模块运行
python tests/test_preprocessor.py   #  9 个
python tests/test_analyzer.py       # 13 个（需要 ANTHROPIC_API_KEY 才跑集成测试）
python tests/test_generator.py      # 36 个
python tests/test_validator.py      # 38 个
python tests/test_main.py           # 23 个
```

---

## 注意事项

**生成的脚本需要人工审核后才能用于生产**，重点检查：

1. 标有 `TODO` 的选择器 — 需要手动补充
2. 标有「低置信度」的操作 — AI 不确定，请验证逻辑是否正确
3. 输入值（尤其密码）— 确认已替换为环境变量

AI 识别准确率受以下因素影响：
- 录屏分辨率（越高越准）
- 操作节奏（太快会漏帧）
- 界面语言与 UI 框架（中文 Element Plus / Ant Design 支持良好）

---

## License

MIT
