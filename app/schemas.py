"""Pydantic 请求 / 响应模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.coverage import MAX_COVERAGE_DOCUMENTS
from app.mutation import MAX_MUTATION_DOCUMENTS, MAX_RULE_LIMIT, RULE_NAMES
from app.profile import MAX_PROFILE_DOCUMENTS
from app.redact import MAX_REDACT_DOCUMENTS
from app.reduction import DEFAULT_MAX_VALIDATIONS, MAX_VALIDATIONS_LIMIT
from app.replay import MAX_REPLAY_DOCUMENTS


class AnalysisCreate(BaseModel):
    """POST /api/analyses 请求体。"""

    name: str | None = Field(default=None, max_length=255, description="可选的分析名称")
    old_schema: dict[str, Any] = Field(..., description="旧版 JSON Schema")
    new_schema: dict[str, Any] = Field(..., description="新版 JSON Schema")
    samples: list[Any] | None = Field(default=None, description="可选的样例文档列表")


class Issue(BaseModel):
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str
    counterexample: Any | None = None
    counterexample_path: str | None = None


class DirectionSummary(BaseModel):
    errors: int
    warnings: int
    total: int


class ReportSummary(BaseModel):
    backward: DirectionSummary
    forward: DirectionSummary
    total: int
    errors: int
    warnings: int


class SampleResult(BaseModel):
    index: int
    valid_against_old: bool
    valid_against_new: bool
    direction: str | None = None


class Report(BaseModel):
    backward_compatible: bool
    forward_compatible: bool
    directions: dict[str, str]
    summary: ReportSummary
    issues: list[Issue]
    sample_results: list[SampleResult]


class AnalysisResponse(BaseModel):
    id: int
    name: str | None
    created_at: str
    old_schema: dict[str, Any]
    new_schema: dict[str, Any]
    samples: list[Any]
    report: Report


class AnalysisListItem(BaseModel):
    id: int
    name: str | None
    created_at: str
    backward_compatible: bool
    forward_compatible: bool
    errors: int
    warnings: int


class AnalysisListResponse(BaseModel):
    total: int
    items: list[AnalysisListItem]


class CompareRequest(BaseModel):
    """POST /api/analyses/compare 请求体。"""

    baseline_id: int = Field(..., ge=1, description="基线分析记录 ID")
    current_id: int = Field(..., ge=1, description="当前分析记录 ID")


class ComparedIssue(BaseModel):
    """对比结果中的一条 issue（含稳定指纹，不含反例等大字段）。"""

    fingerprint: str
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str


class SeverityChangedIssue(ComparedIssue):
    baseline_severity: Literal["error", "warning"]
    current_severity: Literal["error", "warning"]


class IssueGroups(BaseModel):
    added: list[ComparedIssue]
    fixed: list[ComparedIssue]
    persisting: list[ComparedIssue]
    severity_changed: list[SeverityChangedIssue]


class IssueGroupCounts(BaseModel):
    added: int
    fixed: int
    persisting: int
    severity_changed: int


class CompatibilityChange(BaseModel):
    baseline: bool
    current: bool
    changed: bool


class CompatibilityDiff(BaseModel):
    backward: CompatibilityChange
    forward: CompatibilityChange


class MetricDiff(BaseModel):
    baseline: int
    current: int
    delta: int


class DirectionSummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff


class SummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff
    backward: DirectionSummaryDiff
    forward: DirectionSummaryDiff


class CompareResponse(BaseModel):
    baseline_id: int
    current_id: int
    compatibility: CompatibilityDiff
    summary_diff: SummaryDiff
    issues: IssueGroups
    counts: IssueGroupCounts


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate 发布门禁
# ---------------------------------------------------------------------------


class ExemptionRule(BaseModel):
    """issue 豁免规则：direction / code 字面相等，path 支持单段 ``*`` 通配。"""

    direction: Literal["backward", "forward"] = Field(..., description="兼容方向")
    code: str = Field(..., min_length=1, description="issue 代码（精确匹配）")
    path: str = Field(
        ...,
        description=(
            "JSON Pointer 风格 schema 路径（如 #/properties/a）；"
            "单段 * 为通配（如 #/properties/*），根路径为 #"
        ),
    )

    @model_validator(mode="after")
    def _validate_path(self):
        path = self.path
        if path != "#" and not path.startswith("#/"):
            raise ValueError('path 必须为 "#" 或以 "#/" 开头的 JSON Pointer')
        if path != "#":
            for segment in path[2:].split("/"):
                if segment == "":
                    raise ValueError("path 含空路径段")
        return self


class GateRequest(BaseModel):
    """POST /api/analyses/{id}/gate 请求体。"""

    directions: list[Literal["backward", "forward"]] = Field(
        ..., min_length=1, description="需要检查的兼容方向（至少一个）"
    )
    max_warnings: dict[Literal["backward", "forward"], int] | None = Field(
        default=None,
        description="各方向允许的未豁免 warning 上限（未声明方向视为 0）",
    )
    exemptions: list[ExemptionRule] = Field(
        default_factory=list, description="issue 豁免规则，按声明顺序确定优先级"
    )

    @model_validator(mode="after")
    def _dedupe_directions(self):
        seen: set[str] = set()
        unique: list[str] = []
        for name in self.directions:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        self.directions = unique
        if self.max_warnings:
            for name, value in self.max_warnings.items():
                if value < 0:
                    raise ValueError(f"max_warnings.{name} 不能为负数")
        return self


class GateDirectionCounts(BaseModel):
    errors: int
    warnings: int
    exempted: int
    total: int
    max_warnings: int


class GateBlockingIssue(Issue):
    blocked_reason: Literal["error", "warning_limit_exceeded"]


class GateExemptedIssue(Issue):
    rule_index: int = Field(..., ge=0, description="命中的豁免规则下标（exemptions 从 0 开始）")


class GateResponse(BaseModel):
    # "pass" 是 Python 关键字，模型字段使用 passed，对外序列化为 "pass"
    passed: bool = Field(serialization_alias="pass", description="门禁是否通过")
    counts: dict[Literal["backward", "forward"], GateDirectionCounts]
    blocking: list[GateBlockingIssue]
    exempted: list[GateExemptedIssue]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 合规画像
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    """POST /api/analyses/{id}/profile 请求体。"""

    documents: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        max_length=MAX_PROFILE_DOCUMENTS,
        description=(
            f"待检 JSON 文档列表（必须全部为 JSON 对象），"
            f"数量 1..{MAX_PROFILE_DOCUMENTS}"
        ),
    )


class ProfileFailure(BaseModel):
    """画像中按位置 / 规则归并后的一组失败。"""

    fingerprint: str = Field(..., description="稳定规则指纹（SHA-256 前 16 位）")
    pointer: str = Field(..., description="失败位置（JSON Pointer，最深公共失败路径）")
    validator: str = Field(..., description="jsonschema 校验规则名（如 required / type）")
    schema_pointer: str = Field(..., description="schema 中触发规则的位置（JSON Pointer）")
    message: str = Field(..., description="确定性的失败说明（不随输入顺序变化）")
    count: int = Field(..., ge=0, description="命中该问题的文档数（每文档每指纹仅计一次）")
    example_indices: list[int] = Field(
        ..., description="示例文档下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class ProfileSnapshot(BaseModel):
    """画像结果快照（持久化内容）。"""

    total_documents: int
    valid_documents: int
    invalid_documents: int
    compliance_rate: float = Field(..., description="合规率：valid_documents / total_documents")
    failures: list[ProfileFailure]


class ProfileResponse(ProfileSnapshot):
    """POST 画像 / GET 查询画像的响应。"""

    id: int
    analysis_id: int
    version: int = Field(..., description="该分析下的画像版本号（从 1 开始递增）")
    created_at: str


class ProfileSummary(BaseModel):
    """画像列表中的摘要条目。"""

    id: int
    version: int
    created_at: str
    total_documents: int
    valid_documents: int
    invalid_documents: int
    compliance_rate: float
    failure_groups: int


class ProfileListResponse(BaseModel):
    analysis_id: int
    total: int
    items: list[ProfileSummary]


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay 样本回放诊断
# ---------------------------------------------------------------------------


class ReplayRequest(BaseModel):
    """POST /api/analyses/{id}/replay 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_REPLAY_DOCUMENTS,
        description=(
            f"待回放的 JSON 文档列表（任意 JSON 类型），数量 1..{MAX_REPLAY_DOCUMENTS}"
        ),
    )


class ReplayCounts(BaseModel):
    """回放文档按校验结果的分类计数（四类互斥，总和为文档总数）。"""

    valid: int = Field(..., description="旧、新两版 schema 均校验通过的文档数")
    backward: int = Field(..., description="旧版合法、新版拒绝（向后破坏实锤）的文档数")
    forward: int = Field(..., description="新版合法、旧版拒绝（向前破坏实锤）的文档数")
    invalid_both: int = Field(..., description="旧、新两版均拒绝（无法归因）的文档数")


class ReplayIssueHit(BaseModel):
    """报告中一条 issue 在回放样本上的命中情况。"""

    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    count: int = Field(..., ge=0, description="命中该 issue 的文档数（每文档每 issue 仅计一次）")
    example_indices: list[int] = Field(
        ..., description="示例文档下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class ReplayUnattributedError(BaseModel):
    """无法归因到报告 issue 的校验错误（按方向 / 位置 / 规则归并）。"""

    direction: Literal["backward", "forward", "both"] = Field(
        ..., description="失败方向：单向失败但无匹配 issue，或 both（两版均拒绝）"
    )
    reason: Literal["no_matching_issue", "rejected_by_both"]
    pointer: str = Field(..., description="失败所在的 schema 节点位置（JSON Pointer）")
    validator: str = Field(..., description="jsonschema 校验规则名（如 required / type）")
    schema_pointer: str = Field(..., description="schema 中触发规则的实际位置（JSON Pointer）")
    message: str = Field(..., description="确定性的规则说明（不随实例取值变化）")
    count: int = Field(..., ge=0, description="命中该错误的文档数（每文档同类错误仅计一次）")
    example_indices: list[int]


class ReplayResponse(BaseModel):
    """样本回放诊断结果（接口只读，不持久化）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=0)
    counts: ReplayCounts
    issue_hits: list[ReplayIssueHit] = Field(
        ..., description="报告全部 issue 的命中情况，按报告内 issue 顺序排列（未命中 count=0）"
    )
    unattributed: list[ReplayUnattributedError] = Field(
        ..., description="无法归因到报告 issue 的校验错误分组"
    )


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay-coverage 回放样本最小覆盖
# ---------------------------------------------------------------------------


class ReplayCoverageRequest(BaseModel):
    """POST /api/analyses/{id}/replay-coverage 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_COVERAGE_DOCUMENTS,
        description=(
            f"候选回放 JSON 文档列表（任意类型），数量 1..{MAX_COVERAGE_DOCUMENTS}"
        ),
    )
    directions: list[Literal["backward", "forward"]] = Field(
        ...,
        min_length=1,
        description=(
            "参与覆盖的兼容方向筛选（至少一个），重复值按固定顺序去重；"
            "只统计与选中命中这些方向 issue 的覆盖情况"
        ),
    )

    @model_validator(mode="after")
    def _dedupe_directions(self):
        seen: set[str] = set()
        unique: list[str] = []
        for name in self.directions:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        # 按固定方向顺序排列，保证同一筛选语义的请求体（如顺序不同）结果一致
        order = {"backward": 0, "forward": 1}
        self.directions = sorted(unique, key=order.get)
        return self


class CoveredIssueRef(BaseModel):
    """被选中文档覆盖的一条报告 issue 引用。"""

    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    reason: str
    covered_by: list[int] = Field(
        ..., description="覆盖该 issue 的选中文档下标（基于提交顺序，从 0 开始，升序）"
    )


class UncoveredIssueRef(BaseModel):
    """提交的候选文档均无法覆盖的一条报告 issue 引用。"""

    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    reason: str


class SelectedDocument(BaseModel):
    """贪心集合覆盖选出的一篇代表文档。"""

    index: int = Field(..., ge=0, description="文档在提交列表中的下标（从 0 开始）")
    classification: Literal["valid", "backward", "forward", "invalid_both"] = Field(
        ..., description="该文档的旧 / 新双版校验分类（与回放接口四类一致）"
    )
    newly_covered_issues: int = Field(
        ..., ge=1, description="选中该文档时相对已覆盖集合新增的 issue 数（必定 >= 1）"
    )
    covered_issues: int = Field(
        ..., ge=1, description="该文档可命中的筛选方向内 issue 总数（含与前序代表重复覆盖的部分）"
    )


class ReplayCoverageResponse(BaseModel):
    """回放样本最小覆盖选择结果（接口只读，不持久化）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=0)
    directions: list[Literal["backward", "forward"]] = Field(
        ..., description="实际生效的方向筛选（去重、固定顺序）"
    )
    counts: ReplayCounts = Field(..., description="全部候选文档的原始四向分类计数（不做方向筛选）")
    total_issues: int = Field(
        ..., ge=0, description="筛选方向内报告 issue 总数（按业务身份去重）"
    )
    covered_issue_count: int = Field(..., ge=0)
    uncovered_issue_count: int = Field(..., ge=0)
    selected_indices: list[int] = Field(
        ..., description="贪心选中的代表文档下标（按选中先后，即新增覆盖递减、并列索引升序）"
    )
    selected_documents: list[SelectedDocument] = Field(
        ..., description="代表文档明细，顺序与 selected_indices 一致"
    )
    covered_issues: list[CoveredIssueRef] = Field(
        ..., description="被覆盖的 issue（按报告内 issue 顺序）"
    )
    uncovered_issues: list[UncoveredIssueRef] = Field(
        ..., description="候选文档均无法覆盖的 issue（按报告内 issue 顺序）"
    )


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/mutation-coverage 样本变异覆盖评估
# ---------------------------------------------------------------------------


MutationRuleName = Literal[
    "required_removed",
    "enum_narrowed",
    "minimum_increased",
    "maximum_decreased",
    "type_narrowed",
    "min_length_increased",
    "max_length_decreased",
    "min_items_increased",
    "max_items_decreased",
    "min_properties_increased",
    "max_properties_decreased",
]


class MutationCoverageRequest(BaseModel):
    """POST /api/analyses/{id}/mutation-coverage 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_MUTATION_DOCUMENTS,
        description=(
            f"待评估的 JSON 文档列表（任意 JSON 类型；既可用正向样本发现"
            f"收紧类变异，也可用缺少必填项等负向样本发现放松类变异），"
            f"数量 1..{MAX_MUTATION_DOCUMENTS}"
        ),
    )
    max_per_rule: dict[MutationRuleName, int] | None = Field(
        default=None,
        description=(
            "每类变异的生成上限；未声明的规则使用缺省上限 50，"
            "显式传 0 表示关闭该类变异"
        ),
    )

    @model_validator(mode="after")
    def _validate_limits(self):
        if self.max_per_rule:
            unknown = set(self.max_per_rule) - set(RULE_NAMES)
            if unknown:
                raise ValueError(f"未知变异规则: {sorted(unknown)}")
            for rule, value in self.max_per_rule.items():
                if value < 0:
                    raise ValueError(f"max_per_rule.{rule} 不能为负数")
                if value > MAX_RULE_LIMIT:
                    raise ValueError(
                        f"max_per_rule.{rule} 不能超过 {MAX_RULE_LIMIT}"
                    )
        return self


class MutationRuleScore(BaseModel):
    """按变异规则分类的覆盖率统计。"""

    rule: MutationRuleName
    total: int = Field(..., ge=0, description="该类实际生成的变异体数量（已按上限截断）")
    killed: int = Field(..., ge=0, description="被样本杀死（被某文档校验结果差异发现）的数量")
    surviving: int = Field(..., ge=0, description="未被任何样本发现的存活变异体数量")
    mutation_score: float | None = Field(
        ..., description="该类 mutation score = killed / total；该类无变异体时为 null"
    )


class MutationResult(BaseModel):
    """单个变异体的评估结果。"""

    id: str = Field(..., description="变异体稳定 ID（规则 + 路径 + 主体的 SHA-256 前 12 位）")
    rule: MutationRuleName
    pointer: str = Field(..., description="变异施加的 schema 节点位置（JSON Pointer，根为 #）")
    subject: str = Field(..., description="变异主体（必填字段名 / 被删枚举值 / 被删类型 / 关键字名）")
    description: str = Field(..., description="确定性的单点变异说明")
    killed: bool = Field(..., description="是否被提交的样本发现（原/异 schema 校验结果不同）")
    example_indices: list[int] = Field(
        ..., description="杀死该变异体的示例文档下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class MutationCoverageResponse(BaseModel):
    """样本变异覆盖评估结果（接口只读，不持久化）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=0)
    valid_documents: int = Field(..., ge=0, description="通过原新 schema 校验的文档数")
    invalid_documents: int = Field(..., ge=0, description="被原新 schema 拒绝的文档数（可发现放松类变异）")
    total_mutations: int = Field(..., ge=0)
    killed_mutations: int = Field(..., ge=0)
    surviving_mutations: int = Field(..., ge=0)
    mutation_score: float = Field(..., description="总体 mutation score = killed / total；无变异体时为 1.0")
    limits: dict[MutationRuleName, int] = Field(
        ..., description="本次评估各类变异实际生效的生成上限（缺省规则回填为 50）"
    )
    rules: list[MutationRuleScore] = Field(
        ..., description="按规则分类的 mutation score，顺序固定"
    )
    mutants: list[MutationResult] = Field(
        ..., description="全部变异体的逐变异评估结果，按 (规则, 路径, 主体, ID) 排序"
    )
    uncovered_paths: list[str] = Field(
        ..., description="未被样本覆盖的变异路径（存活变异体的 schema 指针，可能重复）"
    )


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/redact 基于 x-sensitive 的实例脱敏
# ---------------------------------------------------------------------------


class RedactRequest(BaseModel):
    """POST /api/analyses/{id}/redact 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_REDACT_DOCUMENTS,
        description=(
            f"待脱敏的 JSON 文档列表（任意 JSON 类型），数量 1..{MAX_REDACT_DOCUMENTS}"
        ),
    )


class RedactedReplacement(BaseModel):
    """一处脱敏替换：实例指针 + 触发替换的 schema 节点指针。"""

    instance_pointer: str = Field(
        ..., description="被替换实例值的位置（JSON Pointer，根为 #）"
    )
    schema_pointer: str = Field(
        ..., description="声明 x-sensitive: true 的 schema 节点位置（JSON Pointer，根为 #）"
    )


class RedactedDocument(BaseModel):
    """单篇文档的脱敏结果。"""

    index: int = Field(..., ge=0, description="文档在提交列表中的下标（从 0 开始）")
    redacted: Any = Field(..., description="脱敏后的文档（入参文档的深拷贝，仅命中位置被替换为 ***）")
    replacements: list[RedactedReplacement] = Field(
        ..., description="替换位置列表（实例指针确定排序，同一实例位置最多一条）"
    )


class RedactResponse(BaseModel):
    """实例脱敏结果（接口只读，不持久化、不改动原文与库中样本）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=0)
    results: list[RedactedDocument] = Field(
        ..., description="按提交顺序返回的逐文档脱敏结果"
    )


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/counterexample-reduction 兼容性反例缩减
# ---------------------------------------------------------------------------


class CounterexampleReductionRequest(BaseModel):
    """POST /api/analyses/{id}/counterexample-reduction 请求体。"""

    document: Any = Field(
        ...,
        description=(
            "待缩减的单份 JSON 文档（任意 JSON 类型）；必须先属于 direction "
            "指定的单向破坏类别（backward：旧版合法、新版拒绝；"
            "forward：新版合法、旧版拒绝），否则返回 422"
        ),
    )
    direction: Literal["backward", "forward"] = Field(
        ..., description="需要保持的兼容破坏方向"
    )
    max_validations: int = Field(
        default=DEFAULT_MAX_VALIDATIONS,
        ge=1,
        le=MAX_VALIDATIONS_LIMIT,
        description=(
            f"候选重验次数上限（每次重验含旧、新两版各一次校验，"
            f"1..{MAX_VALIDATIONS_LIMIT}，缺省 {DEFAULT_MAX_VALIDATIONS}）；"
            f"达到上限时只返回最后一次已验证通过的结果"
        ),
    )


class ReductionStep(BaseModel):
    """一步被接受的缩减操作。"""

    index: int = Field(..., ge=0, description="步骤序号（从 0 开始，按接受先后）")
    op: Literal["delete_property", "delete_element", "shorten_string"] = Field(
        ..., description="操作类型：删除对象属性 / 删除数组元素 / 缩短字符串"
    )
    pointer: str = Field(
        ..., description="操作目标在文档中的位置（JSON Pointer，根为 #）"
    )
    key: str | None = Field(
        default=None, description='delete_property 时被删除的属性名；其他操作为 null'
    )
    element_index: int | None = Field(
        default=None, ge=0, description="delete_element 时被删除元素的下标"
    )
    position: int | None = Field(
        default=None, ge=0, description="shorten_string 时被删除字符在原字符串中的位置"
    )
    char: str | None = Field(default=None, description="shorten_string 时被删除的字符")
    from_length: int | None = Field(
        default=None, ge=0, description="shorten_string 时缩短前的字符串长度"
    )
    to_length: int | None = Field(
        default=None, ge=0, description="shorten_string 时缩短后的字符串长度"
    )
    serialized_length_before: int = Field(
        ..., ge=0, description="该步前当前文档的紧凑 JSON 序列化长度"
    )
    serialized_length_after: int = Field(
        ..., ge=0, description="该步后文档的紧凑 JSON 序列化长度（严格小于前值）"
    )
    validations_used: int = Field(
        ..., ge=1, description="接受该步时累计使用的重验次数"
    )


class CounterexampleReductionResponse(BaseModel):
    """兼容性反例缩减结果（接口只读，不修改分析记录、不持久化）。"""

    analysis_id: int
    direction: Literal["backward", "forward"] = Field(
        ..., description="请求指定并全程保持的兼容破坏方向"
    )
    original: Any = Field(..., description="提交的原始文档（未被修改）")
    reduced: Any = Field(
        ...,
        description=(
            "缩减后的文档；每一步都经旧、新两版重新校验且仍属于指定方向；"
            "达到上限时也是最后一次已验证通过的文档"
        ),
    )
    steps: list[ReductionStep] = Field(
        ..., description="每步被接受的操作记录（按接受先后，指针可定位回原文）"
    )
    validation_count: int = Field(
        ..., ge=0, description="实际执行的候选重验次数（旧、新两版各校验一次算一次）"
    )
    max_validations: int = Field(..., ge=1, description="本次生效的重验次数上限")
    limit_reached: bool = Field(
        ..., description="是否因达到重验次数上限而提前结束（True 时仅返回已验证结果）"
    )
    serialized_length_original: int = Field(..., ge=0, description="原文紧凑 JSON 序列化长度")
    serialized_length_reduced: int = Field(..., ge=0, description="缩减结果紧凑 JSON 序列化长度")


# ---------------------------------------------------------------------------
# POST /api/reference-diagnostics 引用依赖诊断（只读，不落库）
# ---------------------------------------------------------------------------


class ReferenceDiagnosticsRequest(BaseModel):
    """POST /api/reference-diagnostics 请求体。"""

    # schema_ 是为避开 BaseModel.schema() 保留方法；对外字段名仍为 schema
    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        serialization_alias="schema",
        description="待诊断的单份 JSON Schema（仅解析其内部本地 $ref）",
    )

    model_config = {"populate_by_name": True}


class ReferenceEdge(BaseModel):
    """一条可解析的本地引用边。"""

    source: str = Field(..., description="声明 $ref 的 schema 节点位置（JSON Pointer，根为 #）")
    ref: str = Field(..., description="$ref 原文（片段形式）")
    target: str = Field(..., description="解析并规范化后的目标节点位置（JSON Pointer）")


class InvalidReference(BaseModel):
    """一条不可用的 $ref 及其定位与原因。"""

    source: str = Field(..., description="声明该 $ref 的 schema 节点位置（JSON Pointer，根为 #）")
    ref: str | None = Field(..., description="$ref 原文；非字符串取值时为 null")
    reason: Literal[
        "external_reference",
        "name_fragment_unsupported",
        "target_not_found",
        "malformed_pointer",
        "non_string_ref",
    ] = Field(
        ...,
        description=(
            "external_reference：非片段形式的外部 / 相对引用；"
            "name_fragment_unsupported：#anchor 名称片段；"
            "target_not_found：指针目标在文档内不存在；"
            "malformed_pointer：非法 JSON Pointer 转义；"
            "non_string_ref：$ref 不是字符串"
        ),
    )
    message: str


class UnreachableDef(BaseModel):
    """一个从根侧不可达的 $defs 节点。"""

    pointer: str = Field(..., description="$defs 节点位置（JSON Pointer）")
    name: str = Field(..., description="节点在所属 $defs 容器内的键名")


class ReferenceCycle(BaseModel):
    """一条引用循环链（首尾相接，含自环）。"""

    chain: list[str] = Field(
        ...,
        min_length=2,
        description=(
            "循环经过的节点指针链，首尾相接（末元素等于首元素）；"
            "以链上最小指针旋转到链首，保证同一环表示唯一"
        ),
    )


class ReferenceDiagnosticsCounts(BaseModel):
    refs: int = Field(..., ge=0, description="$ref 出现总数（可解析边 + 不可用引用）")
    edges: int = Field(..., ge=0, description="可解析的本地引用边数")
    invalid_refs: int = Field(..., ge=0, description="不可用引用数")
    unreachable_defs: int = Field(..., ge=0, description="不可达 $defs 节点数")
    cycles: int = Field(..., ge=0, description="引用循环数（自环计一条）")


class ReferenceDiagnosticsResponse(BaseModel):
    """引用依赖诊断结果（接口只读，不持久化、不创建分析记录）。"""

    counts: ReferenceDiagnosticsCounts
    edges: list[ReferenceEdge] = Field(..., description="全部可解析引用边（按源指针排序）")
    invalid_refs: list[InvalidReference] = Field(
        ..., description="全部不可用引用（按源指针排序）"
    )
    unreachable_defs: list[UnreachableDef] = Field(
        ..., description="从根侧不可达的 $defs 节点（按指针排序）"
    )
    cycles: list[ReferenceCycle] = Field(..., description="引用循环链（按链上指针排序）")
