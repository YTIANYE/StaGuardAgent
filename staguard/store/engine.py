"""数据库引擎与建表。

`init_schema()` 是幂等的（`checkfirst=True`），所以它会自动挂在应用启动与 CLI 入口上，
不需要单独的迁移步骤——对一个单机可跑的项目来说，这比引入 Alembic 更划算；
真要上生产，这里就是接 Alembic 的切口。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection

from .schema import metadata

logger = logging.getLogger(__name__)

_SQLITE_PRAGMAS = (
    # WAL 让「生成数据」与「巡检读取」可以并发进行
    "PRAGMA journal_mode=WAL",
    # NORMAL 在 WAL 下已足够安全，写入速度相比 FULL 有量级提升（本机实测约 3 倍）
    "PRAGMA synchronous=NORMAL",
    # 批量灌数据时把临时表放在内存里，内存映射默认 64MB 偏小
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-65536",
)


def create_db_engine(db_url: str, echo: bool = False) -> Engine:
    kwargs: dict = {"echo": echo, "future": True, "pool_pre_ping": True}
    if db_url.startswith("sqlite"):
        # FastAPI 的线程池与 APScheduler 的线程会共用同一个 engine
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(db_url, **kwargs)

    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            for pragma in _SQLITE_PRAGMAS:
                cursor.execute(pragma)
            cursor.close()

    return engine


class Database:
    """薄封装：持有 engine，负责建表与事务边界。"""

    def __init__(self, db_url: str, echo: bool = False) -> None:
        self.url = db_url
        self.engine = create_db_engine(db_url, echo=echo)

    def init_schema(self) -> None:
        """建表并**自愈式对齐**已存在的表结构。

        `create_all(checkfirst=True)` 只保证「表存在」，不会补列——
        改了模型却没重建库，就会在写入时炸出 `table xxx has no column named yyy`，
        而且是在运行时才暴露。

        这里额外做一次结构比对：发现列不一致就**重建该表**。
        之所以敢直接重建，是因为本项目所有数据都能由 `gen-data` 从固定种子复现，
        重建的代价是十几秒，比维护一套迁移脚本划算得多。

        真要上生产，这里就是接 Alembic 的切口——把 `_recreate` 换成迁移版本检查即可。
        """
        for table in self._drifted_tables():
            logger.warning(
                "检测到表 %s 结构与模型不一致（数据可由 gen-data 复现），将重建该表", table.name
            )
            table.drop(self.engine, checkfirst=True)
        metadata.create_all(self.engine, checkfirst=True)

    def _drifted_tables(self) -> list:
        inspector = sa.inspect(self.engine)
        existing = set(inspector.get_table_names())
        drifted = []
        for table in metadata.sorted_tables:
            if table.name not in existing:
                continue
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            expected = {column.name for column in table.columns}
            if actual != expected:
                drifted.append(table)
        return drifted

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    def ping(self) -> bool:
        """健康检查用。"""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 健康检查不该因为异常类型而失败
            logger.exception("数据库连通性检查失败")
            return False

    def drop_all(self) -> None:
        """仅用于测试与重建数据集。"""
        metadata.drop_all(self.engine, checkfirst=True)

    def location(self) -> str:
        if self.url.startswith("sqlite"):
            raw = self.url.split("///", 1)[-1]
            return str(Path(raw).resolve())
        return self.url
