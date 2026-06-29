"""
data/db_client.py
------------------
通过数据库连接获取业务数据。

支持：
  - PostgreSQL（asyncpg，可选安装）
  - MySQL（aiomysql，可选安装）
  - SQLite（aiosqlite，可选安装；零配置，适合本地测试）

设计原则：
  - 驱动按需导入，未安装时抛出清晰的 ImportError 提示
  - 统一返回 list[dict]，调用方无需关心数据库类型
  - 连接池生命周期由 async context manager 管理
  - 查询参数化，防止 SQL 注入
"""
from __future__ import annotations

from typing import Any

from loguru import logger


# ── 统一的行转换工具 ─────────────────────────

def _rows_to_dicts(rows: list, keys: list[str]) -> list[dict[str, Any]]:
    """将数据库行列表转换为 dict 列表"""
    return [dict(zip(keys, row)) for row in rows]


# ─────────────────────────────────────────────
# PostgreSQL 客户端
# ─────────────────────────────────────────────

class PostgresClient:
    """
    asyncpg 封装的 PostgreSQL 客户端。
    需要：pip install asyncpg

    用法：
        async with PostgresClient(dsn="postgresql://user:pw@host/db") as db:
            rows = await db.query("SELECT * FROM orders WHERE status=$1", "pending")
    """

    def __init__(
        self,
        dsn: str = "",
        host: str = "localhost",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        min_size: int = 1,
        max_size: int = 5,
    ):
        self._dsn = dsn or None
        self._conn_kwargs: dict[str, Any] = dict(
            host=host, port=port, database=database,
            user=user, password=password,
            min_size=min_size, max_size=max_size,
        )
        self._pool = None

    async def __aenter__(self) -> PostgresClient:
        try:
            import asyncpg
        except ImportError:
            raise ImportError("PostgreSQL 支持需要安装: pip install asyncpg")

        logger.debug("[db:pg] 创建连接池")
        if self._dsn:
            self._pool = await asyncpg.create_pool(self._dsn, **{
                k: v for k, v in self._conn_kwargs.items()
                if k in ("min_size", "max_size")
            })
        else:
            self._pool = await asyncpg.create_pool(**self._conn_kwargs)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._pool:
            await self._pool.close()
            logger.debug("[db:pg] 连接池已关闭")

    async def query(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """
        执行 SELECT 查询，返回 list[dict]。

        Parameters
        ----------
        sql : str
            参数化 SQL，占位符用 $1 $2 …（PostgreSQL 风格）
        *args :
            对应 $1 $2 … 的参数值
        """
        assert self._pool, "请在 async with 块内使用"
        logger.debug(f"[db:pg] {sql[:80]} args={args}")
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *args)
        return [dict(r) for r in records]

    async def execute(self, sql: str, *args: Any) -> str:
        """执行 INSERT / UPDATE / DELETE，返回影响行数描述字符串。"""
        assert self._pool, "请在 async with 块内使用"
        logger.debug(f"[db:pg] execute {sql[:80]}")
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """查询单行，不存在时返回 None。"""
        rows = await self.query(sql, *args)
        return rows[0] if rows else None


# ─────────────────────────────────────────────
# MySQL 客户端
# ─────────────────────────────────────────────

class MySQLClient:
    """
    aiomysql 封装的 MySQL 客户端。
    需要：pip install aiomysql

    用法：
        async with MySQLClient(host="localhost", db="business", user="root", password="pw") as db:
            rows = await db.query("SELECT * FROM orders WHERE status=%s", "pending")
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 3306,
        db: str = "",
        user: str = "",
        password: str = "",
        minsize: int = 1,
        maxsize: int = 5,
        charset: str = "utf8mb4",
    ):
        self._pool_kwargs = dict(
            host=host, port=port, db=db,
            user=user, password=password,
            minsize=minsize, maxsize=maxsize,
            charset=charset, autocommit=True,
        )
        self._pool = None

    async def __aenter__(self) -> MySQLClient:
        try:
            import aiomysql
        except ImportError:
            raise ImportError("MySQL 支持需要安装: pip install aiomysql")

        logger.debug("[db:mysql] 创建连接池")
        self._pool = await __import__("aiomysql").create_pool(**self._pool_kwargs)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            logger.debug("[db:mysql] 连接池已关闭")

    async def query(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """执行 SELECT，返回 list[dict]。占位符用 %s。"""
        assert self._pool, "请在 async with 块内使用"
        logger.debug(f"[db:mysql] {sql[:80]} args={args}")
        async with self._pool.acquire() as conn:
            async with conn.cursor(__import__("aiomysql").DictCursor) as cur:
                await cur.execute(sql, args)
                return list(await cur.fetchall())

    async def execute(self, sql: str, *args: Any) -> int:
        """执行 INSERT / UPDATE / DELETE，返回受影响行数。"""
        assert self._pool, "请在 async with 块内使用"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                return cur.rowcount

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.query(sql, *args)
        return rows[0] if rows else None


# ─────────────────────────────────────────────
# SQLite 客户端（零依赖，适合本地测试）
# ─────────────────────────────────────────────

class SQLiteClient:
    """
    aiosqlite 封装的 SQLite 客户端，适合本地测试和小型数据集。
    需要：pip install aiosqlite

    用法：
        async with SQLiteClient("local.db") as db:
            rows = await db.query("SELECT * FROM orders WHERE status=?", "pending")
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = None

    async def __aenter__(self) -> SQLiteClient:
        try:
            import aiosqlite
        except ImportError:
            raise ImportError("SQLite 异步支持需要安装: pip install aiosqlite")

        logger.debug(f"[db:sqlite] 连接 {self._db_path}")
        self._conn = await __import__("aiosqlite").connect(self._db_path)
        self._conn.row_factory = __import__("aiosqlite").Row
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._conn:
            await self._conn.close()

    async def query(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """执行 SELECT，返回 list[dict]。占位符用 ?。"""
        assert self._conn, "请在 async with 块内使用"
        logger.debug(f"[db:sqlite] {sql[:80]}")
        async with self._conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def execute(self, sql: str, *args: Any) -> int:
        """执行写操作，返回受影响行数。"""
        assert self._conn, "请在 async with 块内使用"
        async with self._conn.execute(sql, args) as cur:
            await self._conn.commit()
            return cur.rowcount

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.query(sql, *args)
        return rows[0] if rows else None

    async def executescript(self, script: str) -> None:
        """执行多条 SQL（建表、初始化用）。"""
        assert self._conn
        await self._conn.executescript(script)
        await self._conn.commit()
