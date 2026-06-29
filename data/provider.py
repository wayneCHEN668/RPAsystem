"""
data/provider.py
-----------------
统一的数据提供者，屏蔽 API / 数据库 / 文件的差异。

engine/runner.py 只与 DataProvider 交互，不直接调用底层客户端。
DataProvider 负责：
  1. 按来源类型路由到对应客户端
  2. 对返回的原始数据做统一的字段规范化
  3. 合并多来源数据（如 API 基础信息 + 数据库明细）

上游：engine/runner.py（通过 FlowDefinition.data_source 决定用哪个来源）
下游：data/api_client.py / data/db_client.py / data/file_reader.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from data.api_client import ApiClient
from data.db_client import PostgresClient, MySQLClient, SQLiteClient
from data.file_reader import read_file


# ─────────────────────────────────────────────
# 数据源配置
# ─────────────────────────────────────────────

@dataclass
class ApiSourceConfig:
    """REST API 数据源配置"""
    base_url: str
    endpoint: str
    params: dict[str, Any] = field(default_factory=dict)
    token: str = ""
    username: str = ""
    password: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    result_key: str = ""        # 若响应是 {"data": [...]}，填 "data"


@dataclass
class DbSourceConfig:
    """数据库数据源配置"""
    driver: str                 # "postgres" | "mysql" | "sqlite"
    sql: str                    # 查询 SQL
    params: tuple = ()          # SQL 参数（按序）
    # 连接参数
    host: str = "localhost"
    port: int = 5432
    database: str = ""
    user: str = ""
    password: str = ""
    dsn: str = ""               # 优先于单独字段（postgres）
    db_path: str = ":memory:"   # SQLite 路径


@dataclass
class FileSourceConfig:
    """文件数据源配置"""
    path: str
    sheet: str | int = 0       # Excel sheet
    col_map: dict[str, str] = field(default_factory=dict)
    usecols: list[str] = field(default_factory=list)
    root_key: str = ""         # JSON root key
    encoding: str = "utf-8-sig"
    delimiter: str = ","


# ─────────────────────────────────────────────
# 统一提供者
# ─────────────────────────────────────────────

class DataProvider:
    """
    统一数据提供者，engine 层通过它获取所有业务数据。

    用法（从 API 获取）：
        provider = DataProvider()
        cfg = ApiSourceConfig(
            base_url="https://api.sys.com",
            endpoint="orders",
            params={"status": "pending"},
            token="Bearer xxx",
        )
        records = await provider.from_api(cfg)

    用法（从文件获取）：
        cfg = FileSourceConfig(path="orders.xlsx", sheet="待处理")
        records = provider.from_file(cfg)

    用法（从数据库获取）：
        cfg = DbSourceConfig(
            driver="postgres",
            dsn="postgresql://user:pw@host/db",
            sql="SELECT * FROM orders WHERE status=$1",
            params=("pending",),
        )
        records = await provider.from_db(cfg)
    """

    # ── API 数据源 ────────────────────────────

    async def from_api(self, config: ApiSourceConfig) -> list[dict[str, Any]]:
        """从 REST API 获取数据，返回 list[dict]。"""
        logger.info(f"[provider] API: {config.base_url}/{config.endpoint}")
        async with ApiClient(
            base_url=config.base_url,
            token=config.token,
            username=config.username,
            password=config.password,
            headers=config.headers,
        ) as client:
            raw = await client.get(config.endpoint, params=config.params)

        return self._unwrap(raw, config.result_key)

    async def from_api_post(
        self, config: ApiSourceConfig, body: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """通过 POST 请求获取数据（部分 API 查询接口用 POST）。"""
        logger.info(f"[provider] API POST: {config.base_url}/{config.endpoint}")
        async with ApiClient(
            base_url=config.base_url,
            token=config.token,
            username=config.username,
            password=config.password,
            headers=config.headers,
        ) as client:
            raw = await client.post(config.endpoint, body=body)

        return self._unwrap(raw, config.result_key)

    # ── 数据库数据源 ──────────────────────────

    async def from_db(self, config: DbSourceConfig) -> list[dict[str, Any]]:
        """从数据库查询数据，返回 list[dict]。"""
        logger.info(f"[provider] DB({config.driver}): {config.sql[:60]}")

        match config.driver.lower():
            case "postgres" | "postgresql":
                async with PostgresClient(
                    dsn=config.dsn,
                    host=config.host, port=config.port,
                    database=config.database,
                    user=config.user, password=config.password,
                ) as db:
                    return await db.query(config.sql, *config.params)

            case "mysql":
                async with MySQLClient(
                    host=config.host, port=config.port,
                    db=config.database,
                    user=config.user, password=config.password,
                ) as db:
                    return await db.query(config.sql, *config.params)

            case "sqlite":
                async with SQLiteClient(config.db_path) as db:
                    return await db.query(config.sql, *config.params)

            case _:
                raise ValueError(
                    f"不支持的数据库驱动: {config.driver}，"
                    f"支持: postgres / mysql / sqlite"
                )

    # ── 文件数据源 ────────────────────────────

    def from_file(self, config: FileSourceConfig) -> list[dict[str, Any]]:
        """从文件读取数据（同步），返回 list[dict]。"""
        logger.info(f"[provider] File: {config.path}")
        suffix = Path(config.path).suffix.lower()

        kwargs: dict[str, Any] = {}
        if suffix in (".xlsx", ".xls"):
            kwargs = {
                "sheet": config.sheet,
                "col_map": config.col_map or None,
                "usecols": config.usecols or None,
            }
        elif suffix == ".csv":
            kwargs = {
                "col_map": config.col_map or None,
                "usecols": config.usecols or None,
                "encoding": config.encoding,
                "delimiter": config.delimiter,
            }
        elif suffix == ".json":
            kwargs = {"root_key": config.root_key}

        # 去掉 None 值，避免覆盖函数默认值
        kwargs = {k: v for k, v in kwargs.items() if v is not None and v != []}
        return read_file(config.path, **kwargs)

    # ── 多源合并 ──────────────────────────────

    async def merge(
        self,
        *sources: tuple[str, Any],
        join_key: str = "",
    ) -> list[dict[str, Any]]:
        """
        合并多个数据源，返回合并后的 list[dict]。

        Parameters
        ----------
        *sources : tuple[str, config]
            ("api", ApiSourceConfig(...)) 或 ("file", FileSourceConfig(...)) 等。
        join_key : str
            若指定，对多个来源的数据按该字段做 left join 合并；
            否则直接 extend（追加）。

        示例：
            records = await provider.merge(
                ("api",  ApiSourceConfig(base_url="...", endpoint="customers")),
                ("file", FileSourceConfig(path="heat_load.xlsx")),
                join_key="customer_id",
            )
        """
        all_records: list[list[dict]] = []

        for source_type, config in sources:
            match source_type:
                case "api":
                    all_records.append(await self.from_api(config))
                case "db":
                    all_records.append(await self.from_db(config))
                case "file":
                    all_records.append(self.from_file(config))
                case _:
                    raise ValueError(f"未知数据源类型: {source_type}")

        if not all_records:
            return []

        if not join_key:
            # 简单追加
            merged: list[dict] = []
            for records in all_records:
                merged.extend(records)
            return merged

        # 按 join_key 做 left join（以第一个来源为主表）
        primary = {r[join_key]: r for r in all_records[0] if join_key in r}
        for records in all_records[1:]:
            lookup = {r[join_key]: r for r in records if join_key in r}
            for key, row in primary.items():
                if key in lookup:
                    row.update({
                        k: v for k, v in lookup[key].items()
                        if k != join_key   # 不重复 join_key
                    })

        result = list(primary.values())
        logger.info(f"[provider] merge 完成: {len(result)} 条（join_key={join_key}）")
        return result

    # ── 工具 ──────────────────────────────────

    @staticmethod
    def _unwrap(raw: Any, result_key: str) -> list[dict[str, Any]]:
        """
        从 API 响应中提取列表数据。
        处理以下常见格式：
          - 直接是 list：[{...}, {...}]
          - 包装在 key 下：{"data": [...], "total": 100}
          - 单个 dict：{...} → 包装为 [{...}]
        """
        if result_key and isinstance(raw, dict):
            if result_key not in raw:
                raise KeyError(f"API 响应中不存在 key '{result_key}'，实际 keys: {list(raw.keys())}")
            raw = raw[result_key]

        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return [raw]

        raise TypeError(f"无法将 API 响应转换为 list[dict]，类型: {type(raw).__name__}")
