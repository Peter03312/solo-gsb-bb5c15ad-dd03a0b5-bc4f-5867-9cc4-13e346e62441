"""HTTP API 路由。

约定：
- POST   /api/v1/reference-certificates        登记基准设备合格证书（追溯起点）
- POST   /api/v1/certificates                  签发普通校准证书
- POST   /api/v1/certificate-batches           批量签发交接批次（整批原子、批次键幂等）
- GET    /api/v1/certificate-batches/{batch_key}  查询签发批次（首次签发结果快照）
- GET    /api/v1/certificates                  证书列表（可按设备/状态/类型过滤）
- GET    /api/v1/certificates/{cert_no}        证书详情（历史内容始终可查）
- GET    /api/v1/certificates/{cert_no}/chain  完整追溯引用链
- GET    /api/v1/certificates/{cert_no}/downstream  全部下游证书（影响面预览）
- POST   /api/v1/certificates/{cert_no}/revoke 撤销证书并级联失效下游
- POST   /api/v1/correction-declarations       登记更正声明（展示字段纠错，声明编号幂等）
- GET    /api/v1/correction-declarations/{declaration_no}  按声明编号查询
- GET    /api/v1/certificates/{cert_no}/corrections  证书更正视图（声明历史+合并展示）
- POST   /api/v1/delivery-archives             创建客户交付归档包（冻结可交付凭据，编号幂等）
- GET    /api/v1/delivery-archives/{archive_no}    按归档包编号查询创建时刻快照
- GET    /api/v1/devices/{device_id}/certificates  某设备全部证书（含历史）
- GET    /api/v1/revocations                   撤销批次列表
- GET    /api/v1/revocations/{batch_id}        撤销批次详情（受影响证书及引用链）
- GET    /api/v1/revocations/{batch_id}/archive-impact  该批撤销对已交付归档凭据的影响面
"""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from psycopg_pool import ConnectionPool

from . import repository as repo
from .schemas import (
    BatchIssueRequest,
    CertificateIssueRequest,
    CorrectionDeclarationRequest,
    DeliveryArchiveRequest,
    ReferenceIssueRequest,
    RevokeRequest,
)

router = APIRouter(prefix="/api/v1")


def pool_dep(request: Request) -> ConnectionPool:
    return request.app.state.pool


@router.post("/reference-certificates", status_code=201)
def issue_reference(req: ReferenceIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_reference(conn, req)


@router.post("/certificates", status_code=201)
def issue_certificate(req: CertificateIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_certificate(conn, req)


@router.post("/certificate-batches", status_code=201)
def issue_certificate_batch(
    req: BatchIssueRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """批量签发交接批次：整批原子落库；批次键幂等（重放返回 200 与首次相同的结果）。"""
    with pool.connection() as conn:
        body, created = repo.issue_batch(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/certificate-batches/{batch_key}")
def get_certificate_batch(batch_key: str, pool: ConnectionPool = Depends(pool_dep)):
    """按批次键查询首次签发结果（快照，不受事后撤销影响）。"""
    with pool.connection() as conn:
        return repo.get_issue_batch(conn, batch_key)


@router.get("/certificates")
def list_certificates(
    device_id: str | None = None,
    status: Literal["ACTIVE", "REVOKED", "INVALID"] | None = None,
    kind: Literal["REFERENCE", "CALIBRATION"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_certificates(conn, device_id, status, kind, limit, offset)


@router.get("/certificates/{cert_no}")
def get_certificate(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_certificate(conn, cert_no)


@router.get("/certificates/{cert_no}/chain")
def get_chain(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_chain(conn, cert_no)


@router.get("/certificates/{cert_no}/downstream")
def get_downstream(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_downstream(conn, cert_no)


@router.post("/certificates/{cert_no}/revoke", status_code=200)
def revoke_certificate(
    cert_no: str, req: RevokeRequest, pool: ConnectionPool = Depends(pool_dep)
):
    with pool.connection() as conn:
        return repo.revoke_certificate(conn, cert_no, req.reason, req.operator)


@router.post("/correction-declarations", status_code=201)
def register_correction(
    req: CorrectionDeclarationRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """登记更正声明：仅更正展示字段；声明编号幂等（同内容重放返回 200 与首次声明）。"""
    with pool.connection() as conn:
        body, created = repo.register_correction(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/correction-declarations/{declaration_no}")
def get_correction_declaration(declaration_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """按声明编号查询：首次登记的声明 + 所属证书的当前更正视图。"""
    with pool.connection() as conn:
        return repo.get_correction_declaration(conn, declaration_no)


@router.get("/certificates/{cert_no}/corrections")
def get_certificate_corrections(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """证书的更正视图：原证书、声明历史（按登记顺序）与合并后的当前展示。"""
    with pool.connection() as conn:
        return repo.get_certificate_corrections(conn, cert_no)


@router.post("/delivery-archives", status_code=201)
def create_delivery_archive(
    req: DeliveryArchiveRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """创建客户交付归档包：单事务冻结创建时刻的原始签发内容、追溯链与合并展示。

    编号+有序列表均相同的重试返回首次快照（200）；同编号不同列表 409。
    """
    with pool.connection() as conn:
        body, created = repo.create_delivery_archive(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/delivery-archives/{archive_no}")
def get_delivery_archive(archive_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """按归档包编号查询创建时刻快照；未知编号 404 archive_not_found。"""
    with pool.connection() as conn:
        return repo.get_delivery_archive(conn, archive_no)


@router.get("/devices/{device_id}/certificates")
def device_history(device_id: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_device_history(conn, device_id)


@router.get("/revocations")
def list_revocations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_revocations(conn, limit, offset)


@router.get("/revocations/{batch_id}")
def get_revocation(batch_id: int, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_revocation(conn, batch_id)


@router.get("/revocations/{batch_id}/archive-impact")
def get_revocation_archive_impact(
    batch_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    """按撤销批次追查已交付归档凭据的影响面：冻结追溯链命中本批被撤销证书
    （含级联失效）的归档包及包内目标证书，按 (归档包编号, 原交付序位) 分页。"""
    with pool.connection() as conn:
        return repo.get_revocation_archive_impact(conn, batch_id, limit, offset)
