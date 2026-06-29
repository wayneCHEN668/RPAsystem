/**
 * Test
 * 由 RPAsystem pipeline/generator.py 自动生成
 * 生成时间: 2026-06-29 14:19:54
 * 目标系统: (未检测到，请手动设置)
 *
 * ⚠️  自动生成的脚本需要人工审核后才能用于生产
 *    重点检查：
 *    1. 标有 TODO 的选择器 — 需要手动补充
 *    2. 标有「低置信度」的操作 — AI 不确定，请验证
 *    3. 输入值（密码等敏感数据）— 替换为环境变量
 */
import { test, expect } from '@playwright/test';

test.describe("Test", () => {

  test.beforeEach(async ({ page }) => {
    // 登录
    // 在密码输入框中输入密码
    await page.getByRole('textbox', { name: '请输入密码' }).fill('用户输入的密码');
    // 点击弹窗中的「关闭」按钮以关闭密码安全检查提示。
    await page.getByRole('button', { name: '关闭' }).click();
  });

  test("Test", async ({ page }) => {
    // 点击左侧导航栏的「课程管理」菜单项
    await page.getByRole('menuitem', { name: '课程管理' }).click();
    // 点击左侧导航栏的「课程列表」菜单项
    await page.getByRole('menuitem', { name: '课程列表' }).click();
    // 在「创建课程」表单中，向「课程名称」输入框填写课程代码
    await page.getByRole('textbox', { name: '课程名称' }).fill('C20260629101324');
    // 在「创建课程」表单中，点击并准备输入课程简介。
    await page.getByRole('textbox').fill('');
    // 在「课程简介」输入框中准备输入内容。
    await page.getByRole('textbox', { name: '输入课程简介...' }).fill('');
    // 在课程名称输入框中输入“PowerPoint2010应用与模拟测验”
    await page.getByRole('textbox', { name: '输入课程名称' }).fill('PowerPoint2010应用与模拟测验');
    // 等待从课件库添加弹窗加载完成，当前显示加载中状态。
    await page.waitForLoadState('networkidle', { timeout: 10000 });
    await expect(page.locator('.loading')).toBeHidden();
    // 在「从课件库添加」弹窗中，点击选择一个名为「制作绿色城市PPT-动作的使用」的课件。
    await page.getByRole('menuitem', { name: '制作绿色城市PPT-动作的使用' }).click();
    // 点击「保存课程」按钮以提交课程创建表单。
    await page.getByRole('button', { name: '保存课程' }).click();
    // 在课程创建表单中输入课程名称“测试课程”
    await page.getByRole('textbox', { name: '测试课程' }).fill('测试课程');
  });
});
