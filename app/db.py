"""PostgreSQL 连接池管理（psycopg3）。"""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger("caltrace")

_pool: ConnectionPool | None = None

# 幂等结构补丁：为沿用旧数据卷的部署补充后续版本新增的表。
# 与 db/init.sql 中同名对象定义保持一致（全新数据卷由 init.sql 建表，此处为 no-op；
# 触发器用 CREATE OR REPLACE 保证重复执行幂等，要求 PostgreSQL 14+）。
_MIGRATION_SQL = (
    """
CREATE TABLE IF NOT EXISTS issue_batches (
    batch_key    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    response     JSONB NOT NULL,
    cert_nos     TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    """
CREATE TABLE IF NOT EXISTS delivery_archives (
    archive_no   TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    response     JSONB NOT NULL,
    cert_nos     TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    """
CREATE TABLE IF NOT EXISTS correction_declarations (
    declaration_no TEXT PRIMARY KEY,
    cert_no        TEXT NOT NULL REFERENCES certificates(cert_no),
    seq            INT NOT NULL,
    corrections    JSONB NOT NULL,
    reason         TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cert_no, seq)
);
""",
    """
CREATE OR REPLACE FUNCTION append_only_sealed() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only and sealed (trigger=%)', TG_TABLE_NAME, TG_NAME
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
""",
    """
CREATE OR REPLACE TRIGGER trg_corrections_no_update
    BEFORE UPDATE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_corrections_no_delete
    BEFORE DELETE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_archives_no_update
    BEFORE UPDATE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_archives_no_delete
    BEFORE DELETE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
)


def _build_dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} "
        f"dbname={settings.db_name} user={settings.db_user} "
        f"password={settings.db_password}"
    )


def init_pool() -> ConnectionPool:
    """创建连接池，并在数据库尚未就绪时重试等待。"""
    global _pool
    if _pool is not None:
        return _pool

    deadline = time.monotonic() + settings.db_connect_timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                _build_dsn(),
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"row_factory": dict_row},
                open=True,
                timeout=10,
            )
            # 确认连接真正可用
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pool = pool
            logger.info("database connection pool ready")
            return pool
        except Exception as exc:  # 数据库容器可能仍在初始化
            last_err = exc
            logger.warning("waiting for database: %s", exc)
            time.sleep(2)
    raise RuntimeError(f"database not ready after {settings.db_connect_timeout}s: {last_err}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema(pool: ConnectionPool) -> None:
    """启动时执行幂等结构补丁（不改动既有表与数据）。"""
    with pool.connection() as conn:
        with conn.transaction():
            for stmt in _MIGRATION_SQL:
                conn.execute(stmt)
    logger.info("schema migration ensured")


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized")
    return _pool
