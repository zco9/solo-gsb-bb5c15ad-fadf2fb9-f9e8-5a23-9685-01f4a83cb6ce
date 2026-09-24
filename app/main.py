"""FastAPI 入口：创建分析、查询报告。"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import compat, compare, coverage, gate, mutation, profile, redact, reduction, refgraph, replay, sarif
from app.database import get_db, init_db
from app.models import Analysis, AnalysisProfile
from app.schemas import (
    AnalysisCreate,
    AnalysisListResponse,
    AnalysisResponse,
    CompareRequest,
    CompareResponse,
    CounterexampleReductionRequest,
    CounterexampleReductionResponse,
    GateRequest,
    GateResponse,
    MutationCoverageRequest,
    MutationCoverageResponse,
    ProfileCreate,
    ProfileListResponse,
    ProfileResponse,
    RedactRequest,
    RedactResponse,
    ReferenceDiagnosticsRequest,
    ReferenceDiagnosticsResponse,
    ReplayRequest,
    ReplayResponse,
    ReplayCoverageRequest,
    ReplayCoverageResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="JSON Schema 兼容性分析 API",
    description=(
        "递归比较旧版 / 新版 JSON Schema 的类型、必填字段、枚举、数组与组合关键字，"
        "分别判断向后 / 向前兼容性，并生成最小反例路径与原因。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 引用依赖诊断（单份 schema 的本地 $ref 分析；只读，不创建分析记录）
# ---------------------------------------------------------------------------


@app.post(
    "/api/reference-diagnostics",
    response_model=ReferenceDiagnosticsResponse,
    tags=["reference-diagnostics"],
    summary="单份 JSON Schema 的 $ref 引用依赖诊断",
)
def diagnose_references(payload: ReferenceDiagnosticsRequest):
    # 只遍历 schema 内部的本地片段引用，不访问网络或外部文件，也不写库
    schema = payload.schema_
    try:
        compat.make_validator(schema)
    except SchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"field": "schema", "message": f"非法 JSON Schema: {exc.message}"},
        )
    return refgraph.analyze_references(schema)


@app.post(
    "/api/analyses",
    response_model=AnalysisResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["analyses"],
    summary="创建兼容性分析",
)
def create_analysis(payload: AnalysisCreate, db: Session = Depends(get_db)):
    # 校验两个 schema 本身是合法的 JSON Schema
    for label, schema in (("old_schema", payload.old_schema), ("new_schema", payload.new_schema)):
        try:
            compat.make_validator(schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"field": label, "message": f"非法 JSON Schema: {exc.message}"},
            )

    report = compat.analyze(payload.old_schema, payload.new_schema, payload.samples)

    record = Analysis(
        name=payload.name,
        old_schema=payload.old_schema,
        new_schema=payload.new_schema,
        samples=payload.samples or [],
        report=report,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.to_dict()


@app.get(
    "/api/analyses",
    response_model=AnalysisListResponse,
    tags=["analyses"],
    summary="列出全部分析（摘要）",
)
def list_analyses(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    rows = db.scalars(
        select(Analysis).order_by(Analysis.id.desc()).limit(limit).offset(offset)
    ).all()
    items = [
        {
            "id": row.id,
            "name": row.name,
            "created_at": row.created_at.isoformat(),
            "backward_compatible": row.report["backward_compatible"],
            "forward_compatible": row.report["forward_compatible"],
            "errors": row.report["summary"]["errors"],
            "warnings": row.report["summary"]["warnings"],
        }
        for row in rows
    ]
    return {"total": len(items), "items": items}


@app.post(
    "/api/analyses/compare",
    response_model=CompareResponse,
    tags=["analyses"],
    summary="对比两次分析报告（基线 vs 当前）",
)
def compare_analyses(payload: CompareRequest, db: Session = Depends(get_db)):
    # 只读取两份报告做对比，绝不修改原始分析记录
    baseline = db.get(Analysis, payload.baseline_id)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"基线分析记录不存在: {payload.baseline_id}",
        )
    current = db.get(Analysis, payload.current_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"当前分析记录不存在: {payload.current_id}",
        )
    result = compare.compare_reports(baseline.report, current.report)
    return {"baseline_id": baseline.id, "current_id": current.id, **result}


@app.post(
    "/api/analyses/{analysis_id}/gate",
    response_model=GateResponse,
    tags=["analyses"],
    summary="发布门禁：按方向阈值与豁免规则评估报告",
)
def evaluation_gate(analysis_id: int, payload: GateRequest, db: Session = Depends(get_db)):
    # 只读评估，绝不修改原始分析报告
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    result = gate.evaluate_gate(
        row.report,
        {
            "directions": payload.directions,
            "max_warnings": (
                {k: v for k, v in payload.max_warnings.items()}
                if payload.max_warnings is not None
                else {}
            ),
            "exemptions": [rule.model_dump() for rule in payload.exemptions],
        },
    )
    return {"passed": result["pass"], "counts": result["counts"],
            "blocking": result["blocking"], "exempted": result["exempted"]}


@app.post(
    "/api/analyses/{analysis_id}/replay",
    response_model=ReplayResponse,
    tags=["analyses"],
    summary="样本回放诊断：用旧/新 schema 校验一批文档并归因到报告 issue",
)
def replay_analysis(analysis_id: int, payload: ReplayRequest, db: Session = Depends(get_db)):
    # 纯只读：只用已存的两个 schema 与报告做回放，绝不修改分析或画像历史
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    result = replay.replay_documents(
        row.old_schema, row.new_schema, row.report, payload.documents
    )
    return {"analysis_id": analysis_id, **result}


@app.post(
    "/api/analyses/{analysis_id}/replay-coverage",
    response_model=ReplayCoverageResponse,
    tags=["analyses"],
    summary="回放样本最小覆盖：贪心选择覆盖报告 issue 的最少代表文档",
)
def replay_coverage(analysis_id: int, payload: ReplayCoverageRequest,
                    db: Session = Depends(get_db)):
    # 纯只读：复用旧/新 schema 校验与 issue 归因，只做集合覆盖选择，绝不修改分析
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    result = coverage.select_minimum_coverage(
        row.old_schema, row.new_schema, row.report,
        payload.documents, payload.directions,
    )
    return {"analysis_id": analysis_id, **result}


@app.post(
    "/api/analyses/{analysis_id}/mutation-coverage",
    response_model=MutationCoverageResponse,
    tags=["analyses"],
    summary="样本变异覆盖评估：对新 schema 生成单点变异并统计样本发现率",
)
def mutation_coverage(
    analysis_id: int, payload: MutationCoverageRequest, db: Session = Depends(get_db)
):
    # 纯只读：只基于已保存的新 schema 生成变异并回放提交文档，绝不修改分析记录
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    result = mutation.evaluate_mutation_coverage(
        row.new_schema,
        payload.documents,
        {k: v for k, v in payload.max_per_rule.items()}
        if payload.max_per_rule is not None
        else None,
    )
    return {"analysis_id": analysis_id, **result}


@app.post(
    "/api/analyses/{analysis_id}/counterexample-reduction",
    response_model=CounterexampleReductionResponse,
    tags=["analyses"],
    summary="兼容性反例缩减：保持单向破坏方向的前提下贪心删除式缩减文档",
    responses={
        422: {"description": "请求体不合法，或文档不属于指定方向的单向破坏类别"},
    },
)
def reduce_counterexample_document(
    analysis_id: int, payload: CounterexampleReductionRequest,
    db: Session = Depends(get_db),
):
    # 纯只读：只用已存的旧 / 新两版 schema 校验并缩减提交文档的深拷贝，
    # 绝不修改分析记录、报告、样本或画像历史
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    try:
        result = reduction.reduce_counterexample(
            row.old_schema, row.new_schema, payload.document,
            payload.direction, payload.max_validations,
        )
    except reduction.NonBreakingDocument as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "requested_direction": exc.direction,
                "valid_against_old": exc.old_valid,
                "valid_against_new": exc.new_valid,
                "reason": exc.reason,
                **(
                    {"actual_direction": exc.actual_direction}
                    if exc.actual_direction is not None
                    else {}
                ),
            },
        )
    return {"analysis_id": analysis_id, **result}


@app.post(
    "/api/analyses/{analysis_id}/redact",
    response_model=RedactResponse,
    tags=["analyses"],
    summary="按新 schema 的 x-sensitive 标记脱敏文档实例值",
    responses={
        422: {"description": "请求体不合法，或 schema 中存在无法解析的 $ref"},
    },
)
def redact_analysis_documents(
    analysis_id: int, payload: RedactRequest, db: Session = Depends(get_db)
):
    # 纯只读：只基于已保存的新 schema 脱敏提交文档的深拷贝，
    # 绝不修改原文、分析记录或库中样本
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    try:
        result = redact.redact_documents(row.new_schema, payload.documents)
    except redact.UnresolvableReference as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": f"无法解析的 $ref: {exc.message}",
                "ref": exc.ref,
                "reason": exc.reason,
                "schema_pointer": exc.schema_pointer,
                "instance_pointer": exc.instance_pointer,
            },
        )
    return {"analysis_id": analysis_id,
            "total_documents": len(payload.documents), **result}


@app.get(
    "/api/analyses/{analysis_id}",
    response_model=AnalysisResponse,
    tags=["analyses"],
    summary="获取单个分析的完整报告",
)
def get_analysis(analysis_id: int, db: Session = Depends(get_db)):
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    return row.to_dict()


@app.get(
    "/api/analyses/{analysis_id}/sarif",
    tags=["analyses"],
    summary="导出 SARIF 2.1.0 分析报告",
    responses={
        200: {
            "content": {"application/sarif+json": {}},
            "description": "SARIF v2.1.0 文档",
        },
        404: {"description": "分析记录不存在"},
    },
)
def export_analysis_sarif(analysis_id: int, db: Session = Depends(get_db)):
    # 只读取已存报告做格式转换，绝不修改原始分析记录
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    document = sarif.build_sarif(row.to_dict())
    return JSONResponse(content=document, media_type="application/sarif+json")


# ---------------------------------------------------------------------------
# 合规画像（profiling）
# ---------------------------------------------------------------------------


@app.post(
    "/api/analyses/{analysis_id}/profile",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["analyses"],
    summary="对分析的新 schema 执行批量文档合规画像",
)
def create_profile(analysis_id: int, payload: ProfileCreate, db: Session = Depends(get_db)):
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")

    snapshot = profile.profile_documents(row.new_schema, payload.documents)

    # version 在该分析下递增；并发画像各自取号，唯一约束兜底
    latest = db.scalars(
        select(AnalysisProfile.version)
        .where(AnalysisProfile.analysis_id == analysis_id)
        .order_by(AnalysisProfile.version.desc())
        .limit(1)
    ).first()
    record = AnalysisProfile(
        analysis_id=analysis_id,
        version=(latest or 0) + 1,
        snapshot=snapshot,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.to_dict()


@app.get(
    "/api/analyses/{analysis_id}/profile",
    response_model=ProfileResponse,
    tags=["analyses"],
    summary="获取分析的最新合规画像快照",
)
def get_latest_profile(analysis_id: int, db: Session = Depends(get_db)):
    if db.get(Analysis, analysis_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    record = db.scalars(
        select(AnalysisProfile)
        .where(AnalysisProfile.analysis_id == analysis_id)
        .order_by(AnalysisProfile.version.desc())
        .limit(1)
    ).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="画像快照不存在")
    return record.to_dict()


@app.get(
    "/api/analyses/{analysis_id}/profiles",
    response_model=ProfileListResponse,
    tags=["analyses"],
    summary="列出分析的全部画像快照（摘要）",
)
def list_profiles(analysis_id: int, db: Session = Depends(get_db)):
    if db.get(Analysis, analysis_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    rows = db.scalars(
        select(AnalysisProfile)
        .where(AnalysisProfile.analysis_id == analysis_id)
        .order_by(AnalysisProfile.version.desc())
    ).all()
    items = [
        {
            "id": row.id,
            "version": row.version,
            "created_at": row.created_at.isoformat(),
            "total_documents": row.snapshot["total_documents"],
            "valid_documents": row.snapshot["valid_documents"],
            "invalid_documents": row.snapshot["invalid_documents"],
            "compliance_rate": row.snapshot["compliance_rate"],
            "failure_groups": len(row.snapshot.get("failures", [])),
        }
        for row in rows
    ]
    return {"analysis_id": analysis_id, "total": len(items), "items": items}


@app.get(
    "/api/analyses/{analysis_id}/profiles/{profile_id}",
    response_model=ProfileResponse,
    tags=["analyses"],
    summary="按画像 ID 获取指定画像快照",
)
def get_profile(analysis_id: int, profile_id: int, db: Session = Depends(get_db)):
    if db.get(Analysis, analysis_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    record = db.get(AnalysisProfile, profile_id)
    if record is None or record.analysis_id != analysis_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="画像快照不存在")
    return record.to_dict()
