"""Pydantic 请求与响应模型。

时间一律使用带时区的 ISO 8601（如 2026-03-01T09:00:00+08:00），
不传时区时按 UTC 处理。
有效期统一按半开区间 [valid_from, valid_to) 解释，首尾相接不算重叠。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CertKind = Literal["REFERENCE", "CALIBRATION"]
CertStatus = Literal["ACTIVE", "REVOKED", "INVALID"]
CertConclusion = Literal["PASS", "FAIL"]


class _IssueBase(BaseModel):
    cert_no: str = Field(..., min_length=1, max_length=128, description="证书编号，全局唯一")
    device_id: str = Field(..., min_length=1, max_length=128, description="被校/基准设备标识")
    device_name: str | None = Field(None, max_length=256, description="设备名称")
    calibrated_at: datetime | None = Field(
        None, description="校准时刻；缺省取 valid_from"
    )
    valid_from: datetime = Field(..., description="有效期开始")
    valid_to: datetime = Field(..., description="有效期结束（不含该时刻）")
    operator: str | None = Field(None, max_length=128)
    remark: str | None = Field(None, max_length=1024)

    @model_validator(mode="after")
    def _check_window(self):
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from 必须早于 valid_to")
        if self.calibrated_at is None:
            self.calibrated_at = self.valid_from
        if not (self.valid_from <= self.calibrated_at < self.valid_to):
            raise ValueError("calibrated_at 必须落在 [valid_from, valid_to) 内")
        return self


class ReferenceIssueRequest(_IssueBase):
    """登记基准（标准）设备合格证书——追溯起点，结论恒为合格。"""


class CertificateIssueRequest(_IssueBase):
    """签发普通校准证书。"""
    conclusion: CertConclusion = Field(..., description="校准结论 PASS/FAIL")
    parent_cert_no: str = Field(..., min_length=1, description="引用的标准设备证书编号")


class RevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1024, description="撤销原因")
    operator: str | None = Field(None, max_length=128)


class CorrectionDeclarationRequest(BaseModel):
    """登记更正声明：仅可更正 device_name / operator / remark 三个展示字段。

    corrections 为本次提交的修正字段集合（非空）：字段值为 null 表示显式置空，
    未提交的字段保持原值——两者严格区分。declaration_no 为幂等键：同编号同内容
    重试返回首次声明，同编号不同内容报冲突。
    """
    declaration_no: str = Field(
        ..., min_length=1, max_length=128,
        description="声明编号（全局唯一，幂等键）",
    )
    cert_no: str = Field(..., min_length=1, max_length=128, description="被更正的证书编号")
    corrections: dict[str, str | None] = Field(
        ...,
        description="修正字段集合（非空）：仅 device_name/operator/remark；"
        "值为 null 表示显式置空，未提交的字段保持不变",
    )
    reason: str = Field(..., max_length=1024, description="更正原因（非空）")


class BatchIssueItem(_IssueBase):
    """批次中的单张证书，沿用单张签发的字段与规则。

    kind=REFERENCE   基准设备合格证书（追溯起点）：不得填写 parent_cert_no，
                     结论恒为 PASS（conclusion 可省略或显式传 "PASS"）。
    kind=CALIBRATION 普通校准证书：parent_cert_no 与 conclusion 必填；
                     parent_cert_no 可引用库内已有证书或本批证书。
    """
    kind: CertKind = Field(..., description="REFERENCE=基准证书；CALIBRATION=普通校准证书")
    conclusion: CertConclusion | None = Field(
        None, description="校准结论；基准证书恒为 PASS，普通证书必填"
    )
    parent_cert_no: str | None = Field(
        None, description="引用的上游证书编号（库内或本批）；普通证书必填"
    )

    @model_validator(mode="after")
    def _check_kind_fields(self):
        if self.kind == "REFERENCE":
            if self.parent_cert_no is not None:
                raise ValueError("基准证书(REFERENCE)不得填写 parent_cert_no")
            if self.conclusion not in (None, "PASS"):
                raise ValueError("基准证书结论恒为合格(PASS)，不得指定其他结论")
        else:
            if not self.parent_cert_no:
                raise ValueError("普通校准证书(CALIBRATION)必须填写 parent_cert_no")
            if self.conclusion is None:
                raise ValueError("普通校准证书(CALIBRATION)必须填写 conclusion")
        return self


class BatchIssueRequest(BaseModel):
    """离线采集交接批次：唯一批次键 + 非空证书列表（列表顺序不代表引用顺序）。"""
    batch_key: str = Field(
        ..., min_length=1, max_length=128,
        description="批次键（幂等键）：同键同请求重放首次签发结果，同键不同请求报冲突",
    )
    certificates: list[BatchIssueItem] = Field(
        ..., min_length=1, max_length=500,
        description="本批证书列表（非空）；列表顺序不代表引用顺序",
    )


class DeliveryArchiveRequest(BaseModel):
    """客户交付归档：归档包编号 + 非空目标证书编号列表（顺序即交付顺序）。

    仅接受编号互不重复、目标均为 CALIBRATION，且目标及各自完整上游链均
    ACTIVE、结论 PASS 的组合；任一证书不存在或不合格，整包不保存。
    archive_no 为幂等键：同编号同有序列表重试返回首次快照，同编号不同列表
    报冲突（列表顺序不同即视为不同列表）。
    """
    archive_no: str = Field(
        ..., min_length=1, max_length=128,
        description="归档包编号（幂等键）：同编号同列表重放首次快照，同编号不同列表报冲突",
    )
    cert_nos: list[str] = Field(
        ..., min_length=1, max_length=500,
        description="目标证书编号列表（非空，按交付顺序；编号必须互不重复）",
    )


class CertificateOut(BaseModel):
    cert_no: str
    kind: CertKind
    device_id: str
    device_name: str | None = None
    calibrated_at: datetime
    valid_from: datetime
    valid_to: datetime
    conclusion: CertConclusion
    parent_cert_no: str | None = None
    operator: str | None = None
    remark: str | None = None
    status: CertStatus
    revoked_reason: str | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class ChainNode(BaseModel):
    depth: int
    certificate: CertificateOut


class TraceChainOut(BaseModel):
    cert_no: str
    nodes: list[ChainNode] = Field(..., description="从追溯起点(基准)到目标证书的引用链")
    root_cert_no: str | None = None
    length: int


class IssueResponse(BaseModel):
    certificate: CertificateOut
    chain: TraceChainOut


class BatchIssueItemOut(BaseModel):
    certificate: CertificateOut
    chain: TraceChainOut


class BatchIssueResponse(BaseModel):
    batch_key: str
    count: int
    issued_at: datetime
    items: list[BatchIssueItemOut] = Field(
        ..., description="各证书及其从基准到自身的追溯链，按请求列表顺序返回"
    )


class InvalidatedItem(BaseModel):
    cert_no: str
    chain: list[CertificateOut]
    depth: int = Field(..., description="相对于被撤销证书的下游层数，1 为直接引用者")


class RevocationOut(BaseModel):
    batch_id: int
    revoked: CertificateOut
    reason: str
    operator: str | None = None
    revoked_at: datetime
    affected_count: int
    affected: list[InvalidatedItem]


class DownstreamItem(BaseModel):
    cert_no: str
    depth: int
    status: CertStatus


class CorrectionDeclarationOut(BaseModel):
    declaration_no: str
    cert_no: str
    seq: int = Field(..., description="该证书内按成功登记顺序分配的序号，从 1 开始")
    corrections: dict[str, str | None] = Field(
        ..., description="本次提交的修正字段集合；null 表示显式置空"
    )
    reason: str
    created_at: datetime


class CorrectionDisplay(BaseModel):
    """按登记顺序合并全部声明后的当前展示值（仅三个可更正字段）。"""
    device_name: str | None = None
    operator: str | None = None
    remark: str | None = None


class CertificateCorrectionsOut(BaseModel):
    """证书的更正视图：原证书 + 声明历史 + 合并后的当前展示。"""
    cert_no: str
    certificate: CertificateOut = Field(..., description="原证书（签发内容，不受更正影响）")
    declarations: list[CorrectionDeclarationOut] = Field(
        ..., description="声明历史，按登记顺序（seq）升序"
    )
    display: CorrectionDisplay = Field(..., description="合并后的当前展示")


class CorrectionDeclarationResponse(BaseModel):
    """登记结果 / 按声明编号查询：首次声明 + 所属证书的当前更正视图。"""
    declaration: CorrectionDeclarationOut
    certificate: CertificateOut = Field(..., description="原证书（签发内容，不受更正影响）")
    declarations: list[CorrectionDeclarationOut] = Field(
        ..., description="该证书的全部声明历史，按登记顺序（seq）升序"
    )
    display: CorrectionDisplay = Field(..., description="合并后的当前展示")


class DownstreamOut(BaseModel):
    cert_no: str
    descendants: list[DownstreamItem]


class PagedCertificates(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class DeliveryArchiveItemOut(BaseModel):
    """单张目标证书的交付凭据：原始签发内容 + 从基准到目标的链 + 合并展示字段。"""
    cert_no: str
    certificate: CertificateOut = Field(..., description="原始签发内容（不受更正影响）")
    chain: TraceChainOut = Field(..., description="从追溯起点(基准)到目标证书的引用链")
    display: CorrectionDisplay = Field(
        ..., description="按归档时刻已登记更正声明合并后的展示字段"
    )


class DeliveryArchiveResponse(BaseModel):
    archive_no: str
    count: int
    created_at: datetime = Field(..., description="归档包创建时刻（快照冻结时点）")
    items: list[DeliveryArchiveItemOut] = Field(
        ..., description="各目标证书的交付凭据，按提交列表顺序返回"
    )


class ErrorBody(BaseModel):
    error: dict
