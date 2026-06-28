# script_generator.py
import anthropic
import json

client = anthropic.Anthropic()

CODE_GEN_PROMPT = """
你是一个 Playwright 自动化脚本专家。根据以下操作序列，生成高质量的 Playwright TypeScript 测试脚本。

操作序列（JSON）：
{actions_json}

要求：
1. 使用 page.getByRole() / page.getByText() / page.getByPlaceholder() 等语义化 locator，优先于 CSS selector
2. 每个操作前添加注释说明业务意图
3. 关键操作后添加 await expect() 断言
4. 使用 await page.waitForLoadState('networkidle') 等待页面加载
5. 输入敏感数据（密码等）使用环境变量
6. 脚本结构：
   - 顶部 import
   - test.describe 分组
   - beforeEach 处理登录（如有）
   - test 主流程

只输出 TypeScript 代码，不要有解释文字。
"""

def generate_playwright_script(actions: list[dict], output_path: str = "test_flow.spec.ts"):
    """将操作序列生成 Playwright 脚本"""
    # 过滤掉解析失败的帧
    valid_actions = [a for a in actions if "error" not in a]
    
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": CODE_GEN_PROMPT.format(
                    actions_json=json.dumps(valid_actions, ensure_ascii=False, indent=2)
                )
            }
        ]
    )
    
    script = message.content[0].text
    
    # 清理可能的 markdown 代码块标记
    script = script.replace("```typescript", "").replace("```ts", "").replace("```", "").strip()
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(script)
    
    print(f"脚本已生成：{output_path}")
    return script