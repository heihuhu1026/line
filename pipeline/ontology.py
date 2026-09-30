"""Ontology Kernel —— 流水线的轻量、确定性、可序列化语义层（改造规格§三~§七）。

设计纪律（见《Ontology Kernel 改造计划》§一、§四十七）：

* LLM 只提出**候选**（intake/PM/architect/dev/test/review 的产物一律 ``DERIVED``）；
* 只有机械检查 / 真实执行能产生 ``PROVEN``；
* 开发自陈 ``self_check`` 永远是 ``DERIVED``，``WAIVED/SKIPPED != PROVEN``；
* 不引入数据库 / 第三方依赖，只用 dataclass + 稳定 canonical JSON；
* 本模块是**语义覆盖层（semantic overlay）**，不替换 state 里任何既有字段。

所有函数都是纯函数：不读文件、不调模型、不改全局状态；图对象可直接 ``json.dumps``。
旧 run 没有 ontology 字段时按空图安全降级（向后兼容，规格§三十六）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------- 版本

#: 规格§五十九：版本写入 env.fingerprint，质量变化时可区分是 prompt / compiler /
#: ontology rule / retrieval / model 哪一层变了。
ONTOLOGY_SCHEMA_VERSION = "1"
ONTOLOGY_RULES_VERSION = "1"

# --------------------------------------------------------------------- 真值等级

#: 用户原始需求 / 人工裁决。一旦写入当前 revision，不允许被 LLM 静默覆盖。
TRUTH_ASSERTED = "ASSERTED"
#: LLM 各阶段推导，可被后续 revision supersede。
TRUTH_DERIVED = "DERIVED"
#: 只能来自真实运行 / AST / import / contract / patch materialization 等机械来源。
TRUTH_PROVEN = "PROVEN"
TRUTH_LEVELS = (TRUTH_ASSERTED, TRUTH_DERIVED, TRUTH_PROVEN)

#: 事实来源优先级（规格§五十三）：这是**来源优先级**，不是评分；同级冲突 ⇒ CONTRADICTION。
SOURCE_PRIORITY = {
    "user": 5,
    "human": 4,
    "pm": 3,
    "architect": 2,
    "skeleton": 2,
    "compiler": 2,
    "test": 1,
    "dev": 0,
    "self_check": 0,
}

# --------------------------------------------------------------------- 对象类型

TYPE_GOAL = "Goal"
TYPE_REQUIREMENT = "Requirement"
TYPE_ENTITY = "Entity"
TYPE_PROPERTY = "Property"
TYPE_ACTION = "Action"
TYPE_RELATION = "Relation"
TYPE_CONSTRAINT = "Constraint"
TYPE_INVARIANT = "Invariant"
TYPE_CLAIM = "Claim"
TYPE_PROOF_OBLIGATION = "ProofObligation"
TYPE_TASK = "Task"
TYPE_SYMBOL = "Symbol"
TYPE_CONTRACT = "Contract"
TYPE_PATCH = "Patch"
TYPE_WORKSPACE = "Workspace"
TYPE_WORKSPACE_REVISION = "WorkspaceRevision"
TYPE_ARTIFACT = "Artifact"
TYPE_EVIDENCE = "Evidence"
TYPE_DEFECT = "Defect"
TYPE_DECISION = "Decision"
TYPE_FAILURE = "Failure"
TYPE_RECOVERY = "Recovery"
TYPE_PROVENANCE = "Provenance"

#: 规格§四的最小对象集合（不无限扩张）。
OBJECT_TYPES: frozenset[str] = frozenset({
    TYPE_GOAL, TYPE_REQUIREMENT, TYPE_ENTITY, TYPE_PROPERTY, TYPE_ACTION,
    TYPE_RELATION, TYPE_CONSTRAINT, TYPE_INVARIANT, TYPE_CLAIM,
    TYPE_PROOF_OBLIGATION, TYPE_TASK, TYPE_SYMBOL, TYPE_CONTRACT, TYPE_PATCH,
    TYPE_WORKSPACE, TYPE_WORKSPACE_REVISION, TYPE_ARTIFACT, TYPE_EVIDENCE,
    TYPE_DEFECT, TYPE_DECISION, TYPE_FAILURE, TYPE_RECOVERY, TYPE_PROVENANCE,
})

# --------------------------------------------------------------------- 关系白名单

#: 固定 predicate 白名单（规格§六）。**禁止 LLM 自由生成 predicate**；
#: 本表是"至少实现"，新增也必须在此登记（"claim_derives_requirement" 等是
#: 为 Requirement→Claim→ProofObligation 链补的最小两条）。
PREDICATES: frozenset[str] = frozenset({
    "satisfies",            # Requirement satisfies Goal
    "constrains",           # Requirement constrains Entity
    "has_property",         # Requirement has_property Property
    "defines",              # Requirement defines Action
    "implements",           # Task implements Requirement
    "carries_obligation",   # Task carries_obligation ProofObligation（施工图的交证义务）
    "owns",                 # Task owns Symbol
    "belongs_to",           # Symbol belongs_to File
    "targets",              # Task targets File
    "uses",                 # Task uses Contract
    "exposes",              # Task exposes Contract
    "depends_on",           # Task depends_on Task
    "changes",              # Patch changes Symbol
    "materialized_in",      # Patch materialized_in WorkspaceRevision
    "evaluates",            # Verification evaluates ProofObligation
    "proves",               # Evidence proves Claim
    "claim_satisfies",      # Claim satisfies ProofObligation
    "claim_derives",        # Claim derives_from Requirement（语义链补全）
    "obligation_for",       # ProofObligation belongs to Requirement（语义链补全）
    "violates",             # Defect violates Invariant / ProofObligation
    "caused_by",            # Defect/Failure caused_by Artifact
    "raises",              # Failure raises Defect（归因 → 具体缺陷）
    "triggers",            # Failure triggers Recovery（归因 → 恢复动作）
    "affects_task",         # Defect affects Task
    "affects_symbol",       # Defect affects Symbol
    "owned_by",             # Failure owned_by Role
    "returns_to",           # Recovery returns_to Stage
    "derived_from",         # Artifact derived_from Artifact
    "supersedes",           # Artifact supersedes Artifact
    "based_on",             # Decision based_on Evidence
    "resolves",             # Decision resolves Defect
    "applies_to",           # Decision applies_to WorkspaceRevision（裁决针对哪个版本）
    "has_revision",         # Workspace has_revision WorkspaceRevision
})

#: predicate → (允许的主语类型集合, 允许的宾语类型集合)；File/Role/Stage/Verification
#: 不是独立对象类型，用 payload.kind 标记，故这里只对**对象端点**做强约束。
_SUBJECT_TYPES = "subject_types"
_OBJECT_TYPES = "object_types"
_PREDICATE_TYPE_RULES: dict[str, dict[str, frozenset[str]]] = {
    "satisfies": {_SUBJECT_TYPES: frozenset({TYPE_REQUIREMENT}), _OBJECT_TYPES: frozenset({TYPE_GOAL})},
    "implements": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_REQUIREMENT})},
    "carries_obligation": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_PROOF_OBLIGATION})},
    "owns": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_SYMBOL})},
    "uses": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_CONTRACT})},
    "exposes": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_CONTRACT})},
    "depends_on": {_SUBJECT_TYPES: frozenset({TYPE_TASK}), _OBJECT_TYPES: frozenset({TYPE_TASK})},
    "changes": {_SUBJECT_TYPES: frozenset({TYPE_PATCH}), _OBJECT_TYPES: frozenset({TYPE_SYMBOL})},
    "materialized_in": {_SUBJECT_TYPES: frozenset({TYPE_PATCH}), _OBJECT_TYPES: frozenset({TYPE_WORKSPACE_REVISION})},
    "proves": {_SUBJECT_TYPES: frozenset({TYPE_EVIDENCE}), _OBJECT_TYPES: frozenset({TYPE_CLAIM})},
    "claim_satisfies": {_SUBJECT_TYPES: frozenset({TYPE_CLAIM}), _OBJECT_TYPES: frozenset({TYPE_PROOF_OBLIGATION})},
    "claim_derives": {_SUBJECT_TYPES: frozenset({TYPE_CLAIM}), _OBJECT_TYPES: frozenset({TYPE_REQUIREMENT})},
    "obligation_for": {_SUBJECT_TYPES: frozenset({TYPE_PROOF_OBLIGATION}), _OBJECT_TYPES: frozenset({TYPE_REQUIREMENT})},
    "violates": {_SUBJECT_TYPES: frozenset({TYPE_DEFECT}), _OBJECT_TYPES: frozenset({TYPE_INVARIANT, TYPE_PROOF_OBLIGATION})},
    "caused_by": {_SUBJECT_TYPES: frozenset({TYPE_DEFECT, TYPE_FAILURE}), _OBJECT_TYPES: frozenset({TYPE_ARTIFACT})},
    "raises": {_SUBJECT_TYPES: frozenset({TYPE_FAILURE}), _OBJECT_TYPES: frozenset({TYPE_DEFECT})},
    "triggers": {_SUBJECT_TYPES: frozenset({TYPE_FAILURE}), _OBJECT_TYPES: frozenset({TYPE_RECOVERY})},
    "affects_task": {_SUBJECT_TYPES: frozenset({TYPE_DEFECT}), _OBJECT_TYPES: frozenset({TYPE_TASK})},
    "affects_symbol": {_SUBJECT_TYPES: frozenset({TYPE_DEFECT}), _OBJECT_TYPES: frozenset({TYPE_SYMBOL})},
    "derived_from": {_SUBJECT_TYPES: frozenset({TYPE_ARTIFACT, TYPE_REQUIREMENT}), _OBJECT_TYPES: frozenset({TYPE_ARTIFACT, TYPE_REQUIREMENT})},
    "supersedes": {_SUBJECT_TYPES: frozenset({TYPE_ARTIFACT}), _OBJECT_TYPES: frozenset({TYPE_ARTIFACT})},
    "based_on": {_SUBJECT_TYPES: frozenset({TYPE_DECISION}), _OBJECT_TYPES: frozenset({TYPE_EVIDENCE})},
    "resolves": {_SUBJECT_TYPES: frozenset({TYPE_DECISION}), _OBJECT_TYPES: frozenset({TYPE_DEFECT})},
    "applies_to": {_SUBJECT_TYPES: frozenset({TYPE_DECISION}), _OBJECT_TYPES: frozenset({TYPE_WORKSPACE_REVISION})},
    "has_revision": {_SUBJECT_TYPES: frozenset({TYPE_WORKSPACE}), _OBJECT_TYPES: frozenset({TYPE_WORKSPACE_REVISION})},
}

# --------------------------------------------------------------------- Proof Obligation

PO_KIND_BEHAVIOR = "behavior"
PO_KIND_INTERFACE = "interface"
PO_KIND_CONTRACT = "contract"
PO_KIND_MATERIALIZATION = "materialization"
PO_KIND_SYNTAX = "syntax"
PO_KIND_IMPORT = "import"
PO_KIND_COMMAND = "command"
PO_KIND_REGRESSION = "regression"
PO_KIND_INVARIANT = "invariant"
PO_KIND_DELIVERY = "delivery"
#: 规格§十八：接口冻结义务。**双证据** —— 方案契约（contract_check）与
#: 冻结骨架一致性（skeleton_conformance）**都成立**才 PROVEN，任一来源失败即 FAILED，
#: 任一来源没跑（skipped/缺数据）即 UNPROVEN。专门消灭「Architect 说 run / Skeleton 说
#: execute / DEV 实现 dispatch / Test 调 handler」这类跨阶段语义漂移。
PO_KIND_INTERFACE_FREEZE = "interface_freeze"
PROOF_KINDS: frozenset[str] = frozenset({
    PO_KIND_BEHAVIOR, PO_KIND_INTERFACE, PO_KIND_CONTRACT, PO_KIND_MATERIALIZATION,
    PO_KIND_SYNTAX, PO_KIND_IMPORT, PO_KIND_COMMAND, PO_KIND_REGRESSION,
    PO_KIND_INVARIANT, PO_KIND_DELIVERY, PO_KIND_INTERFACE_FREEZE,
})

PO_STATUS_UNPROVEN = "UNPROVEN"
PO_STATUS_PROVEN = "PROVEN"
PO_STATUS_FAILED = "FAILED"
PO_STATUS_WAIVED = "WAIVED"
PROOF_STATUSES: frozenset[str] = frozenset({
    PO_STATUS_UNPROVEN, PO_STATUS_PROVEN, PO_STATUS_FAILED, PO_STATUS_WAIVED,
})

#: 机械类证据来源（只有这些 kind 允许 truth=PROVEN）。
EVIDENCE_KINDS: frozenset[str] = frozenset({
    "patch_audit", "syntax", "import", "contract", "skeleton_conformance", "api_digest",
    "command_result", "test_result", "negative_control", "coverage", "rule",
    "materialization", "human_confirmation",
})
#: 其中 human_confirmation 是 ASSERTED（人工事实），**永远不是 PROVEN**（规格§三十二）。
EVIDENCE_ASSERTED_KINDS: frozenset[str] = frozenset({"human_confirmation"})
#: 必须携带真实执行痕迹（command + exit_code）的证据 kind。
EVIDENCE_EXEC_KINDS: frozenset[str] = frozenset({
    "command_result", "test_result", "negative_control",
})
#: 必须携带机械检查器标识（checker_id）的静态证据 kind。
EVIDENCE_CHECKER_KINDS: frozenset[str] = frozenset({
    "syntax", "import", "contract", "skeleton_conformance", "patch_audit", "rule",
    "materialization", "api_digest", "coverage",
})

#: PO kind → 允许证明它的证据 kind（方案§三 3.3 第 6 条：kind 必须匹配，禁止字符串模糊搜索）。
#: 行为/回归/命令类只认真实断言执行（test_result）——rc==0 的入口冒烟（command_result）
#: 永远不能单独证明业务行为（方案§九）；机械类各自由其专属检查器证据证明。
PO_KIND_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    PO_KIND_BEHAVIOR: frozenset({"test_result"}),
    PO_KIND_REGRESSION: frozenset({"test_result"}),
    PO_KIND_INVARIANT: frozenset({"test_result", "rule"}),
    PO_KIND_COMMAND: frozenset({"test_result"}),
    PO_KIND_SYNTAX: frozenset({"command_result", "test_result", "syntax"}),
    PO_KIND_IMPORT: frozenset({"command_result", "test_result", "import"}),
    PO_KIND_DELIVERY: frozenset({"command_result", "test_result", "materialization"}),
    PO_KIND_MATERIALIZATION: frozenset({"materialization"}),
    PO_KIND_INTERFACE: frozenset({"command_result", "test_result"}),
    PO_KIND_CONTRACT: frozenset({"contract", "test_result", "command_result"}),
    PO_KIND_INTERFACE_FREEZE: frozenset({"contract", "skeleton_conformance"}),
}


# --------------------------------------------------------------------- 工具函数

def canonical_json(obj: Any) -> str:
    """规格§六十四：稳定 canonical JSON（ensure_ascii=False / sort_keys / 最紧分隔符）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(obj: Any, *, length: int = 16) -> str:
    """对任意可 JSON 化对象取稳定短哈希。"""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:length]


def _slug(text: str, *, length: int = 24) -> str:
    """自然语言 Claim → 稳定 ascii slug（中文/标点归一为 hash 后缀，保证可重复）。"""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", str(text or ""))
    head = "_".join(w.lower() for w in words[:3])[:length]
    tail = stable_hash(str(text or ""), length=6)
    return f"{head or 'claim'}_{tail}"


# --------------------------------------------------------------------- 语义信封

@dataclass
class Provenance:
    """来源链单环：谁、在哪个阶段/产物、以什么身份产生了这个对象。"""

    source: str                 # user / human / pm / architect / skeleton / compiler / verify / dev / self_check
    stage: str = ""             # intake / pm / architect_plan / ...
    artifact_id: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "Provenance":
        data = data if isinstance(data, dict) else {}
        return cls(
            source=str(data.get("source") or ""),
            stage=str(data.get("stage") or ""),
            artifact_id=str(data.get("artifact_id") or ""),
            at=str(data.get("at") or ""),
        )


@dataclass
class SemanticObject:
    """规格§五 Semantic Object Envelope。一律通过 to_dict/from_dict JSON 序列化。"""

    id: str
    type: str
    truth: str = TRUTH_DERIVED
    revision: int = 1
    status: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    supersedes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "truth": self.truth,
            "revision": int(self.revision),
            "status": self.status,
            "payload": self.payload if isinstance(self.payload, dict) else {},
            "provenance": [p.to_dict() if isinstance(p, Provenance) else dict(p) for p in self.provenance],
            "derived_from": list(self.derived_from),
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SemanticObject":
        data = data if isinstance(data, dict) else {}
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or ""),
            truth=str(data.get("truth") or TRUTH_DERIVED),
            revision=int(data.get("revision") or 1),
            status=str(data.get("status") or ""),
            payload=dict(data.get("payload") or {}),
            provenance=[Provenance.from_dict(p) for p in (data.get("provenance") or [])],
            derived_from=[str(x) for x in (data.get("derived_from") or [])],
            supersedes=str(data.get("supersedes") or ""),
        )


@dataclass
class OntologyRelation:
    """规格§六：固定 predicate、带真值与证据/来源链的关系。"""

    id: str
    subject: str
    predicate: str
    object: str
    truth: str = TRUTH_DERIVED
    revision: int = 1
    evidence: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "OntologyRelation":
        data = data if isinstance(data, dict) else {}
        return cls(
            id=str(data.get("id") or ""),
            subject=str(data.get("subject") or ""),
            predicate=str(data.get("predicate") or ""),
            object=str(data.get("object") or ""),
            truth=str(data.get("truth") or TRUTH_DERIVED),
            revision=int(data.get("revision") or 1),
            evidence=[str(x) for x in (data.get("evidence") or [])],
            provenance=[str(x) for x in (data.get("provenance") or [])],
        )


@dataclass
class ProofObligation:
    """规格§七：可机械验证的义务。required 且无 verifier = 设计期就该被拦截。"""

    id: str
    name: str
    requirement_id: str
    claim: str
    kind: str = PO_KIND_BEHAVIOR
    required: bool = True
    verifier: dict[str, Any] = field(default_factory=dict)
    status: str = PO_STATUS_UNPROVEN
    evidence_ids: list[str] = field(default_factory=list)
    task_id: str | None = None
    provenance: list[Provenance] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["provenance"] = [p.to_dict() if isinstance(p, Provenance) else dict(p) for p in self.provenance]
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "ProofObligation":
        data = data if isinstance(data, dict) else {}
        kind = str(data.get("kind") or PO_KIND_BEHAVIOR)
        status = str(data.get("status") or PO_STATUS_UNPROVEN)
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            requirement_id=str(data.get("requirement_id") or ""),
            claim=str(data.get("claim") or ""),
            kind=kind if kind in PROOF_KINDS else PO_KIND_BEHAVIOR,
            required=bool(data.get("required", True)),
            verifier=dict(data.get("verifier") or {}),
            status=status if status in PROOF_STATUSES else PO_STATUS_UNPROVEN,
            evidence_ids=[str(x) for x in (data.get("evidence_ids") or [])],
            task_id=(str(data["task_id"]) if data.get("task_id") else None),
            provenance=[Provenance.from_dict(p) for p in (data.get("provenance") or [])],
        )


@dataclass
class EvidenceRecord:
    """规格§三十二：证据。PROVEN 只能来自机械/执行来源并绑定 workspace revision。"""

    id: str
    kind: str
    source: str                       # checker_id 或 command（执行来源）
    status: str = PO_STATUS_UNPROVEN  # PROVEN / FAILED / UNPROVEN（证据三态，规格§二十二）
    claim_ids: list[str] = field(default_factory=list)
    proof_obligation_ids: list[str] = field(default_factory=list)
    workspace_revision: str = ""
    artifact_revision: str = ""
    command: str = ""
    exit_code: int | None = None
    stdout_hash: str = ""
    stderr_hash: str = ""
    digest: str = ""
    created_at: str = ""
    truth: str = TRUTH_DERIVED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "EvidenceRecord":
        data = data if isinstance(data, dict) else {}
        kind = str(data.get("kind") or "")
        return cls(
            id=str(data.get("id") or ""),
            kind=kind,
            source=str(data.get("source") or ""),
            status=str(data.get("status") or PO_STATUS_UNPROVEN),
            claim_ids=[str(x) for x in (data.get("claim_ids") or [])],
            proof_obligation_ids=[str(x) for x in (data.get("proof_obligation_ids") or [])],
            workspace_revision=str(data.get("workspace_revision") or ""),
            artifact_revision=str(data.get("artifact_revision") or ""),
            command=str(data.get("command") or ""),
            exit_code=(int(data["exit_code"]) if data.get("exit_code") is not None else None),
            stdout_hash=str(data.get("stdout_hash") or ""),
            stderr_hash=str(data.get("stderr_hash") or ""),
            digest=str(data.get("digest") or ""),
            created_at=str(data.get("created_at") or ""),
            truth=str(data.get("truth") or TRUTH_DERIVED),
        )


@dataclass
class WorkspaceRevision:
    """规格§十九：工作区逐任务/验证的语义版本。证据必须绑定具体 revision。"""

    revision_id: str
    workspace_id: str
    parent_revision: str = ""
    source: str = ""               # task / verify / base / human
    task_id: str = ""
    patch_digest: str = ""
    manifest_digest: str = ""
    verification_digest: str = ""
    created_at: str = ""
    status: str = ""               # WIP / VERIFIED / FAILED / ...

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "WorkspaceRevision":
        data = data if isinstance(data, dict) else {}
        return cls(
            revision_id=str(data.get("revision_id") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            parent_revision=str(data.get("parent_revision") or ""),
            source=str(data.get("source") or ""),
            task_id=str(data.get("task_id") or ""),
            patch_digest=str(data.get("patch_digest") or ""),
            manifest_digest=str(data.get("manifest_digest") or ""),
            verification_digest=str(data.get("verification_digest") or ""),
            created_at=str(data.get("created_at") or ""),
            status=str(data.get("status") or ""),
        )


# --------------------------------------------------------------------- 图

class OntologyGraph:
    """内存中的语义图。所有变更都返回可 JSON 化结构（``to_dict``），便于落 state。

    线程/IO 无关：构造它不需要 run 目录，旧 run 缺字段时从空图起步。
    """

    def __init__(self, *, workspace_id: str = "ws") -> None:
        self.workspace_id = workspace_id
        self.objects: dict[str, SemanticObject] = {}
        self.relations: list[OntologyRelation] = []
        self.obligations: dict[str, ProofObligation] = {}
        self.evidence: dict[str, EvidenceRecord] = {}
        self.revisions: dict[str, WorkspaceRevision] = {}

    # ---- 对象 ----
    def add(self, obj: SemanticObject) -> SemanticObject:
        """登记对象。同 id 再写且 revision 更高 ⇒ 旧版必须通过 ``supersedes`` 指明。"""
        if not obj.id:
            raise ValueError("SemanticObject.id 不能为空")
        self.objects[obj.id] = obj
        return obj

    def get(self, obj_id: str) -> SemanticObject | None:
        return self.objects.get(obj_id)

    def relate(
        self,
        subject: str,
        predicate: str,
        object: str,
        *,
        truth: str = TRUTH_DERIVED,
        evidence: Iterable[str] | None = None,
        provenance: Iterable[str] | None = None,
        revision: int = 1,
    ) -> OntologyRelation:
        if predicate not in PREDICATES:
            raise ValueError(f"非法 predicate（不在白名单）：{predicate}")
        rel = OntologyRelation(
            id=f"rel:{stable_hash([subject, predicate, object, revision])}",
            subject=subject,
            predicate=predicate,
            object=object,
            truth=truth,
            revision=revision,
            evidence=list(evidence or []),
            provenance=list(provenance or []),
        )
        self.relations.append(rel)
        return rel

    def add_obligation(self, po: ProofObligation) -> ProofObligation:
        self.obligations[po.id] = po
        return po

    def add_evidence(self, ev: EvidenceRecord) -> EvidenceRecord:
        self.evidence[ev.id] = ev
        return ev

    def add_revision(self, rev: WorkspaceRevision) -> WorkspaceRevision:
        self.revisions[rev.revision_id] = rev
        return rev

    def head_revision(self) -> str:
        """沿 parent_revision 链走到的最新 revision id（无环由 validator 保证）。"""
        if not self.revisions:
            return ""
        parents = {r.parent_revision for r in self.revisions.values() if r.parent_revision}
        heads = [rid for rid in self.revisions if rid not in parents]
        if len(heads) == 1:
            return heads[0]
        # 多条链/断链时取 revision_id 最大（确定性兜底，问题仍由 validator 报）。
        return sorted(heads or list(self.revisions))[-1]

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ONTOLOGY_SCHEMA_VERSION,
            "rules_version": ONTOLOGY_RULES_VERSION,
            "workspace_id": self.workspace_id,
            "objects": [o.to_dict() for o in self.objects.values()],
            "relations": [r.to_dict() for r in self.relations],
            "proof_obligations": [p.to_dict() for p in self.obligations.values()],
            "evidence": [e.to_dict() for e in self.evidence.values()],
            "workspace_revisions": [r.to_dict() for r in self.revisions.values()],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "OntologyGraph":
        data = data if isinstance(data, dict) else {}
        g = cls(workspace_id=str(data.get("workspace_id") or "ws"))
        for item in data.get("objects") or []:
            obj = SemanticObject.from_dict(item)
            if obj.id:
                g.objects[obj.id] = obj
        for item in data.get("relations") or []:
            g.relations.append(OntologyRelation.from_dict(item))
        for item in data.get("proof_obligations") or []:
            po = ProofObligation.from_dict(item)
            if po.id:
                g.obligations[po.id] = po
        for item in data.get("evidence") or []:
            ev = EvidenceRecord.from_dict(item)
            if ev.id:
                g.evidence[ev.id] = ev
        for item in data.get("workspace_revisions") or []:
            rev = WorkspaceRevision.from_dict(item)
            if rev.revision_id:
                g.revisions[rev.revision_id] = rev
        return g


# --------------------------------------------------------------------- 稳定身份

def semantic_task_id(
    target_files: Iterable[str],
    symbols: Iterable[str],
    facet_identity: Iterable[str] = (),
) -> str:
    """规格§十四：语义身份 = hash(目标文件 + 规范化符号 + canonical facet 身份)。

    刻意**不含 draft T-01/T-02 编号**：架构师 rework 重编号后，同一个语义任务身份不变，
    bug 映射不断链（规格§二十八 J）。
    """
    payload = {
        "files": sorted(str(f).replace("\\", "/") for f in target_files),
        "symbols": sorted(str(s) for s in symbols),
        "facets": sorted(str(x) for x in facet_identity),
    }
    return "stask:" + stable_hash(payload)


# --------------------------------------------------------------------- Requirement → Claim → PO

#: 文本 → PO kind 的确定性启发（纯字面，不引 LLM）。
def _kind_for_text(text: str) -> str:
    t = str(text or "")
    if any(k in t for k in ("建表", "表不存在", "创建表", "落盘", "物化")):
        return PO_KIND_MATERIALIZATION
    if any(k in t for k in ("导入", "import", "ImportError", "依赖安装")):
        return PO_KIND_IMPORT
    if any(k in t for k in ("接口", "签名", "参数顺序", "构造", "方法名")):
        return PO_KIND_INTERFACE
    if any(k in t for k in ("契约", "跨文件", "调用链")):
        return PO_KIND_CONTRACT
    if any(k in t for k in ("命令", "退出码", "用法", "执行", "子命令")):
        return PO_KIND_COMMAND
    return PO_KIND_BEHAVIOR


def _verifier_for_kind(kind: str) -> dict[str, Any]:
    """每种 PO 绑定一个**可执行 verifier**（规格§九：不能只写人话）。"""
    if kind in (PO_KIND_BEHAVIOR, PO_KIND_COMMAND, PO_KIND_REGRESSION):
        return {"type": "command_assert", "command_family": "behavior_assertion"}
    if kind == PO_KIND_INTERFACE:
        return {"type": "mechanical", "check_id": "interface_audit"}
    if kind == PO_KIND_CONTRACT:
        return {"type": "mechanical", "check_id": "contract_check"}
    if kind == PO_KIND_MATERIALIZATION:
        return {"type": "mechanical", "check_id": "patch_apply"}
    if kind == PO_KIND_SYNTAX:
        return {"type": "mechanical", "check_id": "py_compile"}
    if kind == PO_KIND_IMPORT:
        return {"type": "mechanical", "check_id": "import_check"}
    if kind == PO_KIND_DELIVERY:
        return {"type": "workspace_verified", "check_id": "verify_workspace"}
    if kind == PO_KIND_INTERFACE_FREEZE:
        # 规格§十八：双证据 verifier。requires 里的两个 check_id 必须都在
        # EVIDENCE_CHECKER_KINDS 中有对应机械证据，PO 才能到 PROVEN。
        return {
            "type": "dual_evidence",
            "requires": ["contract_check", "skeleton_conformance"],
        }
    return {"type": "mechanical", "check_id": kind}


#: 规格§九：每条内置 Invariant 都必须绑定一个**可执行**检查（不能只写人话）。
#: ``entry`` 是仓库内真实存在的脚本/函数（人工与 Schema Guardian 都可按图索骥）；
#: ``kind`` 标明它怎么跑：smoke/tool=``python <entry>``，module=导入后调用的纯函数。
#: 架构师在 plan.invariants 里只写 id/name 时，:func:`bound_invariant_check` 按本表补绑。
INVARIANT_EXECUTABLE_CHECKS: dict[str, dict[str, str]] = {
    "cross_round_merge_order": {
        "check_id": "smoke_merge", "kind": "smoke", "entry": "tools/smoke_merge.py"},
    "patch_application": {
        "check_id": "smoke_patch_apply", "kind": "smoke", "entry": "tools/smoke_patch_apply.py"},
    "minimal_bugfix_scope": {
        "check_id": "smoke_bugfix", "kind": "smoke", "entry": "tools/smoke_bugfix.py"},
    "recovery_status_transition": {
        "check_id": "smoke_recovery", "kind": "smoke", "entry": "tools/smoke_recovery.py"},
    "documentation_refs": {
        "check_id": "check_refs", "kind": "tool", "entry": "tools/check_refs.py"},
    "task_dependency_acyclic": {
        "check_id": "dependency_cycle", "kind": "module",
        "entry": "pipeline/ontology_validate.py:validate_tasks"},
    "interface_freeze": {
        "check_id": "skeleton_conformance", "kind": "module",
        "entry": "pipeline/verify.py:skeleton_conformance"},
    "verified_workspace_consistency": {
        "check_id": "verify_workspace", "kind": "module",
        "entry": "pipeline/verify.py:verify"},
}

#: check_id → 内置 invariant id 的反查表（check_id 全部唯一；Schema Guardian 会守这一点）。
_CHECK_ID_INDEX: dict[str, str] = {
    binding["check_id"]: inv_id for inv_id, binding in INVARIANT_EXECUTABLE_CHECKS.items()
}


def bound_invariant_check(invariant: Any) -> dict[str, Any]:
    """把一个 Invariant（dict / payload / SemanticObject）解析为可执行检查绑定（纯函数）。

    解析顺序：① 显式 ``check.check_id`` 命中内置表（按 invariant id 或 check_id 双键）；
    ② ``id`` 命中内置 invariant id；③ ``name/text`` 字面包含内置 id；
    ④ 显式声明了机械 check 但查无此检查 → ``{"known": False}``（validator 拦截）；
    ⑤ 纯自然语言不变量（无 check）→ ``{"bound": False}``（不冒充可机械兑现）。
    """
    # 同时接受 SemanticObject（dataclass）与 dict 两种输入。
    if hasattr(invariant, "payload") and not isinstance(invariant, dict):
        invariant = {"id": str(getattr(invariant, "id", "")),
                     "payload": invariant.payload if isinstance(invariant.payload, dict) else {}}
    inv = invariant if isinstance(invariant, dict) else {}
    payload = inv.get("payload") if isinstance(inv.get("payload"), dict) else inv
    check = payload.get("check") if isinstance(payload.get("check"), dict) else {}
    check_id = str(check.get("check_id") or "").strip()
    inv_id = str(payload.get("id") or inv.get("id") or "").strip()
    name = str(payload.get("name") or payload.get("text") or inv.get("name") or "").strip()

    def _hit(key: str, binding: dict[str, str]) -> dict[str, Any]:
        return {
            "bound": True, "known": True, "invariant_id": key,
            "type": "mechanical", "check_id": binding["check_id"],
            "check_kind": binding["kind"], "entry": binding["entry"],
        }

    if check_id and check_id in INVARIANT_EXECUTABLE_CHECKS:
        return _hit(check_id, INVARIANT_EXECUTABLE_CHECKS[check_id])
    if check_id and check_id in _CHECK_ID_INDEX:
        key = _CHECK_ID_INDEX[check_id]
        return _hit(key, INVARIANT_EXECUTABLE_CHECKS[key])
    if inv_id and inv_id in INVARIANT_EXECUTABLE_CHECKS:
        return _hit(inv_id, INVARIANT_EXECUTABLE_CHECKS[inv_id])
    for key in INVARIANT_EXECUTABLE_CHECKS:  # 名字兜底：确定性字面包含，不引 LLM
        if key in name:
            return _hit(key, INVARIANT_EXECUTABLE_CHECKS[key])
    if str(check.get("type") or "") == "mechanical" or check_id:
        return {"bound": True, "known": False, "invariant_id": inv_id,
                "type": "mechanical", "check_id": check_id}
    return {"bound": False, "known": False, "invariant_id": inv_id}


def requirement_claims(scope: Any) -> list[dict[str, str]]:
    """把 PM scope 的 FR / acceptance_criteria 拍平成 ``{req_id, text}``（确定性）。"""
    scope = scope if isinstance(scope, dict) else {}
    out: list[dict[str, str]] = []
    for fr in scope.get("functional_requirements") or []:
        if not isinstance(fr, dict):
            continue
        rid = str(fr.get("id") or "").strip() or f"FR-{len(out) + 1:02d}"
        desc = str(fr.get("description") or fr.get("title") or "").strip()
        if desc:
            out.append({"req_id": rid, "text": desc})
        for acc in fr.get("acceptance") or []:
            if str(acc or "").strip():
                out.append({"req_id": rid, "text": str(acc).strip()})
    for crit in scope.get("acceptance_criteria") or []:
        if str(crit or "").strip():
            out.append({"req_id": "", "text": str(crit).strip()})
    return out


def build_requirement_projection(
    graph: OntologyGraph,
    scope: Any,
    *,
    original_requirement: str = "",
    delivery_obligation: bool = True,
) -> OntologyGraph:
    """规格§八/§十二/§五十一：Requirement → Claim → ProofObligation 投影（纯函数）。

    * 原始用户需求 = ASSERTED Goal/Requirement（不可被 LLM 覆盖）；
    * PM 的 FR / 验收口径 = DERIVED Requirement，``derived_from`` 指向原始需求；
    * 每条可验证文本生成一个 DERIVED Claim + 一个 required ProofObligation（带 verifier）。

    旧字段 functional_requirements / acceptance / acceptance_criteria 原样保留，
    本投影只是叠加语义身份。
    """
    scope = scope if isinstance(scope, dict) else {}
    prov_pm = Provenance(source="pm", stage="pm")
    if original_requirement:
        root = SemanticObject(
            id="req:root", type=TYPE_REQUIREMENT,
            truth=TRUTH_ASSERTED,
            status="active",
            payload={"text": str(original_requirement)},
            provenance=[Provenance(source="user", stage="intake")],
        )
        graph.add(root)
        goal = SemanticObject(
            id="goal:root", type=TYPE_GOAL, truth=TRUTH_ASSERTED,
            payload={"text": str(original_requirement)[:200]},
            provenance=[Provenance(source="user", stage="intake")],
        )
        graph.add(goal)
        graph.relate("req:root", "satisfies", "goal:root", truth=TRUTH_ASSERTED)

    for item in requirement_claims(scope):
        rid = item["req_id"] or f"AC-{stable_hash(item['text'], length=6)}"
        req_id = f"req:{rid}"
        if graph.get(req_id) is None:
            req_obj = SemanticObject(
                id=req_id, type=TYPE_REQUIREMENT, truth=TRUTH_DERIVED,
                payload={"text": item["text"]}, provenance=[prov_pm],
                derived_from=(["req:root"] if original_requirement else []),
            )
            graph.add(req_obj)
        kind = _kind_for_text(item["text"])
        claim_id = f"claim:{rid}:{_slug(item['text'])}"
        po_id = f"po:{rid}:{_slug(item['text'])}"
        graph.add(SemanticObject(
            id=claim_id, type=TYPE_CLAIM, truth=TRUTH_DERIVED,
            payload={"text": item["text"]}, provenance=[prov_pm],
            derived_from=[req_id],
        ))
        graph.add_obligation(ProofObligation(
            id=po_id, name=item["text"][:80], requirement_id=req_id,
            claim=item["text"], kind=kind, required=True,
            verifier=_verifier_for_kind(kind), provenance=[prov_pm],
        ))
        graph.relate(claim_id, "claim_derives", req_id)
        graph.relate(claim_id, "claim_satisfies", po_id)
        graph.relate(po_id, "obligation_for", req_id)

    if delivery_obligation:
        po_id = "po:delivery:workspace-verified"
        if po_id not in graph.obligations:
            graph.add_obligation(ProofObligation(
                id=po_id, name="交付物在物化工作区中被真实验证（verify 真实执行并通过）",
                requirement_id="req:root" if original_requirement else "",
                claim="verified workspace", kind=PO_KIND_DELIVERY, required=True,
                verifier=_verifier_for_kind(PO_KIND_DELIVERY),
            ))
    return graph


# --------------------------------------------------------------------- 三态裁决（Proof Gate 核心）

def release_proof_status(
    *,
    semantic_pass: bool,
    mechanical_blockers: Iterable[str] | None = None,
    verify_verdict: str = "skipped",
    obligations: Iterable[ProofObligation | dict[str, Any]] | None = None,
    workspace_verified: bool = False,
    negative_control_no_power: Iterable[str] | None = None,
    unresolved_contracts: Iterable[str] | None = None,
    plan_gap: bool = False,
) -> dict[str, Any]:
    """规格§四十五：最终 PASS 的机械必要条件（纯函数）。

    返回 ``{"status", "mandatory_missing", "failed", "reasons"}``：

    * ``FAILED``   —— 存在失败证据（阻断 / verify fail / PO FAILED）；
    * ``UNPROVEN`` —— 没有失败，但缺必需证据（verify skipped / required PO 未 PROVEN /
      工作区未验证 / 负向对照无判别力 / 契约未决 / 方案漏项）；
    * ``PROVEN``   —— 失败与缺失都为空。

    关键铁律（规格§二十/§四十五）：``verify skipped + semantic pass = NOT PASS``。
    本函数**不看 semantic_pass 来发 PROVEN**，只在语义层想 pass 时用它区分
    FAILED 与 UNPROVEN 的路由；semantic 不 pass 时调用方按既有 recovery 走。
    """
    blockers = [str(b) for b in (mechanical_blockers or []) if str(b).strip()]
    no_power = [str(x) for x in (negative_control_no_power or []) if str(x).strip()]
    contracts = [str(x) for x in (unresolved_contracts or []) if str(x).strip()]
    failed: list[str] = []
    missing: list[str] = []

    verdict = str(verify_verdict or "skipped").strip()
    if verdict == "fail":
        failed.append("verify_failed：运行验证存在失败命令/物化失败")
    if blockers:
        failed.append(f"mechanical_blockers：{len(blockers)} 条阻断级机械证据")

    normalized: list[ProofObligation] = []
    for raw in obligations or ():
        normalized.append(raw if isinstance(raw, ProofObligation) else ProofObligation.from_dict(raw))
    for po in normalized:
        if not po.required:
            continue
        if po.status == PO_STATUS_FAILED:
            failed.append(f"{po.id}：{po.name[:40]}（机械验证失败）")
        elif po.status != PO_STATUS_PROVEN:
            # WAIVED / UNPROVEN 都不是 PROVEN（规格§七 WAIVED != PROVEN）
            missing.append(f"{po.id}：{po.name[:40]}（{po.status}）")

    if verdict == "skipped":
        missing.append("verify_skipped：运行验证被跳过（UNPROVEN，skipped≠pass）")
    if not workspace_verified:
        missing.append("workspace_not_verified：没有 VERIFIED 工作区")
    if no_power:
        missing.append(f"negative_control_no_power：{len(no_power)} 条断言撤掉改动仍通过（无判别力）")
    if contracts:
        missing.append(f"unresolved_contracts：{len(contracts)} 条跨文件契约未决")
    if plan_gap:
        missing.append("plan_gap：缺陷指向方案未规划的文件")

    if failed:
        status = "FAILED"
    elif missing:
        status = "UNPROVEN"
    else:
        status = "PROVEN"
    return {
        "status": status,
        "semantic_pass": bool(semantic_pass),
        "mandatory_missing": missing,
        "failed": failed,
        "can_pass": status == "PROVEN" and bool(semantic_pass),
        "code": "" if status == "PROVEN" else ("required_proof_failed" if failed else "required_proof_unproven"),
    }


# --------------------------------------------------------------------- verify 报告 → 证据/义务评估

def _text_hash(value: Any) -> str:
    """输出文本哈希（只留指纹，不把整段 stdout 塞进语义图）。"""
    return stable_hash(str(value or ""))


def _is_assertion_command(command: str) -> bool:
    """判断一条真实命令是否属于**断言型**（pytest/unittest/直接跑 test_*.py）。

    刻意排除 ``py_compile``（只解析不执行，命令行里出现 test_main.py 文件名不算跑测试）
    与 ``python -c``（窄验证由命令证据覆盖，不充当行为断言）。
    """
    low = str(command or "").lower()
    if "py_compile" in low or " -c " in low:
        return False
    if "pytest" in low or "unittest" in low:
        return True
    # 直接执行测试脚本：python .../test_x.py / .../x_test.py
    return bool(re.search(r"[a-z0-9_\-]*test[a-z0-9_\-]*\.py\b", low))


def _as_evidence(evidence: "EvidenceRecord | dict[str, Any]") -> "EvidenceRecord":
    return evidence if isinstance(evidence, EvidenceRecord) else EvidenceRecord.from_dict(evidence)


def evidence_po_bound(evidence: "EvidenceRecord | dict[str, Any]", po: "ProofObligation") -> bool:
    """方案§三 3.3 第 1/6 条：证据**显式绑定**该 PO 且证据 kind 与 PO kind 匹配。

    禁止字符串模糊匹配（``"add" in command`` 之类一律不算证明关系）。
    """
    ev = _as_evidence(evidence)
    if po.id not in (ev.proof_obligation_ids or []):
        return False
    return ev.kind in PO_KIND_EVIDENCE_KINDS.get(po.kind, frozenset())


def evidence_proves_po(
    evidence: "EvidenceRecord | dict[str, Any]",
    po: "ProofObligation",
    *,
    head_revision: str = "",
) -> bool:
    """严格判定一条证据能否证明一个 PO（方案§三 3.3，六条全部满足才 True）。

    1. ``evidence.proof_obligation_ids`` 显式包含 ``po.id``（禁止一条证据默认证明全部 PO）；
    2. ``evidence.truth == PROVEN``（真实机械/执行事实；DERIVED/ASSERTED 不能证明）；
    3. ``evidence.status == PROVEN``（valid；失败证据走 :func:`evidence_fails_po`）；
    4. 证据绑定的 ``workspace_revision`` 与当前验证 revision 一致；
    5. 证据未被 supersede：证据没有独立 supersedes 边，其绑定 revision 一旦不再是
       head（被后续 revision 取代）即视为 stale —— 与第 4 条同判（旧 revision 证据不可证明当前交付）；
    6. ``evidence.kind`` 与 ``po.kind`` 匹配（behavior 只认 test_result，rc==0 冒充业务证明被拒）。

    纯函数、无 IO、无模糊匹配。
    """
    ev = _as_evidence(evidence)
    if not evidence_po_bound(ev, po):
        return False
    if ev.truth != TRUTH_PROVEN or ev.status != PO_STATUS_PROVEN:
        return False
    if not ev.workspace_revision:
        return False
    if head_revision and ev.workspace_revision != head_revision:
        return False
    return True


def evidence_fails_po(
    evidence: "EvidenceRecord | dict[str, Any]",
    po: "ProofObligation",
    *,
    head_revision: str = "",
) -> bool:
    """严格判定一条证据能否判一个 PO **FAILED**（绑定/kind/revision 规则同 proves，状态取 FAILED）。

    失败同样必须精确归因：未绑定到该 PO 的失败证据不能判它 FAILED（应 UNPROVEN）。
    """
    ev = _as_evidence(evidence)
    if not evidence_po_bound(ev, po):
        return False
    if ev.truth != TRUTH_PROVEN or ev.status != PO_STATUS_FAILED:
        return False
    if not ev.workspace_revision:
        return False
    if head_revision and ev.workspace_revision != head_revision:
        return False
    return True


def evaluate_against_verify(
    graph: OntologyGraph,
    verify_report: Any,
    *,
    contract_problems: Iterable[Any] = (),
    skeleton_conformance: Any = None,
    contract_checked: bool = False,
    command_po_bindings: "Mapping[str, Iterable[str]] | None" = None,
    parent_revision: str = "",
    at: str = "",
) -> OntologyGraph:
    """规格§二十/§三十二/§十八：把真实 verify 报告机械映射为 Evidence + PO 三态（纯函数）。

    * 物化结果 → ``materialization`` 证据 + 一个 :class:`WorkspaceRevision`；
    * 每条真实执行的命令 → ``command_result`` / ``test_result`` 证据（带 exit_code）；
    * 负向对照 → ``negative_control`` 证据；
    * 跨文件契约 → ``contract`` 证据；冻结骨架一致性 → ``skeleton_conformance`` 证据
      （``interface_freeze`` PO 必须**两条都 PROVEN**，规格§十八）；
    * 投影出的 PO 按 kind 对号入座：pass=PROVEN / fail=FAILED / skipped=UNPROVEN。

    ``contract_checked`` 显式声明 contract_check 这轮真机跑过（它由编排器聚合验证调用，
    不在 verify() 内部）；``skeleton_conformance`` 传 ``None`` 表示该来源缺席（UNPROVEN）。

    ``command_po_bindings``（命令串→PO id 列表）与命令 dict 自带的 ``target_po`` /
    ``target_po_ids`` / ``proof_obligation_ids`` 字段是**显式证据绑定**（方案§三 3.3）：
    只要任一绑定来源出现，behavior/command/regression/invariant 类 PO 就走严格路径，
    只有显式绑定且 kind 匹配的 PROVEN 证据能证明它，禁止串证；无任何绑定来源时
    保留旧行为（pass + 断言型证据 → 行为族 PO 全 PROVEN），兼容旧调用方与旧 run。

    **self_check 不参与本函数** —— 这里的每一条证据都来自机械执行/静态检查。
    """
    report = verify_report if isinstance(verify_report, dict) else {}
    verdict = str(report.get("verdict") or "skipped")
    materialized = sorted(str(p) for p in (report.get("materialized") or []))
    problems = [str(p) for p in (report.get("problems") or [])]
    executed = [
        c for c in (report.get("commands") or [])
        if isinstance(c, dict) and str(c.get("status") or "") in ("ok", "fail", "timeout", "error")
    ]
    # ---- 显式证据绑定（方案§三 3.3）：入参 bindings + 命令 dict 自带 target_po 字段 ----
    explicit_bindings: dict[str, list[str]] = {}
    if command_po_bindings:
        for cmd_text, po_ids in command_po_bindings.items():
            explicit_bindings[str(cmd_text)] = [str(x) for x in (po_ids or []) if str(x)]
    has_explicit_bindings = bool(explicit_bindings) or any(
        isinstance(c, dict) and (c.get("target_po") or c.get("target_po_ids") or c.get("proof_obligation_ids"))
        for c in (report.get("commands") or [])
    )
    interface = report.get("interface_audit") if isinstance(report.get("interface_audit"), dict) else {}
    missing_symbols = list(interface.get("missing_symbols") or [])
    contracts = [str(p) for p in (contract_problems or ())]
    control = report.get("negative_control") if isinstance(report.get("negative_control"), dict) else {}
    no_power = [str(x) for x in (control.get("no_power") or [])]

    # ---- WorkspaceRevision（证据必须绑定具体 revision，规格§十九）----
    manifest_digest = stable_hash(materialized)
    rev_id = f"wsr:verify:{manifest_digest[:12]}"
    rev_status = {"pass": "VERIFIED", "fail": "FAILED"}.get(verdict, "UNVERIFIED")
    # verify 工作区由在制链头物化而来（方案§二十三）：父链已存在才挂，缺省安全。
    rev_parent = parent_revision if (parent_revision and parent_revision in graph.revisions) else ""
    graph.add_revision(WorkspaceRevision(
        revision_id=rev_id, workspace_id=graph.workspace_id, source="verify",
        parent_revision=rev_parent,
        manifest_digest=manifest_digest,
        verification_digest=stable_hash(
            {"v": verdict, "c": [
                {"cmd": str(c.get("command") or ""), "code": c.get("exit_code"),
                 "status": str(c.get("status") or "")} for c in executed],
             "p": problems, "nc": no_power}
        ),
        created_at=at, status=rev_status,
    ))
    if graph.get("ws:main") is None:
        graph.add(SemanticObject(id="ws:main", type=TYPE_WORKSPACE,
                                 payload={"workspace_id": graph.workspace_id}))
    graph.relate("ws:main", "has_revision", rev_id)

    def _ev_id(kind: str, source: str) -> str:
        return f"ev:{kind}:{stable_hash([source, rev_id], length=10)}"

    # ---- 物化证据 ----
    mat_ev_id = _ev_id("materialization", manifest_digest)
    mat_failed = any(("未能套用" in p) or ("未能落盘" in p) or ("一条都没能" in p) for p in problems)
    if materialized or verdict in ("pass", "fail"):
        graph.add_evidence(EvidenceRecord(
            id=mat_ev_id, kind="materialization", source="patches.apply_all",
            status=PO_STATUS_FAILED if mat_failed else (PO_STATUS_PROVEN if materialized else PO_STATUS_UNPROVEN),
            truth=TRUTH_PROVEN if (materialized and not mat_failed) else TRUTH_DERIVED,
            workspace_revision=rev_id, digest=manifest_digest, created_at=at,
        ))

    # ---- 命令/测试证据 ----
    cmd_evidence: list[str] = []
    assertion_evidence: list[str] = []
    for c in executed:
        cmd = str(c.get("command") or "")
        # TestCompiler 透传了业务断言（exit_code==0 之外）的命令等价于断言型测试：
        # 它的通过/失败有机械断言支撑；裸 rc=0 的入口冒烟仍然只是 command_result。
        behavioral_assertions = [
            str(a).strip() for a in (c.get("assertions") or [])
            if str(a).strip() not in ("", "exit_code==0")
        ]
        is_test = _is_assertion_command(cmd) or bool(behavioral_assertions)
        kind = "test_result" if is_test else "command_result"
        eid = _ev_id(kind, f"{cmd}:{c.get('exit_code')}")
        ok = str(c.get("status") or "") == "ok"
        # 显式绑定：命令 dict 自带字段优先，其次入参 bindings（只绑图上已存在的 PO）。
        bound_pos: list[str] = []
        for raw in (
            c.get("proof_obligation_ids"),
            c.get("target_po_ids"),
            [c.get("target_po")] if c.get("target_po") else None,
            explicit_bindings.get(cmd),
        ):
            for pid in (raw or []):
                pid = str(pid or "")
                if pid and pid in graph.obligations and pid not in bound_pos:
                    bound_pos.append(pid)
        ev = EvidenceRecord(
            id=eid, kind=kind, source=cmd,
            status=PO_STATUS_PROVEN if ok else PO_STATUS_FAILED,
            truth=TRUTH_PROVEN, workspace_revision=rev_id,
            proof_obligation_ids=bound_pos,
            command=cmd, exit_code=c.get("exit_code"),
            stdout_hash=_text_hash(c.get("stdout_tail")),
            stderr_hash=_text_hash(c.get("stderr_tail")),
            created_at=at,
        )
        graph.add_evidence(ev)
        cmd_evidence.append(eid)
        if is_test:
            assertion_evidence.append(eid)

    # ---- 负向对照证据（方案§十八：必须合法化）----
    # negative_control 是**执行类**证据：每条都必须真实重跑过，带
    # command/exit_code/execution source/workspace_revision，四者缺一就不许产生证据，
    # 更不许无痕迹标 PROVEN。撤掉改动后断言仍过 ⇒ 无判别力（FAILED，命令进 no_power）；
    # 撤掉后变红 ⇒ 有判别力（PROVEN）。旧报告没有 runs 字段时缺省为不产生证据（旧 run 可读）。
    nc_ev_ids: list[str] = []
    for run in (control.get("runs") or []):
        if not isinstance(run, dict):
            continue
        nc_cmd = str(run.get("command") or "")
        nc_code = run.get("exit_code")
        nc_st = str(run.get("status") or "")
        if (not nc_cmd or not isinstance(nc_code, int)
                or nc_st not in ("ok", "fail", "timeout", "error")):
            continue
        eid = _ev_id("negative_control", nc_cmd)
        graph.add_evidence(EvidenceRecord(
            id=eid, kind="negative_control",
            source="verify.negative_control:" + nc_cmd,
            status=PO_STATUS_FAILED if nc_st == "ok" else PO_STATUS_PROVEN,
            truth=TRUTH_PROVEN, workspace_revision=rev_id, created_at=at,
            command=nc_cmd, exit_code=nc_code,
            stdout_hash=_text_hash(run.get("stdout_tail")),
            stderr_hash=_text_hash(run.get("stderr_tail")),
        ))
        nc_ev_ids.append(eid)

    # ---- 契约 / 冻结骨架双路静态证据（规格§十八 Interface Freeze）----
    # 两路都只在真机静态检查真的跑过时产生；缺席 = 没有该来源证据（UNPROVEN），
    # 绝不拿另一路顶替（双证据缺一不可）。
    contract_ev_id = ""
    skeleton_ev_id = ""
    static_ran = verdict in ("pass", "fail")
    if static_ran and contract_checked:
        contract_ev_id = _ev_id("contract", f"contract_check:{len(contracts)}")
        graph.add_evidence(EvidenceRecord(
            id=contract_ev_id, kind="contract", source="verify.contract_check",
            status=PO_STATUS_FAILED if contracts else PO_STATUS_PROVEN,
            truth=TRUTH_PROVEN, workspace_revision=rev_id, created_at=at,
        ))
    conf = skeleton_conformance if isinstance(skeleton_conformance, dict) else {}
    if static_ran and conf:
        skel_bad = [str(x) for x in (conf.get("missing") or [])]
        skel_bad += [str(x) for x in (conf.get("mismatch") or [])]
        skeleton_ev_id = _ev_id("skeleton_conformance", f"skel:{len(skel_bad)}")
        graph.add_evidence(EvidenceRecord(
            id=skeleton_ev_id, kind="skeleton_conformance",
            source="verify.skeleton_conformance",
            status=PO_STATUS_FAILED if skel_bad else PO_STATUS_PROVEN,
            truth=TRUTH_PROVEN, workspace_revision=rev_id, created_at=at,
        ))

    def _freeze_symbol_bad(file_path: str, symbol: str) -> tuple[bool, bool]:
        """该冻结符号在契约路 / 骨架路是否失败（确定性字面匹配，纯数据判断）。"""
        base = os.path.basename(file_path)
        contract_bad = any(
            symbol and symbol in p and (not file_path or file_path in p or base in p)
            for p in contracts
        )
        skel_bad = False
        if conf:
            names = set((conf.get("by_file") or {}).get(file_path) or [])
            cls = symbol.split(".", 1)[0] if "." in symbol else symbol
            lines = list(conf.get("missing") or []) + list(conf.get("mismatch") or [])
            file_missing = any(
                (file_path in line or base in line)
                and (f"`{symbol}`" in line or f"`{cls}`" in line)
                for line in lines
            )
            # by_file 里「文件缺失」占位列也要命中（此时没有具体符号名）。
            file_absent = names == {"文件缺失"}
            skel_bad = bool(names & {symbol, cls}) or file_missing or file_absent
        return contract_bad, skel_bad

    # ---- PO 对号入座 ----
    for po in list(graph.obligations.values()):
        ev_ids: list[str] = []
        new_status = PO_STATUS_UNPROVEN
        if po.kind == PO_KIND_INTERFACE_FREEZE:
            if verdict == "skipped":
                new_status = PO_STATUS_UNPROVEN
            else:
                file_path = str(po.verifier.get("file") or "")
                symbol = str(po.verifier.get("symbol") or "")
                c_bad, s_bad = _freeze_symbol_bad(file_path, symbol)
                if c_bad or s_bad:
                    new_status = PO_STATUS_FAILED
                    ev_ids = [e for e in (contract_ev_id, skeleton_ev_id)
                              if e and ((e == contract_ev_id and c_bad) or (e == skeleton_ev_id and s_bad))]
                elif contract_ev_id and skeleton_ev_id:
                    # 双证据都在且都过 ⇒ PROVEN；任一路没跑（id 为空）⇒ 继续 UNPROVEN。
                    new_status = PO_STATUS_PROVEN
                    ev_ids = [contract_ev_id, skeleton_ev_id]
        elif po.kind == PO_KIND_DELIVERY:
            if verdict == "pass":
                new_status = PO_STATUS_PROVEN
                ev_ids = cmd_evidence[:1] + ([mat_ev_id] if materialized else [])
            elif verdict == "fail":
                new_status = PO_STATUS_FAILED
        elif po.kind == PO_KIND_MATERIALIZATION:
            if materialized and not mat_failed:
                new_status = PO_STATUS_PROVEN
                ev_ids = [mat_ev_id]
            elif verdict == "fail" or mat_failed:
                new_status = PO_STATUS_FAILED
        elif po.kind in (PO_KIND_INTERFACE, PO_KIND_CONTRACT):
            bad = contracts if po.kind == PO_KIND_CONTRACT else [str(x) for x in missing_symbols]
            if verdict == "skipped":
                new_status = PO_STATUS_UNPROVEN
            elif bad:
                new_status = PO_STATUS_FAILED
            else:
                new_status = PO_STATUS_PROVEN
                ev_ids = [mat_ev_id] if materialized else cmd_evidence[:1]
        elif po.kind in (PO_KIND_SYNTAX, PO_KIND_IMPORT):
            if verdict == "skipped":
                new_status = PO_STATUS_UNPROVEN
            elif missing_symbols:
                new_status = PO_STATUS_FAILED
            elif verdict in ("pass", "fail"):
                # 命令真跑过且无缺失符号：语法/可导入性由执行本身机械证明
                new_status = PO_STATUS_PROVEN if cmd_evidence else PO_STATUS_UNPROVEN
                ev_ids = cmd_evidence[:1]
        else:
            # behavior / command / regression / invariant
            if has_explicit_bindings:
                # 严格路径（方案§三 3.3）：只认显式绑定到本 PO、kind 匹配、revision 一致的证据，
                # 任何未绑定给它的断言通过都不能串证（含 rc==0 的入口冒烟 command_result）。
                proven_evs = [
                    eid for eid in cmd_evidence
                    if evidence_proves_po(graph.evidence[eid], po, head_revision=rev_id)
                ]
                failed_evs = [
                    eid for eid in cmd_evidence
                    if evidence_fails_po(graph.evidence[eid], po, head_revision=rev_id)
                ]
                if proven_evs:
                    new_status = PO_STATUS_PROVEN
                    ev_ids = proven_evs
                elif failed_evs:
                    new_status = PO_STATUS_FAILED
                    ev_ids = failed_evs
                else:
                    # 跑了但没有绑定给本 PO 的匹配证据（含只跑入口冒烟/断言未覆盖）⇒ UNPROVEN
                    new_status = PO_STATUS_UNPROVEN
            elif verdict == "pass" and assertion_evidence:
                # 兼容路径：调用方未提供任何绑定信息时保留旧行为（旧 smoke/旧 run 依赖）
                new_status = PO_STATUS_PROVEN
                ev_ids = assertion_evidence
            elif verdict == "pass" and cmd_evidence and not assertion_evidence:
                # 只有入口冒烟、没有断言：行为义务仍未证明（真机 121404：31 用例全 regression）
                new_status = PO_STATUS_UNPROVEN
            elif verdict == "fail":
                new_status = PO_STATUS_FAILED
                ev_ids = [e for e in cmd_evidence
                          if graph.evidence.get(e) and graph.evidence[e].status == PO_STATUS_FAILED]
        if nc_ev_ids and po.required and new_status == PO_STATUS_PROVEN and no_power:
            new_status = PO_STATUS_UNPROVEN
        po.status = new_status
        po.evidence_ids = ev_ids
        for eid in ev_ids:
            ev = graph.evidence.get(eid)
            if ev is not None and po.id not in ev.proof_obligation_ids:
                ev.proof_obligation_ids.append(po.id)
        # Claim ↔ Evidence 的 proves 关系（Claim id 与 PO 同 slug，前缀不同）。
        if new_status == PO_STATUS_PROVEN and po.id.startswith("po:"):
            claim_id = "claim:" + po.id[3:]
            if graph.get(claim_id) is not None:
                for eid in ev_ids:
                    graph.relate(eid, "proves", claim_id,
                                 truth=TRUTH_PROVEN, evidence=[eid])
    return graph


# --------------------------------------------------------------------- Defect 投影（规格§二十四）
def defect_id(key: str) -> str:
    """缺陷台账稳定键（diagnose.defect_key v2）→ 语义对象 id。"""
    return "defect:" + stable_hash(str(key or ""), length=12)


def project_defect(
    graph: "OntologyGraph",
    *,
    key: str,
    row: Any = None,
    task_id: str = "",
    po_ids: Iterable[str] = (),
    evidence_ids: Iterable[str] = (),
    status: str = "OPEN",
) -> str:
    """把一条 Defect Ledger 行投影成语义图上的 Defect（DERIVED，不重写台账）。

    关系只挂**已存在**的端点（不造悬空）：
      * ``affects_task`` —— 由 resolve_bug_task 机械解析出的语义任务；
      * ``violates``     —— 已失败的 ProofObligation；
      * 证据链通过 EvidenceRecord.proof_obligation_ids / defects 字段另挂。
    幂等：同 key 再投覆盖对象本身，关系不重复添加。
    """
    row = row if isinstance(row, dict) else {}
    did = defect_id(key)
    if graph.get(did) is None:
        graph.add(SemanticObject(
            id=did, type=TYPE_DEFECT, truth=TRUTH_DERIVED,
            status=str(status or "OPEN"),
            payload={
                "ledger_key": str(key or ""),
                "kind": str(row.get("kind") or ""),
                "path": str(row.get("path") or ""),
                "symbol": str(row.get("symbol") or ""),
                "what": str(row.get("what") or ""),
                "where": str(row.get("where") or ""),
                "status_row": str(row.get("status") or ""),
                "evidence_ids": [str(x) for x in (evidence_ids or [])],
            },
            provenance=[Provenance(source="defect_ledger", stage="review")],
        ))
        existing: set[tuple[str, str]] = set()
    else:
        obj = graph.get(did)
        obj.status = str(status or "OPEN")
        existing = {(r.predicate, r.object) for r in graph.relations if r.subject == did}
    if task_id and graph.get(str(task_id)) is not None \
            and ("affects_task", str(task_id)) not in existing:
        graph.relate(did, "affects_task", str(task_id), truth=TRUTH_DERIVED)
    for po_id in po_ids:
        pid = str(po_id)
        if pid in graph.obligations and ("violates", pid) not in existing:
            graph.relate(did, "violates", pid, truth=TRUTH_DERIVED)
    return did


def failure_id(round_no: Any, failure_type: str, key: str = "") -> str:
    """Failure 身份：轮次 + 9 类闭集类别 + 可选缺陷键（稳定可序列化）。"""
    return "fail:" + stable_hash(
        canonical_json([int(round_no or 0), str(failure_type or ""), str(key or "")]),
        length=12,
    )


def project_failure(
    graph: "OntologyGraph",
    *,
    fid: str,
    failure: dict,
    round_no: int = 0,
    defect_ids: Iterable[str] = (),
    artifact_id: str = "",
    recovery_id: str = "",
) -> str:
    """把 diagnose.classify() 输出的 ``failure`` 三元组投影为 Failure 对象（DERIVED）。

    ``failure`` 至少含 ``type/owner_role/recover_stage``（空 type 拒绝建对象）。
    只挂已存在端点：``raises``→Defect、``caused_by``→Artifact、``triggers``→Recovery。
    """
    failure = failure if isinstance(failure, dict) else {}
    ftype = str(failure.get("type") or "")
    if not ftype:
        return ""
    if graph.get(fid) is None:
        graph.add(SemanticObject(
            id=fid, type=TYPE_FAILURE, truth=TRUTH_DERIVED,
            payload={
                "failure_type": ftype,
                "owner_role": str(failure.get("owner_role") or ""),
                "recover_stage": str(failure.get("recover_stage") or ""),
                "round": int(round_no or 0),
            },
            provenance=[Provenance(source="diagnose.classify", stage="review")],
        ))
    existing = {(r.predicate, r.object) for r in graph.relations if r.subject == fid}
    for did in defect_ids:
        d = str(did)
        if graph.get(d) is not None and ("raises", d) not in existing:
            graph.relate(fid, "raises", d, truth=TRUTH_DERIVED)
    if artifact_id and graph.get(str(artifact_id)) is not None \
            and ("caused_by", str(artifact_id)) not in existing:
        graph.relate(fid, "caused_by", str(artifact_id), truth=TRUTH_DERIVED)
    if recovery_id and graph.get(str(recovery_id)) is not None \
            and ("triggers", str(recovery_id)) not in existing:
        graph.relate(fid, "triggers", str(recovery_id), truth=TRUTH_DERIVED)
    return fid


def project_recovery(graph: "OntologyGraph", *, rid: str, recovery: dict, round_no: int = 0) -> str:
    """把 classify() 的 recovery 策略投影为 Recovery 对象（DERIVED）。

    ``returns_to`` 是 Stage 而非对象端点，按白名单语义放 payload，不造对象。
    """
    recovery = recovery if isinstance(recovery, dict) else {}
    if graph.get(rid) is None:
        graph.add(SemanticObject(
            id=rid, type=TYPE_RECOVERY, truth=TRUTH_DERIVED,
            payload={
                "action": str(recovery.get("action") or "retry_owner"),
                "reason": str(recovery.get("reason") or ""),
                "streak": int(recovery.get("streak") or 0),
                "round": int(round_no or 0),
            },
            provenance=[Provenance(source="diagnose.classify", stage="review")],
        ))
    return rid


def trace_recovery_owner(graph: "OntologyGraph", fid: str) -> dict:
    """规格§二十七：沿 ``Failure.raises→Defect`` 与 ``Defect→...→源头`` 回溯责任。

    只沿**图上真实存在**的关系走，找不到的层级如实留空（不猜角色）：
      * Failure.payload.owner_role —— taxonomy 机械给出的责任角色；
      * defects   —— 本 Failure 引发的缺陷；
      * tasks     —— 经 ``affects_task`` 机械解析到的施工图；
      * artifacts —— 经 ``caused_by`` 链（含 ``derived_from`` 闭包）到达的产物；
      * chain     —— 扁平路径，供审计。
    """
    obj = graph.get(str(fid))
    if obj is None or obj.type != TYPE_FAILURE:
        return {"owner_role": "", "defects": [], "tasks": [], "artifacts": [], "chain": []}

    out: dict[str, list] = {}
    for rel in graph.relations:
        out.setdefault(rel.subject, []).append((rel.predicate, rel.object))

    chain: list[str] = []
    defects, tasks, artifacts = [], [], []

    def _walk_caused_by(start: str) -> None:
        for pred, target in out.get(start, []):
            if pred == "caused_by" and target not in artifacts:
                artifacts.append(target)
                chain.append(f"{start} -caused_by-> {target}")
                # 产物继续沿 derived_from 上溯到 Plan/Requirement 等源头
                frontier = [target]
                seen: set[str] = set()
                while frontier:
                    cur = frontier.pop()
                    for p2, t2 in out.get(cur, []):
                        if p2 == "derived_from" and t2 not in seen:
                            seen.add(t2)
                            chain.append(f"{cur} -derived_from-> {t2}")
                            node = graph.get(t2)
                            if node is not None and node.type == TYPE_ARTIFACT:
                                artifacts.append(t2)
                                frontier.append(t2)

    for pred, target in out.get(str(fid), []):
        if pred != "raises":
            continue
        defects.append(target)
        chain.append(f"{fid} -raises-> {target}")
        for p2, t2 in out.get(target, []):
            if p2 == "affects_task" and t2 not in tasks:
                tasks.append(t2)
                chain.append(f"{target} -affects_task-> {t2}")
            elif p2 == "caused_by":
                _walk_caused_by(target)

    return {
        "owner_role": str(obj.payload.get("owner_role") or ""),
        "recover_stage": str(obj.payload.get("recover_stage") or ""),
        "defects": defects,
        "tasks": tasks,
        "artifacts": artifacts,
        "chain": chain,
    }


# --------------------------------------------------------------------- 事实优先级 / Claim 仲裁（规格§三十三）

#: 事实来源闭集与优先级：USER > HUMAN > PM > ARCHITECT > TEST > DEV_SELF_CHECK。
FACT_SOURCE_USER = "user"
FACT_SOURCE_HUMAN = "human"
FACT_SOURCE_PM = "pm"
FACT_SOURCE_ARCHITECT = "architect"
FACT_SOURCE_TEST = "test"
FACT_SOURCE_DEV_SELF_CHECK = "dev_self_check"

FACT_PRIORITY: dict[str, int] = {
    FACT_SOURCE_USER: 60,
    FACT_SOURCE_HUMAN: 50,
    FACT_SOURCE_PM: 40,
    FACT_SOURCE_ARCHITECT: 30,
    FACT_SOURCE_TEST: 20,
    FACT_SOURCE_DEV_SELF_CHECK: 10,
}

#: 同主体对立极性的仲裁结论：高优先级覆盖低优先级 / 同优先级冲突（不猜）
RESOLUTION_ACCEPTED = "accepted"
RESOLUTION_OVERRIDDEN = "overridden"
RESOLUTION_CONTRADICTION = "contradiction"


def _fact_rank(claim: dict[str, Any]) -> int:
    """仲裁排序键：三真值优先于角色 —— DERIVED 永远不能压过 ASSERTED。"""
    truth_rank = 1000 if str(claim.get("truth") or TRUTH_DERIVED) == TRUTH_ASSERTED else 0
    return truth_rank + FACT_PRIORITY.get(str(claim.get("source") or ""), 0)


def reconcile_claims(claims: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """规格§三十三：同主体对立 Claim 的**确定性仲裁**（纯函数，与字段出现顺序无关）。

    输入 claim：``{"id", "subject", "polarity"(1|-1), "source", "truth"?}``。
    规则（后出现的字段**永不**自动覆盖先出现的）：

    1. 按 ``subject`` 分组；极性相同即互为印证，全部接受；
    2. 极性对立时按 ``(真值, 角色优先级)`` 排序：DERIVED 不可覆盖 ASSERTED，
       低优先级被高优先级覆盖（记 overridden，不静默删除）；
    3. 最高层**同级**仍对立 ⇒ ``contradiction``，双方都保留、不猜赢家，
       调用方必须阻断交人工（CONTRADICTION）。

    无 subject 的陈述无法比较，原样接受（不强行并组）。
    """
    items = [c for c in (claims or []) if isinstance(c, dict) and c.get("id")]
    accepted: list[str] = []
    overridden: list[dict[str, str]] = []
    contradictions: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in items:
        subject = str(c.get("subject") or "")
        if not subject:
            accepted.append(str(c["id"]))
            continue
        groups.setdefault(subject, []).append(c)

    for subject, members in groups.items():
        # id 稳定排序保证结论与输入顺序无关
        members.sort(key=lambda c: str(c.get("id")))
        positives = [c for c in members if int(c.get("polarity") or 1) > 0]
        negatives = [c for c in members if int(c.get("polarity") or 1) < 0]
        if not positives or not negatives:
            accepted.extend(str(c["id"]) for c in members)
            continue
        top = max(_fact_rank(c) for c in members)
        winners = [c for c in members if _fact_rank(c) == top]
        win_pol = {1 if int(c.get("polarity") or 1) > 0 else -1 for c in winners}
        if len(win_pol) > 1:
            # 最高层里既有肯又有否：同级冲突，不猜。
            contradictions.append({
                "subject": subject,
                "ids": sorted(str(c["id"]) for c in members),
                "winner_ids": sorted(str(c["id"]) for c in winners),
                "sources": sorted({str(c.get("source") or "") for c in winners}),
                "reason": "same_level_conflict",
            })
            accepted.extend(str(c["id"]) for c in members)
        else:
            winner = winners[0]
            accepted.append(str(winner["id"]))
            for c in members:
                if c["id"] == winner["id"]:
                    continue
                overridden.append({
                    "id": str(c["id"]),
                    "subject": subject,
                    "by": str(winner["id"]),
                    "reason": "higher_priority_fact"
                    if _fact_rank(c) < top else "lower_polarity_tie",
                })
    return {
        "accepted": sorted(set(accepted)),
        "overridden": overridden,
        "contradictions": contradictions,
    }


# --------------------------------------------------------------------- Intake/PM 语义投影（规格§三十四）
def _intake_claim_id(truth: str, subject: str) -> str:
    return "claim:inh:" + ("A" if truth == TRUTH_ASSERTED else "D") + ":" \
        + stable_hash(subject, length=10)


def project_intake(
    graph: "OntologyGraph",
    *,
    scope: Any = None,
    intake_rows: Iterable[dict[str, Any]] = (),
) -> list[str]:
    """规格§三十四：intake/PM 的背景与推断**全部 DERIVED** 投影为 DerivedClaim。

    * ``scope.background`` / ``target_users`` —— PM 归纳，DERIVED，``derived_from``
      原始需求；
    * intake 行的人工 ``final_decision`` —— ASSERTED（人工事实，LLM 不可覆盖）；
      只有 ``default_assumption`` 而无裁决 —— DERIVED（默认假设不是事实）。

    只挂在已存在的 ``req:root`` 上（底图先 build_requirement_projection）。
    返回新建/复用的 claim id 列表；幂等。
    """
    scope = scope if isinstance(scope, dict) else {}
    created: list[str] = []

    def _add(truth: str, source: str, subject: str, text: str, stage: str) -> None:
        subject, text = str(subject or "").strip(), str(text or "").strip()
        if not subject or not text or graph.get("req:root") is None:
            return
        cid = _intake_claim_id(truth, f"{source}:{subject}:{text}")
        if graph.get(cid) is None:
            graph.add(SemanticObject(
                id=cid, type=TYPE_CLAIM, truth=truth,
                payload={"subject": subject[:120], "text": text[:400], "source": source},
                provenance=[Provenance(source=source, stage=stage)],
                derived_from=["req:root"],
            ))
            graph.relate(cid, "claim_derives", "req:root", truth=truth)
        created.append(cid)

    if str(scope.get("background") or "").strip():
        _add(TRUTH_DERIVED, FACT_SOURCE_PM, "background",
             str(scope["background"]), "pm")
    for i, user in enumerate(scope.get("target_users") or []):
        _add(TRUTH_DERIVED, FACT_SOURCE_PM, f"target_users[{i}]", str(user or ""), "pm")
    for row in intake_rows:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject") or row.get("element") or "").strip()
        decision = str(row.get("final_decision") or "").strip()
        default = str(row.get("default_assumption") or "").strip()
        if decision:
            _add(TRUTH_ASSERTED, FACT_SOURCE_HUMAN, subject, decision, "intake_decision")
        elif default:
            _add(TRUTH_DERIVED, FACT_SOURCE_PM, subject, default, "intake")
    return created


# --------------------------------------------------------------------- Requirement ↔ Plan 单元匹配

#: 与行为审计（orchestrator._behavior_terms）同口径但独立实现，避免跨层依赖。
_LINK_RATIO = 0.35
_LINK_MIN_SHARED = 2


def _link_terms(text: str) -> set[str]:
    """需求/施工文本 → 词项集合：ascii 标识符词（≥3）+ 中文 bigram。"""
    text = str(text or "").lower()
    terms = {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", text) if not w.isdigit()}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    terms.update(cjk[i:i + 2] for i in range(max(0, len(cjk) - 1)))
    return terms


def requirement_unit_links(scope: Any, units: Iterable[Any]) -> dict[str, Any]:
    """规格§十三：把 PM 的 Requirement/Claim 确定性挂到 Plan IR 单元（按文件聚合）。

    纯字面匹配（ascii 词 + CJK bigram，共享比 ≥0.35 且 ≥2 项），**不靠模型、不猜**：
    达不到阈值就是没挂上 —— 暴露为 ``unlinked_requirements`` 交给 Design Gate，
    而不是偷偷挂到第一个文件上。返回::

        {
          "by_file": {"db.py": {"requirements": ["req:FR-01"],
                                "proof_obligations": ["po:FR-01:..."],
                                "claims": ["claim:FR-01:..."]}},
          "unlinked_requirements": ["req:FR-02"],
          "unlinked_files": ["util.py"],
        }
    """
    scope = scope if isinstance(scope, dict) else {}
    claims = requirement_claims(scope)  # [{req_id, text}]
    unit_list = [u for u in units if isinstance(u, dict)]

    def _unit_terms(u: dict) -> set[str]:
        blob_parts: list[str] = [str(u.get("file") or "")]
        blob_parts.extend(str(s) for s in (u.get("symbols") or []))
        blob_parts.append(str(u.get("change") or ""))
        blob_parts.append(str(u.get("interface") or ""))
        for it in (u.get("acceptance") or []):
            if isinstance(it, dict):
                blob_parts.append(str(it.get("text") or ""))
            else:
                blob_parts.append(str(it))
        for it in (u.get("intent") or []):
            if isinstance(it, dict):
                blob_parts.append(str(it.get("text") or ""))
            else:
                blob_parts.append(str(it))
        return _link_terms(" ".join(blob_parts))

    unit_terms = [(str(u.get("file") or ""), _unit_terms(u)) for u in unit_list]
    by_file: dict[str, dict[str, list[str]]] = {}
    unlinked_requirements: set[str] = set()

    # 同一 req 的多条 claim 分别匹配；req 只要有一条 claim 命中即视为该单元实现它。
    req_hit: dict[str, dict[str, set[str]]] = {}
    for item in claims:
        rid = item["req_id"] or f"AC-{stable_hash(item['text'], length=6)}"
        req_id = f"req:{rid}"
        q = _link_terms(item["text"])
        if not q:
            continue
        hit_any = False
        for file_path, uterms in unit_terms:
            if not file_path:
                continue
            shared = q & uterms
            if len(shared) >= _LINK_MIN_SHARED and len(shared) / max(1, min(len(q), len(uterms))) >= _LINK_RATIO:
                slot = req_hit.setdefault(file_path, {"requirements": set(), "claims": set()})
                slot["requirements"].add(req_id)
                slot["claims"].add(f"claim:{rid}:{_slug(item['text'])}")
                hit_any = True
        if not hit_any:
            unlinked_requirements.add(req_id)

    for file_path, sets in req_hit.items():
        po_ids: list[str] = []
        for claim_id in sorted(sets["claims"]):
            # PO id 与 claim 同 slug（见 build_requirement_projection）。
            po_ids.append("po:" + claim_id[len("claim:"):])
        by_file[file_path] = {
            "requirements": sorted(sets["requirements"]),
            "claims": sorted(sets["claims"]),
            "proof_obligations": sorted(po_ids),
        }
    linked_files = set(by_file)
    unlinked_files = sorted(f for f, _ in unit_terms if f and f not in linked_files)
    return {
        "by_file": by_file,
        "unlinked_requirements": sorted(unlinked_requirements),
        "unlinked_files": unlinked_files,
    }


# --------------------------------------------------------------------- Interface Freeze（规格§十八）

#: 与 verify.skeleton_conformance **同一字面口径**解析冻结骨架行（此处只取名、不核签名）。
_FREEZE_CLASS_RE = re.compile(r"^class\s+(\w+)")
_FREEZE_METHOD_RE = re.compile(r"^\s{4}def\s+(\w+)\s*\(")
_FREEZE_FUNC_RE = re.compile(r"^def\s+(\w+)\s*\(")


def _skeleton_declarations(skeleton: Any) -> dict[str, list[tuple[str, str]]]:
    """把冻结骨架归一成 ``{文件: [(全限定符号, 粒度), ...]}``（class/method/func）。

    接受两种既有形态：摘要态 ``{path: [声明行]}`` 与文件态
    ``{"files": [{"path": ..., "symbols"?: [...]}]}``。纯字符串解析，不读文件。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not isinstance(skeleton, dict):
        return out
    rows: list[tuple[str, list[str]]] = []
    files = skeleton.get("files")
    if isinstance(files, list):
        for entry in files:
            if isinstance(entry, dict) and str(entry.get("path") or "").strip():
                lines = entry.get("symbols") or entry.get("declarations") or []
                rows.append((str(entry["path"]).replace("\\", "/"),
                             [str(x) for x in lines] if isinstance(lines, list) else []))
    for rel, lines in skeleton.items():
        if rel == "files" or not isinstance(lines, list):
            continue
        rows.append((str(rel).replace("\\", "/"), [str(x) for x in lines]))
    for rel, lines in rows:
        decls: list[tuple[str, str]] = []
        cur_class = ""
        for line in lines:
            m = _FREEZE_CLASS_RE.match(line.strip())
            if m and not line.startswith(" "):
                cur_class = m.group(1)
                decls.append((cur_class, "class"))
                continue
            m = _FREEZE_METHOD_RE.match(line)
            if m and cur_class:
                decls.append((f"{cur_class}.{m.group(1)}", "method"))
                continue
            m = _FREEZE_FUNC_RE.match(line)
            if m:
                decls.append((m.group(1), "func"))
        if decls:
            out[rel] = decls
    return out


def freeze_symbols(skeleton: Any, plan_file_symbols: dict[str, Iterable[str]]) -> list[dict[str, str]]:
    """规格§十八：求「冻结接口」集合 = 骨架声明 ∩ 方案承诺（纯函数，不猜）。

    只有**方案自己也承诺了**的符号才冻结 —— 骨架与方案是两次独立 LLM 输出，
    只在骨架出现、方案没承诺的符号不产义务（保留既有「只记录不判负」口径，避免把
    两次输出不一致误判成实现缺陷）。判定（确定性）：

      * 类 ``C``：方案符号含 ``C`` 或任一 ``C.x``；
      * 方法 ``C.m``：方案符号含 ``C.m`` 或含其属主 ``C``；
      * 模块函数 ``f``：方案符号含 ``f``。
    """
    plan_sets = {
        str(f).replace("\\", "/"): {str(s) for s in (syms or ())}
        for f, syms in (plan_file_symbols or {}).items()
    }
    frozen: list[dict[str, str]] = []
    for rel, decls in _skeleton_declarations(skeleton).items():
        committed = plan_sets.get(rel)
        if not committed:
            continue
        for symbol, granularity in decls:
            ok = symbol in committed
            if not ok and granularity == "class":
                ok = any(s == symbol or s.startswith(symbol + ".") for s in committed)
            if not ok and granularity == "method" and "." in symbol:
                ok = symbol.split(".", 1)[0] in committed
            if ok:
                frozen.append({"file": rel, "symbol": symbol, "granularity": granularity})
    frozen.sort(key=lambda x: (x["file"], x["symbol"]))
    return frozen


def project_interface_freeze(graph: "OntologyGraph", frozen: Iterable[dict[str, str]]) -> list[str]:
    """把冻结符号投影为 ``interface_freeze`` 双证据 PO（幂等，纯函数）。

    每个冻结符号一对 Claim(DERIVED, architect) → ProofObligation(required)，
    PO 的 verifier 声明 requires=[contract_check, skeleton_conformance]；PROVEN
    判定见 :func:`evaluate_against_verify`。返回创建/已存在的 PO id 列表。
    """
    prov = Provenance(source="architect", stage="architect_plan")
    po_ids: list[str] = []
    for item in frozen:
        file_path = str(item.get("file") or "").replace("\\", "/")
        symbol = str(item.get("symbol") or "")
        if not file_path or not symbol:
            continue
        h = stable_hash([file_path, symbol], length=12)
        claim_id = f"claim:iface:{h}"
        po_id = f"po:iface:{h}"
        text = f"冻结接口 {file_path}::{symbol} 与方案契约、实际产物三者一致"
        if graph.get(claim_id) is None:
            graph.add(SemanticObject(
                id=claim_id, type=TYPE_CLAIM, truth=TRUTH_DERIVED,
                payload={"text": text, "file": file_path, "symbol": symbol},
                provenance=[prov],
            ))
        if po_id not in graph.obligations:
            graph.add_obligation(ProofObligation(
                id=po_id, name=f"InterfaceFreeze {file_path}::{symbol}",
                requirement_id="", claim=claim_id, kind=PO_KIND_INTERFACE_FREEZE,
                verifier=_verifier_for_kind(PO_KIND_INTERFACE_FREEZE),
            ))
            # file/symbol 放 verifier 里随 PO 持久化（ProofObligation 无独立 payload 字段）。
            graph.obligations[po_id].verifier["file"] = file_path
            graph.obligations[po_id].verifier["symbol"] = symbol
        # Claim satisfies PO：两个一等端点，类型规则已登记。重复投影不造重复边
        # （relate 本身是只追加的，幂等性由投影函数按 (s,p,o) 三元组去重保证）。
        if not any(r.subject == claim_id and r.predicate == "claim_satisfies"
                   and r.object == po_id for r in graph.relations):
            graph.relate(claim_id, "claim_satisfies", po_id, truth=TRUTH_DERIVED)
        po_ids.append(po_id)
    return po_ids


# --------------------------------------------------------------------- Task→Patch→WorkspaceRevision 投影（方案§十九~§二十四）

#: 任务事务产生的工作区 revision 状态。
WSR_STATUS_BASE = "BASE"
WSR_STATUS_COMMITTED = "COMMITTED"
WSR_STATUS_WIP = "WIP"


def workspace_symbol_id(file_path: str, name: str) -> str:
    """Symbol 的确定性语义身份（方案§二十一：必须来自确定性解析，禁止 LLM 猜）。

    身份只依赖 ``(文件相对路径, 符号名)``，与措辞/轮次无关。
    """
    return "sym:" + stable_hash(
        [str(file_path or "").replace("\\", "/"), str(name or "")], length=12
    )


def _relate_once(graph: "OntologyGraph", subject: str, predicate: str, obj: str,
                 *, truth: str = TRUTH_DERIVED) -> bool:
    """(subject,predicate,object) 去重后再 relate —— relate 本身只 append。

    返回 True 表示本次确实新增了一条边（供投影计数）。
    """
    if any(r.subject == subject and r.predicate == predicate and r.object == obj
           for r in graph.relations):
        return False
    graph.relate(subject, predicate, obj, truth=truth)
    return True


def project_workspace_chain(
    graph: "OntologyGraph",
    transactions: Iterable[dict[str, Any]],
    *,
    at: str = "",
) -> dict[str, Any]:
    """方案§十九~§二十四：把 task transactions 投影为
    ``Task → Patch → Symbol / Patch materialized_in WorkspaceRevision`` 真实父子链。

    输入事务（orchestrator ``state["task_transactions"]`` 的行，字段全部缺省安全）：
    ``task_semantic_id / patch_id / base_manifest / parent_workspace_revision /
    result_workspace_revision / status / round / patches``，其中 patches 为**真正物化
    成功**的 ``[{path, symbol, change_type, patch_mode}]``。

    纪律：纯函数、幂等（对象按 id 覆盖同数据、关系三元组去重）；只追加投影、不裁决；
    parent 只指向图上**已存在**的 revision（旧事务的复合 base 串不可用作 id，宁断不造
    悬空端点）。返回投影计数与链式 head（供 verify revision 挂父）。
    """
    prov = Provenance(source="taskcompiler", stage="task_transaction")
    revision_count = patch_count = symbol_count = 0
    head = ""
    head_round: Any = None
    base_added: set[str] = set()

    for seq, txn in enumerate(transactions or []):
        if not isinstance(txn, dict):
            continue
        result_id = str(txn.get("result_workspace_revision") or "")
        round_no = txn.get("round")
        semantic_id = str(txn.get("task_semantic_id") or "")
        patch_digest = str(txn.get("patch_id") or "")

        if result_id and result_id not in graph.revisions:
            # 父 revision：优先事务里机械记录的链头；旧事务无该字段时，同轮按执行顺序
            # 接当前 head（内容寻址链的兜底，绝不引用图上不存在的 id）。
            recorded_parent = str(txn.get("parent_workspace_revision") or "")
            if recorded_parent and recorded_parent not in graph.revisions:
                recorded_parent = ""
            if not recorded_parent and head and head in graph.revisions and round_no == head_round:
                recorded_parent = head
            # 根：本轮基线内容 revision（source=base，只挂一次）。
            base_manifest = str(txn.get("base_manifest") or "")
            if not recorded_parent and base_manifest and base_manifest not in graph.revisions \
                    and base_manifest not in base_added:
                graph.add_revision(WorkspaceRevision(
                    revision_id=base_manifest, workspace_id=graph.workspace_id,
                    source="base", manifest_digest=base_manifest,
                    created_at=at, status=WSR_STATUS_BASE,
                ))
                base_added.add(base_manifest)
                if graph.get("ws:main") is None:
                    graph.add(SemanticObject(id="ws:main", type=TYPE_WORKSPACE,
                                             payload={"workspace_id": graph.workspace_id}))
                _relate_once(graph, "ws:main", "has_revision", base_manifest)
                if not recorded_parent:
                    recorded_parent = base_manifest
            status = WSR_STATUS_COMMITTED if str(txn.get("status") or "") == "committed" \
                else WSR_STATUS_WIP
            graph.add_revision(WorkspaceRevision(
                revision_id=result_id, workspace_id=graph.workspace_id,
                parent_revision=recorded_parent,
                source="task", task_id=semantic_id,
                patch_digest=patch_digest,
                manifest_digest=result_id,
                created_at=str(txn.get("at") or at), status=status,
            ))
            if graph.get("ws:main") is None:
                graph.add(SemanticObject(id="ws:main", type=TYPE_WORKSPACE,
                                         payload={"workspace_id": graph.workspace_id}))
            _relate_once(graph, "ws:main", "has_revision", result_id)
            revision_count += 1

        # ---- Patch / Symbol（只给真正物化成功的文件；方案§二十二）----
        if result_id and result_id in graph.revisions:
            task_obj = graph.objects.get(semantic_id) if semantic_id else None
            task_files = set()
            if task_obj is not None:
                task_files = {
                    str(p).replace("\\", "/")
                    for p in (task_obj.payload.get("target_files") or [])
                }
            for entry in (txn.get("patches") or []):
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").replace("\\", "/")
                if not path:
                    continue
                pid = "patch:" + stable_hash(
                    [patch_digest or semantic_id or str(txn.get("task") or ""),
                     str(round_no), seq, path], length=12)
                if graph.get(pid) is None:
                    graph.add(SemanticObject(
                        id=pid, type=TYPE_PATCH, truth=TRUTH_DERIVED,
                        payload={
                            "patch_digest": patch_digest,
                            "task_semantic_id": semantic_id,
                            "task_id": semantic_id,  # validate_tasks 的越权判据别名
                            "path": path,
                            "change_type": str(entry.get("change_type") or ""),
                            "patch_mode": str(entry.get("patch_mode") or ""),
                        },
                        provenance=[prov],
                    ))
                    patch_count += 1
                _relate_once(graph, pid, "materialized_in", result_id)
                symbol_name = str(entry.get("symbol") or "")
                if symbol_name:
                    sid = workspace_symbol_id(path, symbol_name)
                    if graph.get(sid) is None:
                        graph.add(SemanticObject(
                            id=sid, type=TYPE_SYMBOL, truth=TRUTH_DERIVED,
                            payload={"name": symbol_name, "file": path, "path": path},
                            provenance=[prov],
                        ))
                        symbol_count += 1
                    _relate_once(graph, pid, "changes", sid)
                    # owns 只在符号文件属于该 Task 目标面时挂（租约口径，避免误报越权）。
                    if task_obj is not None and (not task_files or path in task_files):
                        _relate_once(graph, semantic_id, "owns", sid)

        if result_id:
            head = result_id
            head_round = round_no

    return {
        "revisions": revision_count,
        "patches": patch_count,
        "symbols": symbol_count,
        "head_revision": head,
    }


def project_artifact_chain(graph: "OntologyGraph", envelopes: Iterable[dict[str, Any]]) -> dict[str, int]:
    """方案§三十/§三十一：artifact envelope → Artifact 一等对象 + provenance 边。

    **不另造一套 artifact hash**：直接消费编排器 ``_record`` 已算好的 envelope
    （artifact_id / stage / artifact_revision / supersedes / input_hash /
    output_hash / caused_by / truth）。关系只挂已存在端点：

    * ``derived_from`` —— envelope 的 ``caused_by`` 直接上游（intake→pm→plan→…→verify）；
    * ``supersedes``   —— 同阶段上一版（旧版不删，replay 可还原「当时基于哪个 Plan」）。

    两遍处理（先建对象再挂边），幂等。返回新增计数
    ``{"artifacts", "derived_from", "supersedes"}``。
    """
    rows = [e for e in (envelopes or []) if isinstance(e, dict) and str(e.get("artifact_id") or "")]
    count = {"artifacts": 0, "derived_from": 0, "supersedes": 0}
    for env in rows:
        aid = str(env["artifact_id"])
        if graph.get(aid) is not None:
            continue
        stage_name = str(env.get("stage") or "")
        # 人工闸门产物携带人工事实（ASSERTED 必须有 user/human provenance，见校验器）。
        source = "human" if stage_name == "human_review" else str(
            env.get("produced_by") or stage_name or "pipeline"
        )
        try:
            revision = int(env.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        graph.add(SemanticObject(
            id=aid, type=TYPE_ARTIFACT,
            truth=str(env.get("truth") or TRUTH_DERIVED),
            payload={
                "stage": str(env.get("stage") or ""),
                "artifact_revision": revision,
                "input_hash": str(env.get("input_hash") or ""),
                "output_hash": str(env.get("output_hash") or ""),
                "created_at": str(env.get("created_at") or ""),
            },
            provenance=[Provenance(source=source, stage=str(env.get("stage") or ""))],
        ))
        count["artifacts"] += 1
    for env in rows:
        aid = str(env["artifact_id"])
        for up in (env.get("caused_by") or []):
            up = str(up or "")
            if up and graph.get(up) is not None and _relate_once(
                graph, aid, "derived_from", up,
                truth=str(env.get("truth") or TRUTH_DERIVED),
            ):
                count["derived_from"] += 1
        for old in (env.get("supersedes") or []):
            old = str(old or "")
            if old and graph.get(old) is not None and _relate_once(
                graph, aid, "supersedes", old, truth=TRUTH_DERIVED
            ):
                count["supersedes"] += 1
    return count


def project_conflict_claims(
    graph: "OntologyGraph", groups: Iterable[Iterable[dict[str, Any]]]
) -> int:
    """方案§三十二/§三十三：把机械发现的极性冲突组投影为带 subject/polarity 的 Claim。

    入参为 :func:`prompts.claim_conflict_groups` 的产物（每组 ≥2 条事实，组内已经过
    「同话题 + 极性翻转」严判据连通）。组内事实共享一个内容派生的 ``subject``，
    随后由 ``validate_contradictions_structured`` 复用 ``reconcile_claims`` 仲裁：
    同级对立 ⇒ ``claim_contradiction``（硬阻断）；不同事实等级 ⇒ 高者覆盖，不报错。

    Claim 身份由 (subject, text, source) 稳定决定，跨轮重投幂等。返回新建 Claim 数。
    """
    created = 0
    for group in (groups or []):
        members = [f for f in group if isinstance(f, dict) and str(f.get("text") or "").strip()]
        if len(members) < 2:
            continue
        subject = "conflict:" + stable_hash(
            sorted(str(m.get("text") or "") for m in members), length=12
        )
        for fact in members:
            try:
                polarity = int(fact.get("polarity") or 0)
            except (TypeError, ValueError):
                polarity = 0
            if polarity not in (-1, 1):
                continue
            text = str(fact.get("text") or "")
            source = str(fact.get("source") or "pm")
            cid = "claim:conflict:" + stable_hash([subject, text, source], length=12)
            if graph.get(cid) is not None:
                continue
            truth = TRUTH_ASSERTED if str(fact.get("truth") or "").upper() == "ASSERTED" else TRUTH_DERIVED
            graph.add(SemanticObject(
                id=cid, type=TYPE_CLAIM, truth=truth,
                payload={
                    "subject": subject,
                    "polarity": polarity,
                    "text": text[:300],
                    "where": str(fact.get("where") or ""),
                },
                provenance=[Provenance(
                    source=source, stage="intake_decision" if source == "human" else "pm"
                )],
            ))
            created += 1
    return created


# --------------------------------------------------------------------- Schema Guardian（规格§五十八）

def schema_self_check() -> list[str]:
    """启动期本体登记表自洽校验（纯函数，空列表 = 通过）。

    被 ``flow.validate()`` 在启动时调用 —— 漏登记 predicate 类型规则 / PO verifier /
    证据 kind 时**启动就报错**，不等真机跑到第 6 轮。只查「表与表之间」的一致性：

      1. 版本号非空；
      2. 类型规则里出现的 predicate 必须在白名单（unknown relation）；
      3. 规则引用的端点类型必须在 OBJECT_TYPES（unknown artifact/object type）；
      4. 每个 PROOF_KIND 必须能从 ``_verifier_for_kind`` 拿到非空 verifier
         （required PO without verifier 在登记层就不可能出现）；
      5. 双证据 verifier 的 requires 项必须各自存在可落地的机械证据/检查口径；
      6. 证据 kind 三个集合不重不漏（ASSERTED/EXEC 必在 KINDS，且互斥）；
      7. 每条内置 Invariant 绑定都带 check_id + entry（unknown proof verifier）。
    """
    problems: list[str] = []
    if not str(ONTOLOGY_SCHEMA_VERSION or "").strip():
        problems.append("ONTOLOGY_SCHEMA_VERSION 为空")
    if not str(ONTOLOGY_RULES_VERSION or "").strip():
        problems.append("ONTOLOGY_RULES_VERSION 为空")

    for predicate, rule in _PREDICATE_TYPE_RULES.items():
        if predicate not in PREDICATES:
            problems.append(f"predicate 类型规则登记了未知关系 {predicate!r}（不在 PREDICATES 白名单）")
        for side in (_SUBJECT_TYPES, _OBJECT_TYPES):
            unknown = sorted(t for t in rule.get(side, frozenset()) if t not in OBJECT_TYPES)
            if unknown:
                problems.append(f"predicate {predicate} 的{('主语' if side == _SUBJECT_TYPES else '宾语')}"
                                f"类型含未知对象类型 {unknown}")

    for kind in sorted(PROOF_KINDS):
        verifier = _verifier_for_kind(kind)
        if not isinstance(verifier, dict) or not verifier:
            problems.append(f"PO kind {kind} 没有绑定任何 verifier（required PO 将无法机械兑现）")
            continue
        if verifier.get("type") == "dual_evidence":
            requires = verifier.get("requires") or []
            if len(requires) < 2:
                problems.append(f"PO kind {kind} 的 dual_evidence 至少声明两个 requires 检查")
            for check_id in requires:
                # contract_check 对应证据 kind=contract；其余按 check_id 同名核对。
                evidence_kind = "contract" if check_id == "contract_check" else check_id
                if evidence_kind not in EVIDENCE_CHECKER_KINDS:
                    problems.append(
                        f"PO kind {kind} requires 的检查 {check_id} 没有登记机械证据 kind"
                    )

    if not (EVIDENCE_ASSERTED_KINDS <= EVIDENCE_KINDS):
        problems.append("EVIDENCE_ASSERTED_KINDS 必须是 EVIDENCE_KINDS 的子集")
    if not (EVIDENCE_EXEC_KINDS <= EVIDENCE_KINDS):
        problems.append("EVIDENCE_EXEC_KINDS 必须是 EVIDENCE_KINDS 的子集")
    if not (EVIDENCE_CHECKER_KINDS <= EVIDENCE_KINDS):
        problems.append("EVIDENCE_CHECKER_KINDS 必须是 EVIDENCE_KINDS 的子集")
    if EVIDENCE_ASSERTED_KINDS & (EVIDENCE_EXEC_KINDS | EVIDENCE_CHECKER_KINDS):
        problems.append("人工 ASSERTED 证据与机械执行/检查证据 kind 不得重叠")

    for inv_id, binding in INVARIANT_EXECUTABLE_CHECKS.items():
        if not str(binding.get("check_id") or "").strip():
            problems.append(f"内置 Invariant {inv_id} 的绑定缺少 check_id")
        if not str(binding.get("entry") or "").strip():
            problems.append(f"内置 Invariant {inv_id} 的绑定缺少可执行 entry")
        if binding.get("kind") not in ("smoke", "tool", "module"):
            problems.append(f"内置 Invariant {inv_id} 的绑定 kind 非法：{binding.get('kind')!r}")
    return problems


# --------------------------------------------------------------------- Release Decision（方案§二十六~§二十八）

#: 机器放行裁决闭集：pass（可交付）/ rework（继续整改或转人工）。
DECISION_PASS = "pass"
DECISION_REWORK = "rework"


def release_basis(graph: "OntologyGraph") -> dict[str, Any]:
    """汇总放行裁决的事实基础：被验证的链头 revision + 支撑必需 PO 的证据。

    返回 ``{"revision": str, "evidence_ids": list[str]}``：

    * ``revision`` —— 只认真实执行 verify 产生（``source=="verify"``）且当前仍是
      工作区链头的 WorkspaceRevision；链头停在 WIP 时返回空串（未验证版本不得放行）；
    * ``evidence_ids`` —— 与任意 required PO 绑定、状态 PROVEN 的证据 id（稳定排序去重）。

    纯函数、缺省安全：空图 / 缺字段返回空基础，不抛异常。
    """
    head = graph.head_revision()
    rev = graph.revisions.get(head) if head else None
    verified = head if rev is not None and rev.source == "verify" else ""
    required_po = {po.id for po in graph.obligations.values() if po.required}
    ids: set[str] = set()
    for ev in graph.evidence.values():
        if ev.status != PO_STATUS_PROVEN:
            continue
        bound = {str(x) for x in (ev.proof_obligation_ids or [])}
        if required_po.intersection(bound):
            ids.add(ev.id)
    return {"revision": verified, "evidence_ids": sorted(ids)}


def can_release(
    *,
    proof_status: dict[str, Any] | None,
    review_verdict: str,
    verified_workspace_revision: str = "",
    graph: "OntologyGraph | None" = None,
) -> dict[str, Any]:
    """方案§二十八：放行条件的**唯一收敛点**（纯确定性函数，不问 LLM）。

    机器允许 ``pass`` 当且仅当同时满足：

    1. 语义评审终判为 pass（含确定性裁决层的复核）；
    2. Proof Gate 机械证明 ``status == PROVEN``（required PO 全 PROVEN、verify 真跑且过、
       无阻断级机械证据、无语义完整性 error —— 后者已由 gate 就地降级进 proof_status）；
    3. 存在经 verify 验证且仍是链头的 WorkspaceRevision（可给 graph 时自动核对）。

    返回 ``{"can_pass": bool, "verdict": "pass"|"rework", "status": PROVEN/UNPROVEN/FAILED,
    "verified_revision": str, "blocking_reasons": [str, ...]}``。
    """
    proof = proof_status if isinstance(proof_status, dict) else {}
    reasons: list[str] = []

    verified = str(verified_workspace_revision or "")
    if not verified and graph is not None:
        verified = str(release_basis(graph).get("revision") or "")

    if str(review_verdict or "").strip() != DECISION_PASS:
        reasons.append("semantic_review_not_pass：语义评审终判不是 pass")
    if not verified:
        reasons.append("no_verified_workspace：没有经 verify 验证且仍是链头的 WorkspaceRevision")
    p_status = str(proof.get("status") or "UNPROVEN")
    if p_status != PO_STATUS_PROVEN:
        for item in (proof.get("failed") or []):
            reasons.append(f"proof_failed：{item}")
        for item in (proof.get("mandatory_missing") or []):
            reasons.append(f"proof_unproven：{item}")
        if not proof.get("failed") and not proof.get("mandatory_missing"):
            reasons.append(f"proof_status_{p_status.lower()}：机械证明未达 PROVEN")

    can_pass = not reasons
    if can_pass:
        status = PO_STATUS_PROVEN
        verdict = DECISION_PASS
    else:
        status = PO_STATUS_FAILED if proof.get("failed") else PO_STATUS_UNPROVEN
        verdict = DECISION_REWORK
    return {
        "can_pass": can_pass,
        "verdict": verdict,
        "status": status,
        "verified_revision": verified,
        "blocking_reasons": reasons,
    }


def project_decision(
    graph: "OntologyGraph",
    *,
    verdict: str,
    reason: str = "",
    revision: str = "",
    evidence_ids: Iterable[str] = (),
    defect_ids: Iterable[str] = (),
    round_no: Any = 0,
    at: str = "",
) -> str:
    """方案§二十六：把一次放行裁决投影为 Decision 一等对象（DERIVED，幂等）。

    关系只挂**已存在**的端点（不造悬空）：``based_on`` Evidence、``resolves`` Defect、
    ``applies_to`` WorkspaceRevision。身份由「轮次 + 裁决 + 版本 + 证据集合」稳定决定，
    同一轮重投不产生重复对象/边。返回 Decision id。
    """
    ev_ids = sorted({str(x) for x in evidence_ids if str(x)})
    df_ids = sorted({str(x) for x in defect_ids if str(x)})
    did = "dec:" + stable_hash(
        canonical_json([int(round_no or 0), str(verdict), str(revision), ev_ids]),
        length=12,
    )
    if graph.get(did) is None:
        graph.add(SemanticObject(
            id=did, type=TYPE_DECISION, truth=TRUTH_DERIVED,
            payload={
                "verdict": str(verdict or ""),
                "reason": str(reason or "")[:500],
                "revision": str(revision or ""),
                "at": str(at or ""),
                "round": int(round_no or 0),
            },
            provenance=[Provenance(source="release_gate", stage="review")],
        ))
    else:
        obj = graph.get(did)
        obj.payload.update({
            "verdict": str(verdict or ""),
            "reason": str(reason or "")[:500],
            "revision": str(revision or ""),
        })
    for ev_id in ev_ids:
        if ev_id in graph.evidence:
            _relate_once(graph, did, "based_on", ev_id, truth=TRUTH_DERIVED)
    if revision and revision in graph.revisions:
        _relate_once(graph, did, "applies_to", revision, truth=TRUTH_DERIVED)
    for defect_id in df_ids:
        if graph.get(defect_id) is not None:
            _relate_once(graph, did, "resolves", defect_id, truth=TRUTH_DERIVED)
    return did


