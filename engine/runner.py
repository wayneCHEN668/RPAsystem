# engine/runner.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from loguru import logger
from playwright.async_api import Page

from config.settings import settings
from data.provider import DataProvider
from engine.browser import BrowserSession
from engine.reporter import Reporter, StepResult, FlowResult


@dataclass
class FlowStep:
    """描述一个业务操作步骤"""
    name: str
    action: Callable[[Page, dict[str, Any]], Awaitable[None]]
    screenshot: bool = True          # 执行后是否截图
    critical: bool = True            # 失败是否终止整个流程
    data_keys: list[str] = field(default_factory=list)  # 需要哪些外部数据字段


@dataclass
class FlowDefinition:
    """一个完整业务流程的定义"""
    name: str
    base_url: str
    steps: list[FlowStep]
    data_source: str = "none"        # api / db / excel / none
    data_params: dict[str, Any] = field(default_factory=dict)


class FlowRunner:
    """
    执行一个业务流程定义，注入外部数据，记录每步结果。
    
    用法:
        runner = FlowRunner(data_provider=provider)
        result = await runner.run(flow_def)
    """

    def __init__(self, data_provider: DataProvider | None = None):
        self.data_provider = data_provider or DataProvider()
        self.reporter = Reporter()

    async def run(self, flow: FlowDefinition) -> FlowResult:
        """执行整个流程，返回包含所有步骤结果的报告"""
        logger.info(f"开始执行流程: {flow.name}")
        flow_result = FlowResult(flow_name=flow.name, start_time=time.time())

        # 1. 预先获取所有需要的外部数据
        business_data = await self._fetch_business_data(flow)
        logger.info(f"已加载业务数据: {list(business_data.keys())}")

        # 2. 启动浏览器会话执行流程
        async with BrowserSession() as session:
            page = await session.new_page()
            await session.safe_goto(page, flow.base_url)

            for i, step in enumerate(flow.steps):
                step_result = await self._execute_step(
                    session, page, step, business_data, index=i
                )
                flow_result.steps.append(step_result)

                # 关键步骤失败则终止
                if not step_result.success and step.critical:
                    logger.error(f"关键步骤 [{step.name}] 失败，终止流程")
                    flow_result.aborted = True
                    break

        flow_result.end_time = time.time()
        flow_result.success = all(s.success for s in flow_result.steps if flow.steps[i].critical
                                   for i, s in enumerate(flow_result.steps))

        # 3. 生成报告
        report_path = await self.reporter.generate(flow_result)
        flow_result.report_path = report_path

        logger.info(
            f"流程 [{flow.name}] 完成 "
            f"{'成功' if flow_result.success else '失败'} "
            f"耗时 {flow_result.duration:.1f}s  报告: {report_path}"
        )
        return flow_result

    async def _execute_step(
        self,
        session: BrowserSession,
        page: Page,
        step: FlowStep,
        business_data: dict[str, Any],
        index: int,
    ) -> StepResult:
        """执行单个步骤，捕获异常，记录截图"""
        logger.info(f"  步骤 {index + 1}: {step.name}")
        result = StepResult(name=step.name, start_time=time.time())

        try:
            # 只传递该步骤声明需要的数据字段
            step_data = (
                {k: business_data[k] for k in step.data_keys if k in business_data}
                if step.data_keys
                else business_data
            )
            await step.action(page, step_data)
            result.success = True
            logger.success(f"  ✓ {step.name}")

        except Exception as e:
            result.success = False
            result.error = str(e)
            logger.error(f"  ✗ {step.name}: {e}")

            if settings.screenshot_on_fail:
                result.screenshot = await session.screenshot(
                    page, f"step_{index + 1:02d}_{step.name}_FAIL"
                )

        else:
            if step.screenshot:
                result.screenshot = await session.screenshot(
                    page, f"step_{index + 1:02d}_{step.name}_OK"
                )

        result.end_time = time.time()
        return result

    async def _fetch_business_data(self, flow: FlowDefinition) -> dict[str, Any]:
        """根据流程配置从对应数据源获取业务数据"""
        if flow.data_source == "none" or not self.data_provider:
            return {}

        match flow.data_source:
            case "api":
                return await self.data_provider.from_api(**flow.data_params)
            case "db":
                return await self.data_provider.from_db(**flow.data_params)
            case "excel":
                return self.data_provider.from_excel(**flow.data_params)
            case _:
                logger.warning(f"未知数据源类型: {flow.data_source}")
                return {}