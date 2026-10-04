"""FastAPI 应用入口。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import close_pool, ensure_schema, init_pool
from .errors import ApiError
from .routers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = init_pool()
    ensure_schema(app.state.pool)
    yield
    close_pool()


app = FastAPI(
    title="计量校准证书追溯服务",
    description=(
        "登记基准设备合格证书作为追溯起点，签发/撤销普通校准证书，"
        "校验引用有效性、防止成环与有效期重叠，撤销时级联失效下游证书；"
        "支持离线采集批次的整批原子签发与批次键幂等重放；"
        "支持对已签发证书的展示字段（设备名称/操作员/备注）登记更正声明，"
        "声明按登记顺序生效且不改写原证书；"
        "支持客户交付归档包：以归档包编号与非空证书编号列表冻结创建时刻的"
        "原始签发内容、追溯链与按更正声明合并的展示字段，编号幂等、可按编号查快照；"
        "支持按撤销批次追查已交付归档凭据的影响面（冻结链命中本批被撤销证书的"
        "归档包与包内目标证书，分页输出）。"
    ),
    version="1.4.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logging.getLogger("caltrace").exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "服务内部错误", "details": {}}},
    )


@app.get("/health", tags=["meta"])
def health(request: Request):
    with request.app.state.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "calibration-certificate-traceability",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "api_base": "/api/v1",
        "endpoints": [
            "POST /api/v1/reference-certificates",
            "POST /api/v1/certificates",
            "POST /api/v1/certificate-batches",
            "GET  /api/v1/certificate-batches/{batch_key}",
            "GET  /api/v1/certificates",
            "GET  /api/v1/certificates/{cert_no}",
            "GET  /api/v1/certificates/{cert_no}/chain",
            "GET  /api/v1/certificates/{cert_no}/downstream",
            "POST /api/v1/certificates/{cert_no}/revoke",
            "POST /api/v1/correction-declarations",
            "GET  /api/v1/correction-declarations/{declaration_no}",
            "GET  /api/v1/certificates/{cert_no}/corrections",
            "POST /api/v1/delivery-archives",
            "GET  /api/v1/delivery-archives/{archive_no}",
            "GET  /api/v1/devices/{device_id}/certificates",
            "GET  /api/v1/revocations",
            "GET  /api/v1/revocations/{batch_id}",
            "GET  /api/v1/revocations/{batch_id}/archive-impact",
        ],
    }


app.include_router(router)
