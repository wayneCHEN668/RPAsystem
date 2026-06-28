# engine/scheduler.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from data.provider import DataProvider
from engine.runner import FlowDefinition, FlowResult, FlowRunner


@dataclass
class BatchTask:
    """批量任务：用同一个流程定义，对多条数据各执行一次"""
    flow: FlowDefinition
    records: list[dict[str, Any]]   # 每条记录覆盖 flow.data_params
    concurrency: int = 1             # 并发数，建议生产环境 ≤ 3


class FlowScheduler:
    """
    两种模式:
    1. 批量模式 — 对一批数据记录依次/并发执行同一流程
    2. 定时模式 — 按 cron 表达式定时触发（依赖 APScheduler）
    """

    def __init__(self, data_provider: DataProvider | None = None):
        self.data_provider = data_provider
        self._results: list[FlowResult] = []

    # ------------------------------------------------------------------ #
    # 批量执行
    # ------------------------------------------------------------------ #

    async def run_batch(self, task: BatchTask) -> list[FlowResult]:
        """
        对 task.records 中每条记录执行一次 flow，支持并发控制。
        
        示例:
            records = [{"order_id": "001"}, {"order_id": "002"}]
            results = await scheduler.run_batch(BatchTask(flow=my_flow, records=records))
        """
        logger.info(
            f"批量执行 [{task.flow.name}]  "
            f"共 {len(task.records)} 条  并发 {task.concurrency}"
        )
        semaphore = asyncio.Semaphore(task.concurrency)
        results: list[FlowResult] = []

        async def run_one(record: dict[str, Any]) -> FlowResult:
            async with semaphore:
                # 克隆 flow 并用当前记录覆盖数据参数
                flow_copy = FlowDefinition(
                    name=f"{task.flow.name}[{record}]",
                    base_url=task.flow.base_url,
                    steps=task.flow.steps,
                    data_source=task.flow.data_source,
                    data_params={**task.flow.data_params, **record},
                )
                runner = FlowRunner(self.data_provider)
                return await runner.run(flow_copy)

        tasks = [run_one(r) for r in task.records]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        success = sum(1 for r in results if r.success)
        logger.info(f"批量完成: {success}/{len(results)} 成功")
        self._results.extend(results)
        return list(results)

    # ------------------------------------------------------------------ #
    # 定时执行（依赖 APScheduler，可选安装）
    # ------------------------------------------------------------------ #

    def schedule_cron(
        self,
        flow: FlowDefinition,
        cron: str = "0 8 * * 1-5",   # 默认工作日 8:00
        data_provider: DataProvider | None = None,
    ) -> None:
        """
        注册定时任务。需要 pip install apscheduler。
        
        示例:
            scheduler.schedule_cron(flow, cron="0 9 * * *")
            scheduler.start()
        """
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            raise RuntimeError("定时功能需要安装: pip install apscheduler")

        if not hasattr(self, "_aps"):
            self._aps = AsyncIOScheduler()

        async def _job() -> None:
            logger.info(f"定时触发: {flow.name}  时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
            runner = FlowRunner(data_provider or self.data_provider)
            result = await runner.run(flow)
            self._results.append(result)

        self._aps.add_job(_job, CronTrigger.from_crontab(cron))
        logger.info(f"已注册定时任务 [{flow.name}]  cron: {cron}")

    def start(self) -> None:
        """启动定时调度器（阻塞）"""
        if not hasattr(self, "_aps"):
            raise RuntimeError("请先调用 schedule_cron() 注册任务")
        self._aps.start()
        logger.info("定时调度器已启动")

    def stop(self) -> None:
        if hasattr(self, "_aps"):
            self._aps.shutdown()