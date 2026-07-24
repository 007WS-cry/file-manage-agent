from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.channels import UntrackedValue
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.state.reducers import merge_by_id, merge_by_message_id, merge_by_task_id

"""本模块定义文件版本治理的顶层状态、子图状态和业务记录结构。"""


class RunState(TypedDict):
    """一次文件版本治理运行的生命周期状态。"""

    run_id: str
    # 本次运行的唯一标识。

    thread_id: str
    # LangGraph Checkpointer 使用的线程 ID；非 CLI 调用可回退为 run_id。

    status: Literal[
        "created",
        "running",
        "recovering",
        "waiting_human",
        "completed",
        "partial",
        "failed",
    ]
    # 当前运行状态；recovering 为 0.7.0 统一恢复流程预留，部分成功最终为 partial。

    current_stage: str
    # 当前正在执行的主流程阶段或子图名称。

    started_at: str | None
    # 运行开始时间，建议使用 ISO 8601 格式。

    finished_at: str | None
    # 运行结束时间；任务未结束时为 None。


class RequestState(TypedDict):
    """用户提交的文件治理范围、判断参数和本地证据来源。"""

    root_directory: str
    # 需要扫描和治理的根目录。

    recursive: bool
    # 是否递归扫描根目录下的子目录。

    allowed_extensions: list[str]
    # 允许纳入扫描范围的扩展名，例如 .xlsx、.docx、.pdf。

    max_files: int
    # 单次运行允许处理的最大文件数量，防止任务失控。

    grouping_similarity_threshold: float
    # 将两个文件归入同一版本组所需的最低相似度。

    auto_select_threshold: float
    # 自动推荐主版本所需的最低置信度。

    pdf_match_threshold: float
    # 将 PDF 判定为某个可编辑版本导出件所需的最低匹配分数。

    delivery_log_path: str | None
    # 本地发送记录 JSON 文件路径；未提供时跳过发送证据匹配。

    use_llm_summary: bool
    # 是否允许 LLM 为内容差异生成自然语言摘要。


class WorkspaceState(TypedDict):
    """原始文件和运行产物所在的工作空间。"""

    input_root: str
    # 经过解析和规范化后的输入根目录。

    input_readonly: bool
    # 原始文件是否为只读；必须始终设置为 True。

    artifact_root: str
    # 标准化内容和中间比较结果的产物目录。

    report_root: str
    # 可选治理报告的输出目录。


class FileRecord(TypedDict):
    """扫描得到的单个原始文件记录。"""

    id: str
    # 文件记录唯一 ID，由规范化绝对路径生成。

    absolute_path: str
    # 原始文件的绝对路径。

    file_name: str
    # 包含扩展名的原始文件名。

    normalized_stem: str
    # 移除版本号、日期、final 等弱标记后的规范化文件名主体。

    extension: str
    # 小写形式的文件扩展名。

    size_bytes: int
    # 文件大小，单位为字节。

    modified_at: str
    # 文件最后修改时间，使用带时区的 ISO 8601 格式。

    sha256: str
    # 原始文件的 SHA-256 哈希。

    duplicate_of: str | None
    # 完全重复时指向规范文件 ID，否则为 None。

    parse_status: Literal[
        "pending",
        "parsed",
        "duplicate",
        "unsupported",
        "failed",
    ]
    # 文件内容的解析状态。

    parse_error: str | None
    # 文件解析失败原因；未失败时为 None。


class RawExtractedContent(TypedDict):
    """解析器产生、尚未完全标准化的临时内容。"""

    text: str
    # 解析器提取的连续文本。

    structure: dict[str, Any]
    # 工作表、表格、页面和标题等结构化信息。

    key_fields: dict[str, Any]
    # 金额、日期、客户、编号等初步关键字段。

    warnings: list[str]
    # 解析过程中产生的非致命警告。


class DocumentRecord(TypedDict):
    """原始文件解析后得到的标准化文档记录。"""

    id: str
    # 标准化文档记录唯一 ID。

    file_id: str
    # 对应的原始文件 ID。

    parser_name: str
    # 实际使用的解析器名称和版本。

    content_ref: str
    # 完整标准化内容的产物引用，避免把大段正文放入图状态。

    content_preview: str
    # 用于日志和报告展示的短内容预览。

    normalized_digest: str
    # 标准化内容的摘要哈希，用于快速发现内容重复。

    structure_summary: dict[str, Any]
    # 页数、工作表、表格和段落数量等结构摘要。

    key_fields: dict[str, Any]
    # 从文档中提取的关键业务字段。

    warnings: list[str]
    # 内容提取和标准化警告。


class VersionGroupRecord(TypedDict):
    """一组被判断为同一业务文档不同版本的文件。"""

    id: str
    # 版本组唯一 ID。

    label: str
    # 面向用户显示的文档组名称。

    file_ids: list[str]
    # 属于该版本组的全部文件 ID，包括完全重复文件。

    grouping_signals: list[str]
    # 文件名、关键字段和内容相似度等分组证据。

    confidence: float
    # 版本分组结果的置信度。


class ComparisonJob(TypedDict):
    """版本分析子图内部使用的文件对比较任务。"""

    id: str
    # 比较任务唯一 ID。

    group_id: str
    # 文件对所属的版本组 ID。

    left_file_id: str
    # 待比较的第一个文件 ID。

    right_file_id: str
    # 待比较的第二个文件 ID。

    status: Literal["pending", "running", "completed", "failed"]
    # 当前比较任务状态。


class DiffRecord(TypedDict):
    """两个疑似版本之间的内容差异和先后判断。"""

    id: str
    # 差异记录唯一 ID。

    group_id: str
    # 差异所属版本组 ID。

    file_a_id: str
    # 参与比较的第一个文件 ID。

    file_b_id: str
    # 参与比较的第二个文件 ID。

    older_file_id: str | None
    # 推测的较早版本；无法判断时为 None。

    newer_file_id: str | None
    # 推测的较新版本；无法判断时为 None。

    structural_similarity: float
    # 表格、工作表、段落等结构相似度。

    content_similarity: float
    # 标准化文本和关键字段的内容相似度。

    key_changes: list[str]
    # 金额、日期、条款和表格值等关键修改。

    summary: str
    # 当前生效的关键修改摘要。

    summary_source: Literal["deterministic", "version_subagent"]
    # 摘要来自确定性规则还是成功返回的 Version Subagent。

    summary_message_id: str | None
    # 产生当前摘要的 Team Message ID；确定性摘要时为 None。

    summary_artifact_ref: str | None
    # 详细版本解释的首个受控产物引用；未生成独立产物时为 None。

    ordering_signals: list[str]
    # 支撑版本先后关系的证据。

    confidence: float
    # 差异比较和先后判断的综合置信度。


class VersionEdge(TypedDict):
    """版本图中的一条父版本到子版本关系。"""

    id: str
    # 版本边唯一 ID。

    group_id: str
    # 版本边所属的版本组 ID。

    parent_file_id: str
    # 推测的较早版本文件 ID。

    child_file_id: str
    # 推测的较新版本文件 ID。

    relation: Literal["derived_from", "duplicate_of", "uncertain"]
    # 版本关系类型：派生、完全重复或暂不确定。

    evidence: list[str]
    # 支撑该版本关系的证据。

    confidence: float
    # 版本关系的置信度。


class BranchRecord(TypedDict):
    """同一父版本产生多个后续版本的分叉记录。"""

    id: str
    # 分叉记录唯一 ID。

    group_id: str
    # 分叉所属的版本组 ID。

    root_file_id: str
    # 产生分叉的共同父版本文件 ID。

    child_file_ids: list[str]
    # 从共同父版本派生的子版本文件 ID。

    reason: str
    # 判断为版本分叉的原因。

    confidence: float
    # 分叉判断的置信度。


class VersionChainRecord(TypedDict):
    """一个版本组整理后的可读版本链。"""

    id: str
    # 版本链记录唯一 ID。

    group_id: str
    # 版本链所属的版本组 ID。

    ordered_file_ids: list[str]
    # 能够确定先后顺序的文件 ID 列表。

    leaf_file_ids: list[str]
    # 当前版本图中没有后继版本的叶子文件 ID。

    is_complete: bool
    # 是否已将组内所有文件纳入无矛盾的版本关系。

    warnings: list[str]
    # 循环、孤立版本和不确定关系等版本链警告。


class DecisionRecord(TypedDict):
    """一个版本组的主版本推荐结果。"""

    id: str
    # 推荐结果唯一 ID。

    group_id: str
    # 推荐结果对应的版本组 ID。

    candidate_scores: dict[str, float]
    # 候选文件 ID 到推荐评分的映射。

    recommended_file_id: str | None
    # 当前推荐的主版本文件 ID；无法选择时为 None。

    reasons: list[str]
    # 推荐或无法推荐的可解释原因。

    confidence: float
    # 当前推荐结果的综合置信度。

    needs_human_review: bool
    # 是否必须由用户确认主版本。

    selected_by: Literal["rule", "human", "unresolved"]
    # 主版本由规则选择、用户选择，或尚未解决。

    preserve_file_ids: list[str]
    # 必须保留的版本链文件；默认保留组内全部文件。


class HumanReviewState(TypedDict):
    """LangGraph interrupt 暂停与恢复所需的人工确认状态。"""

    pending_group_ids: list[str]
    # 当前需要用户确认的版本组 ID。

    selections: dict[str, str]
    # 用户选择结果，键为版本组 ID，值为主版本文件 ID。

    review_note: str | None
    # 用户在人工确认阶段提供的补充说明。


class MemoryItemState(TypedDict):
    """一个经过脱敏和长度限制、可在图状态或应用数据库中传递的 Memory 条目。"""

    id: str
    # Memory 条目唯一 ID；由安全结构化字段计算，不包含业务正文。

    namespace: str
    # 隔离不同工作空间的哈希命名空间，不保存原始目录路径。

    scope: Literal["short_term", "long_term"]
    # Memory 生命周期；短期仅随当前图状态存在，长期允许写入应用数据库。

    kind: Literal[
        "stage_summary",
        "confirmed_version_choice",
        "reliable_evidence_relation",
        "governance_preference",
    ]
    # Memory 类型，只允许数据库约束定义的四类治理事实。

    summary: str
    # 由固定模板生成的有界摘要，禁止写入文档正文、密钥或完整模型 Prompt。

    structured_data: dict[str, Any]
    # 经过字段白名单校验的 ID、评分和证据类型等结构化数据。

    artifact_refs: list[str]
    # 支撑结论的受控产物引用；不得保存原始正文或外部凭据。

    source_run_id: str
    # 产生该条目的治理运行 ID。

    confirmed_by_human: bool
    # 条目是否直接来自用户明确确认。

    confidence: float
    # 条目置信度，范围为 0.0 到 1.0。

    created_at: str
    # 条目创建时间，使用带时区的 ISO 8601 格式。


class MemoryState(TypedDict):
    """治理图共享的短期与长期 Memory 配置、召回结果和待持久化缓冲区。"""

    enabled: bool
    # 是否启用长期 Memory 数据库访问；默认关闭以保持旧版运行兼容。

    namespace: str
    # 当前工作空间的隔离命名空间，默认由输入根目录哈希生成。

    database_path: str | None
    # 独立应用数据库文件路径；关闭 Memory 时为 None。

    checkpoint_path: str | None
    # 可选 SQLite checkpoint 文件路径，用于强制校验两类数据库不共用文件。

    recall_limit: int
    # 每次新运行最多召回的长期 Memory 条目数量。

    status: Literal["disabled", "pending", "ready", "failed"]
    # 长期 Memory 的当前加载或持久化状态。

    recalled_items: list[MemoryItemState]
    # 本次运行从应用数据库召回的长期治理事实。

    short_term_items: list[MemoryItemState]
    # 仅随当前 LangGraph 状态和 Checkpointer 存在的阶段摘要。

    pending_long_term_items: list[MemoryItemState]
    # 已通过安全策略、等待写入应用数据库的长期治理事实。

    persisted_item_ids: list[str]
    # 本次运行已经成功写入应用数据库的 Memory 条目 ID。

    last_error: str | None
    # 最近一次 Memory 操作的脱敏错误摘要；没有错误时为 None。


class ErrorRecord(TypedDict):
    """统一错误恢复协议使用的结构化错误生命周期记录。"""

    id: str
    # 错误唯一 ID；同一节点执行中的同一错误在重试时保持不变。

    stage: str
    # 错误发生的主流程阶段或子图名称。

    node_name: str
    # 实际发生错误的函数节点名称。

    category: Literal[
        "filesystem",
        "parse",
        "comparison",
        "evidence",
        "llm",
        "validation",
        "protocol",
        "prompt",
        "hook",
        "memory",
        "skill",
        "context",
        "database",
        "checkpoint",
        "timeout",
        "unknown",
    ]
    # 错误分类；由 Error Recovery 子图查询对应的确定性恢复策略。

    exception_type: str | None
    # 已脱敏的异常类型名称；规则校验产生的错误可以为 None。

    message: str
    # 可供日志、人工恢复请求和报告展示的脱敏错误说明。

    related_file_id: str | None
    # 与错误相关的文件 ID；非文件错误时为 None。

    task_id: str | None
    # 相关 Task ID；Task DAG 创建前的错误可以为 None。

    node_execution_id: str | None
    # 相关节点执行记录 ID；用于后续幂等判断和结果复用。

    retryable: bool
    # 当前错误分类是否允许自动重试。

    retry_count: int
    # 已经执行的额外重试次数，不包含第一次正常执行。

    max_retries: int
    # 允许执行的最大额外重试次数。

    fallback: (
        Literal[
            "skip_file",
            "coordinator",
            "no_memory",
            "default_skill",
            "keep_context",
            "partial_result",
        ]
        | None
    )
    # 当前错误可采用的安全降级；不存在安全降级时为 None。

    requires_human: bool
    # 自动重试和安全降级均不足时是否需要人工恢复。

    status: Literal[
        "pending",
        "retrying",
        "fallback_applied",
        "waiting_human",
        "recovered",
        "failed",
    ]
    # 错误从捕获到最终恢复或失败的生命周期状态。

    fatal: bool
    # 兼容 0.6.0 的字段；后续恢复图只在最终终止时将其视为致命错误。

    created_at: str
    # 首次捕获错误的 ISO 8601 时间。

    recovered_at: str | None
    # 错误完成恢复的时间；尚未恢复时为 None。


class NodeExecutionRecord(TypedDict):
    """一个可以由 checkpoint 重放或通过幂等键复用的节点执行记录。"""

    id: str
    # 节点幂等键，由运行、Task、节点名称和安全输入摘要计算。

    task_execution_id: str | None
    # 所属逻辑 Task 的执行 ID；Task DAG 创建前可以为 None。

    run_id: str
    # 所属治理运行 ID。

    task_id: str | None
    # 所属 Task ID；生命周期节点可以为 None。

    stage: str
    # 节点所属主流程阶段或子图名称。

    node_name: str
    # 实际执行的函数节点名称。

    input_digest: str
    # 只根据稳定 ID、配置值、文件哈希和产物引用计算的输入摘要。

    status: Literal[
        "pending",
        "running",
        "succeeded",
        "failed",
        "reused",
    ]
    # 节点尚未执行、执行中、成功、失败或结果复用的状态。

    attempt_count: int
    # 节点累计执行次数，包含第一次执行。

    state_update_ref: str | None
    # 成功状态更新的受控 JSON 产物引用，用于重放时复用。

    result_refs: list[str]
    # 节点产生的业务产物引用。

    result_digest: str | None
    # 状态更新和产物引用的完整性摘要。

    last_error_id: str | None
    # 最近一次执行失败对应的 ErrorRecord ID。

    started_at: str
    # 第一次开始执行的 ISO 8601 时间。

    finished_at: str | None
    # 成功、失败或复用完成的时间。


class DegradationRecord(TypedDict):
    """一次安全降级及其对最终治理结果影响的记录。"""

    id: str
    # 降级记录唯一 ID。

    error_id: str
    # 触发该降级的 ErrorRecord ID。

    stage: str
    # 应用降级的业务阶段。

    action: Literal[
        "skip_file",
        "coordinator",
        "no_memory",
        "default_skill",
        "keep_context",
        "partial_result",
    ]
    # 实际使用的降级动作。

    summary: str
    # 面向用户和报告的简短降级说明。

    affected_file_ids: list[str]
    # 受降级影响的文件 ID；非文件级降级时为空列表。

    impact: str
    # 降级对置信度、完整性或可解释性的影响。

    created_at: str
    # 降级动作生效的 ISO 8601 时间。


class RecoveryCategoryPolicyState(TypedDict):
    """一个错误类别对应的确定性重试、退避、降级和人工恢复策略。"""

    retryable: bool
    # 当前类别是否允许自动重试。

    max_retries: int
    # 允许执行的最大额外重试次数，不包含第一次正常执行。

    initial_backoff_seconds: float
    # 第一次自动重试前的确定性等待秒数。

    backoff_multiplier: float
    # 后续重试等待时间使用的指数倍数。

    max_backoff_seconds: float
    # 单次自动重试允许使用的最大等待秒数。

    fallback: (
        Literal[
            "skip_file",
            "coordinator",
            "no_memory",
            "default_skill",
            "keep_context",
            "partial_result",
        ]
        | None
    )
    # 重试不可用或耗尽后允许采用的安全降级。

    requires_human: bool
    # 无法自动恢复时是否允许请求用户提供恢复输入。


class RecoveryPolicyState(TypedDict):
    """一次治理运行使用的错误分类和恢复策略快照。"""

    enabled: bool
    # 是否启用恢复策略判断；关闭时异常由恢复入口转入失败报告。

    default_policy: RecoveryCategoryPolicyState
    # 未知或未单独配置错误类别使用的默认策略。

    category_policies: dict[str, RecoveryCategoryPolicyState]
    # 错误类别名称到完整恢复策略的映射。


class ErrorContextState(TypedDict):
    """业务子图和工具创建统一恢复错误时使用的最小执行上下文。"""

    run_id: str
    # 当前治理运行 ID；独立子图测试使用稳定 standalone 标识。

    task_id: str
    # 错误归属的真实或兼容 Task ID，不允许为空。

    task_execution_id: str
    # 同一逻辑 Task 在有限重试期间保持稳定的执行 ID。

    policy: RecoveryPolicyState
    # 当前运行完整的 Recovery Policy 快照。


class RecoveryHumanState(TypedDict):
    """恢复型人工确认的待处理请求和用户响应状态。"""

    kind: Literal["error_recovery"]
    # interrupt 类型；必须与主版本选择的 file_governance_review 区分。

    pending_error_id: str | None
    # 当前等待用户处理的错误 ID。

    allowed_actions: list[
        Literal[
            "retry",
            "skip_file",
            "provide_path",
            "abort",
        ]
    ]
    # 当前错误允许用户选择的恢复动作。

    selected_action: (
        Literal[
            "retry",
            "skip_file",
            "provide_path",
            "abort",
        ]
        | None
    )
    # 用户恢复后选择的动作；尚未恢复时为 None。

    replacement_path: str | None
    # 用户补充或修正的输入路径；不需要路径时为 None。

    note: str | None
    # 用户提供的简短恢复说明；不得进入长期 Memory。


class RecoveryState(TypedDict):
    """顶层状态保存的恢复策略、待处理错误和恢复动作。"""

    policy: RecoveryPolicyState
    # 当前运行使用的确定性恢复策略快照。

    pending_error_ids: list[str]
    # 等待 Error Recovery 子图处理的错误 ID 队列。

    current_error_id: str | None
    # 当前正在分类或处理的错误 ID。

    action: Literal[
        "none",
        "retry",
        "reuse_result",
        "skip_file",
        "fallback",
        "continue_partial",
        "wait_human",
        "abort",
    ]
    # Error Recovery 子图输出的恢复动作。

    resume_node: str | None
    # 自动重试或补充输入后需要重新执行的顶层节点。

    resume_after_node: str | None
    # 结果复用或安全降级后需要继续执行的顶层节点。

    retry_delay_seconds: float
    # 当前重试动作使用的确定性退避时间。

    fallback: (
        Literal[
            "skip_file",
            "coordinator",
            "no_memory",
            "default_skill",
            "keep_context",
            "partial_result",
        ]
        | None
    )
    # 当前选择的安全降级策略。

    human: RecoveryHumanState
    # 独立于主版本选择的恢复型人工确认状态。

    degradation_ids: list[str]
    # 本次恢复流程已经产生的降级记录 ID。

    last_policy_reason: str | None
    # 当前恢复动作的分类和策略理由。


class ReportState(TypedDict):
    """最终返回给用户的版本治理报告。"""

    summary: str
    # 本次运行的整体结果摘要。

    report_markdown: str
    # 包含文件组、版本链、差异和推荐结果的 Markdown 报告。

    warnings: list[str]
    # 需要用户注意的解析失败、低置信度和版本分叉。

    report_path: str | None
    # 报告写入磁盘后的路径；仅返回文本时为 None。

    generated_at: str | None
    # 报告生成时间。

    degradation_ids: list[str]
    # 本次报告“降级项”章节引用的安全降级记录 ID。

    recovered_error_ids: list[str]
    # 本次报告“已恢复错误”章节引用的已恢复或已应用降级错误 ID。


class PdfMatchJob(TypedDict):
    """一项 PDF 与可编辑源版本的匹配任务。"""

    id: str
    # PDF 匹配任务唯一 ID。

    group_id: str
    # 当前 PDF 所属的版本组 ID。

    pdf_file_id: str
    # 等待匹配来源的 PDF 文件 ID。

    source_candidate_ids: list[str]
    # 同一版本组内可能生成该 PDF 的可编辑文件 ID。

    status: Literal["pending", "running", "completed", "failed"]
    # PDF 匹配任务当前执行状态。


class DeliveryLogEntry(TypedDict):
    """从本地发送日志读取、尚未匹配到文件版本的原始记录。"""

    id: str
    # 本地发送记录唯一 ID。

    attachment_name: str
    # 当时发送的附件文件名。

    attachment_sha256: str | None
    # 附件 SHA-256；旧日志没有哈希时为 None。

    normalized_digest: str | None
    # 附件标准化内容摘要；日志未保存时为 None。

    sent_at: str | None
    # 附件发送时间；未知时为 None。

    recipient_label: str
    # 脱敏后的客户或收件人标识。

    customer_confirmed: bool
    # 是否存在客户确认、批准或接受记录。

    evidence_ref: str
    # 指向原始日志记录的稳定引用。


class PdfExportRecord(TypedDict):
    """PDF 与其最可能可编辑来源版本的匹配结果。"""

    id: str
    # PDF 来源匹配记录唯一 ID。

    group_id: str
    # PDF 所属的版本组 ID。

    pdf_file_id: str
    # PDF 文件 ID。

    source_file_id: str | None
    # 最可能的可编辑来源文件 ID；无法可靠判断时为 None。

    match_score: float
    # PDF 与来源候选的原始内容匹配评分。

    matched_signals: list[str]
    # 文本、关键字段、表格结构和时间等匹配证据。

    confidence: float
    # PDF 来源判断的综合置信度。


class DeliveryRecord(TypedDict):
    """本地发送记录与具体文件版本的匹配结果。"""

    id: str
    # 发送证据唯一 ID。

    group_id: str | None
    # 匹配到的版本组 ID；未匹配时为 None。

    file_id: str | None
    # 匹配到的文件版本 ID；未匹配时为 None。

    evidence_source: Literal["local_log", "email_mcp", "manual"]
    # 证据来源；本版只生成 local_log 类型。

    sent_at: str | None
    # 文件发送时间；未知时为 None。

    recipient_label: str
    # 脱敏后的客户或收件人标识。

    evidence_ref: str
    # 原始发送记录、邮件或人工证明的引用。

    match_method: Literal[
        "sha256",
        "normalized_digest",
        "file_name",
        "unmatched",
    ]
    # 发送记录与文件版本的匹配方法。

    customer_confirmed: bool
    # 是否存在客户确认、批准或接受记录。

    confidence: float
    # 发送证据匹配到该版本的置信度。


class RecommendationCandidateSet(TypedDict):
    """Recommendation 子图中一个版本组的候选集合。"""

    id: str
    # 候选集合唯一 ID，通常由版本组 ID 派生。

    group_id: str
    # 候选集合所属的版本组 ID。

    candidate_file_ids: list[str]
    # 可参与主版本推荐的非重复文件 ID。

    editable_leaf_file_ids: list[str]
    # 位于版本链末端的可编辑文件 ID。


class PdfMatchWorkerState(TypedDict):
    """单个并行 PDF 匹配 Worker 接收和返回的状态。"""

    request: RequestState
    # 当前 PDF 匹配阈值等只读请求参数。

    job: PdfMatchJob
    # 当前 Worker 负责的 PDF 匹配任务。

    files: list[FileRecord]
    # 当前匹配任务可能引用的文件记录。

    documents: list[DocumentRecord]
    # 当前匹配任务需要读取的标准化文档记录。

    pdf_match_jobs: Annotated[list[PdfMatchJob], merge_by_id]
    # 当前 Worker 返回的完成或失败任务状态。

    pdf_exports: Annotated[list[PdfExportRecord], merge_by_id]
    # 当前 Worker 产生的 PDF 来源匹配结果。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 当前 Worker 产生的非致命或致命错误。


class PromptState(TypedDict):
    """System Prompt 状态：记录本次运行实际加载的系统提示词。"""

    enabled: bool
    # 本次运行是否启用 System Prompt。

    version: str
    # Prompt 的语义版本，例如 file-governance-v1。

    source_path: str | None
    # Prompt 资源文件路径；关闭时为 None。

    content: str
    # 完整 System Prompt 内容；关闭或加载失败时为空字符串。

    content_sha256: str | None
    # Prompt 内容的 SHA-256，用于测试、审计和版本确认。

    dynamic_rules: list[str]
    # 根据本次请求追加的只读、证据和人工确认规则。

    status: Literal[
        "pending",
        "loaded",
        "disabled",
        "failed",
    ]
    # Prompt 当前处于等待加载、已加载、已关闭或加载失败状态。


class ContextSummaryState(TypedDict):
    """一次 Context Compact 产生的有界摘要及受控产物引用。"""

    id: str
    # Context Summary 唯一 ID，由运行、阶段和压缩序号确定性生成。

    run_id: str
    # 产生该摘要的治理运行 ID。

    stage: Literal["after_inventory", "after_evidence"]
    # 触发压缩的固定业务阶段。

    summary: str
    # 由固定模板生成的简短压缩说明，不包含正文、Prompt 或凭据。

    artifact_refs: list[str]
    # 被移出图状态的上下文产物引用；不直接保存大型内容。

    estimated_tokens: int
    # 压缩完成后的近似上下文 Token 数。

    compaction_index: int
    # 当前运行内从一开始递增的压缩序号。

    created_at: str
    # 摘要创建时间，使用带时区的 ISO 8601 格式。


class ContextCompactState(TypedDict):
    """顶层治理图共享的 Context Compact 配置、进度和摘要索引。"""

    enabled: bool
    # 是否启用自动上下文估算和阶段压缩；默认关闭以兼容旧运行。

    trigger_token_threshold: int
    # 估算上下文超过该 Token 数时才允许触发压缩。

    retained_preview_characters: int
    # Evidence 完成后每个文档预览仍保留在图状态中的最大字符数。

    persist_summaries: bool
    # 是否把有界 Context Summary 写入独立应用数据库。

    database_path: str | None
    # Context Summary 使用的应用数据库文件路径；关闭时为 None。

    checkpoint_path: str | None
    # 可选 SQLite checkpoint 路径，用于强制数据库文件隔离。

    status: Literal["disabled", "pending", "ready", "failed"]
    # Context Compact 当前处于关闭、等待、正常或失败状态。

    current_stage: Literal["after_inventory", "after_evidence"] | None
    # 最近完成估算或压缩的固定阶段。

    estimated_tokens: int
    # 最近一次阶段处理后的近似上下文 Token 数。

    summaries: list[ContextSummaryState]
    # 当前运行已经产生的 Context Summary 索引。

    last_error: str | None
    # 最近一次压缩、产物或数据库操作的脱敏错误；正常时为 None。


class ApplicationDatabaseState(TypedDict):
    """应用数据库的启用状态、SQLite 隔离配置和运行期连接结果。"""

    enabled: bool
    # 是否持久化运行、Memory、审计、人工选择、错误恢复和节点执行；默认关闭。

    backend: Literal["sqlite"]
    # 当前应用数据库后端；0.6.0 只支持独立 SQLite 文件。

    database_path: str | None
    # 七张应用表共用的 SQLite 文件绝对路径；关闭时为 None。

    checkpoint_path: str | None
    # 可选 LangGraph checkpoint 路径，用于强制两个数据库文件完全隔离。

    auto_create_parent: bool
    # 是否允许 Engine 自动创建数据库父目录；当前实现固定为 True。

    echo: bool
    # 是否输出 SQLAlchemy SQL 日志；默认关闭以减少结构化数据泄漏风险。

    timeout_seconds: float
    # SQLite 等待短暂文件锁释放的最大秒数。

    status: Literal["disabled", "pending", "ready", "failed"]
    # 应用数据库当前处于关闭、等待连接、可用或失败状态。

    last_error: str | None
    # 最近一次建连、运行更新或审核持久化失败的脱敏说明。


class ContextCompactionPlanState(TypedDict):
    """Context Compact 子图根据阶段和阈值生成的不可变压缩计划。"""

    stage: Literal["after_inventory", "after_evidence"]
    # 当前计划对应的固定业务阶段。

    estimated_tokens_before: int
    # 执行压缩前的近似上下文 Token 数。

    reclaimable_tokens: int
    # 当前阶段允许移出图状态的近似 Token 数。

    should_compact: bool
    # 是否同时满足启用、阈值和可回收上下文条件。

    compact_prompt_content: bool
    # 是否清空已加载且后续不再消费的 System Prompt 正文。

    compact_document_ids: list[str]
    # Evidence 完成后允许移出详细预览和结构字段的文档 ID。


class ContextCompactGraphState(TypedDict):
    """独立 Context Compact 子图使用的输入、计划、临时载荷和输出状态。"""

    error_context: ErrorContextState
    # Context Compact 节点创建统一恢复错误所需的 Task 和策略上下文。

    run: RunState
    # 当前治理运行信息，用于生成稳定摘要和产物 ID。

    workspace: WorkspaceState
    # 只读输入目录和可写中间产物目录。

    prompt: PromptState
    # 可在安全阶段清空正文、但保留版本和摘要哈希的 Prompt 状态。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 可在 Evidence 完成后缩减详细预览的标准化文档记录。

    context_compact: ContextCompactState
    # 当前压缩配置、历史摘要和状态。

    stage: Literal["after_inventory", "after_evidence"]
    # 本次子图调用的固定压缩阶段。

    plan: ContextCompactionPlanState | None
    # Token 估算节点生成的当前压缩计划。

    compaction_payload: Annotated[dict[str, Any] | None, UntrackedValue]
    # 等待写入中间产物的大型临时上下文，不进入任何 checkpoint。

    summary_draft: ContextSummaryState | None
    # 等待补充产物引用并写入应用数据库的有界摘要草稿。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 估算、压缩、产物和数据库操作产生的非致命错误。


class HookConfigState(TypedDict):
    """Hooks 配置状态：定义生命周期阶段、执行顺序和失败策略。"""

    enabled: bool
    # 是否启用可扩展 Hooks；关闭后不执行配置中的 Hook。

    before_run: list[str]
    # 顶层业务流程开始前执行的 Hook 名称，列表顺序即执行顺序。

    before_model: list[str]
    # 模型调用前执行的 Hook 名称；本版可通过模拟模型测试。

    after_model: list[str]
    # 模型调用后执行的 Hook 名称；本版可通过模拟模型测试。

    after_run: list[str]
    # 报告生成后、运行最终收口前执行的 Hook 名称。

    default_failure_policy: Literal["block", "ignore"]
    # Hook 未单独配置策略时使用的默认失败策略。

    failure_policies: dict[str, Literal["block", "ignore"]]
    # Hook 名称到失败策略的映射；覆盖默认失败策略。


class HookEvent(TypedDict):
    """Hook 执行事件：记录单个 Hook 的顺序、状态和处理策略。"""

    id: str
    # Hook 事件唯一 ID。

    phase: Literal[
        "before_run",
        "before_model",
        "after_model",
        "after_run",
    ]
    # Hook 所属生命周期阶段。

    sequence: int
    # Hook 在当前阶段的执行序号，用于验证调用顺序。

    hook_name: str
    # 实际执行或跳过的 Hook 函数名称。

    status: Literal[
        "success",
        "failed",
        "skipped",
    ]
    # Hook 执行成功、失败或因配置关闭而跳过。

    failure_policy: Literal["block", "ignore"]
    # 本次 Hook 失败时实际采用的处理策略。

    message: str
    # 简短执行结果，不保存文档正文或敏感工具输出。

    created_at: str
    # Hook 事件产生时间，使用带时区的 ISO 8601 格式。


class TodoItem(TypedDict):
    """面向用户展示的高层进度项，所有状态均由关联 Task 推导。"""

    id: str
    # Todo 的稳定唯一 ID。

    title: str
    # 面向用户展示的中文 Todo 标题。

    status: Literal[
        "pending",
        "in_progress",
        "completed",
        "blocked",
    ]
    # Todo 当前展示状态，不允许脱离 Task 单独修改。

    related_task_ids: list[str]
    # 决定该 Todo 状态的底层 Task ID 列表。

    order: int
    # Todo 在用户界面或 CLI 输出中的固定显示顺序。


class TaskItem(TypedDict):
    """Task System 中的真实执行状态，是 Todo 和执行进度的唯一事实来源。"""

    task_id: str
    # Task 的稳定唯一 ID，建议使用“run_id:task_type”。

    execution_id: str
    # 逻辑 Task 的稳定执行 ID；同一 Task 的有限重试必须保持不变。

    task_type: Literal[
        "inventory",
        "version_analysis",
        "evidence",
        "recommendation",
        "human_review",
        "report",
    ]
    # Task 所代表的治理阶段类型。

    title: str
    # 面向日志、Todo 和调试输出的中文任务标题。

    status: Literal[
        "pending",
        "running",
        "retrying",
        "completed",
        "partial",
        "failed",
        "skipped",
    ]
    # Task 当前真实执行状态；partial 为有可用降级结果的终态。

    attempt_count: int
    # Task 累计开始执行的次数，包含第一次正常执行和后续重试。

    dependencies: list[str]
    # 普通 Task 启动前须依赖成功终结；Report Task 可在依赖进入任一终态后收口。

    assigned_role: Literal[
        "coordinator",
        "content",
        "version",
        "evidence",
    ]
    # 当前 Task 的固定负责角色；0.4.4 由 Team Orchestration 实际选择并调用。

    input_refs: list[str]
    # Task 使用的状态字段、文件记录或产物引用，不保存完整文档正文。

    output_refs: list[str]
    # Task 完成后产生的状态记录或产物引用。

    error: str | None
    # Task 失败或被失败依赖阻断时的简短错误；正常状态为 None。

    created_at: str
    # Task 首次创建时间，使用带时区的 ISO 8601 格式。

    updated_at: str
    # Task 最近一次真实状态变化时间，使用带时区的 ISO 8601 格式。


class TaskStatusUpdate(TypedDict):
    """顶层流程传给 Team Orchestration 子图的一次 Task 状态变更。"""

    task_id: str
    # 本次需要更新的目标 Task ID。

    execution_id: str
    # 目标逻辑 Task 的稳定执行 ID，用于拒绝跨 Task 或过期更新。

    status: Literal[
        "running",
        "retrying",
        "completed",
        "partial",
        "failed",
        "skipped",
    ]
    # 目标 Task 需要进入的新状态。

    attempt_count: int
    # 本次更新完成后的 Task 累计执行次数。

    output_refs: list[str]
    # 本阶段新产生的状态记录或产物引用。

    error: str | None
    # 失败或阻断原因；成功和正常跳过时为 None。

    updated_at: str
    # 本次状态变更发生时间，使用带时区的 ISO 8601 格式。


class SkillRecord(TypedDict):
    """一个受控 Skill 的元数据、按需加载内容和当前绑定状态。"""

    skill_id: str
    # Skill 的稳定唯一 ID，必须与 resources/skills 下的注册表一致。

    name: str
    # 面向日志和文档展示的中文名称。

    description: str
    # 用于选择和审计的简短能力说明，不包含完整 Skill 指令。

    source_path: str
    # SKILL.md 的受控绝对路径，只能位于注册表所在目录内。

    task_types: list[str]
    # 允许选择该 Skill 的固定 Task 类型。

    roles: list[str]
    # 允许绑定该 Skill 的固定 Agent 角色。

    status: Literal["available", "loaded", "bound"]
    # Skill 当前可用、已读取但未绑定或已绑定到一个 Task 的运行状态。

    bound_task_id: str | None
    # 当前绑定的真实 Task ID；未绑定时为 None。

    content: str
    # 按需读取的 SKILL.md 正文；恢复 available 时必须清空。

    content_sha256: str | None
    # 已加载正文的 SHA-256；未加载或释放后为 None。


class SkillRegistryState(TypedDict):
    """一次治理运行使用的受控 Skill 注册表状态。"""

    version: str
    # Skill 注册表协议版本，例如 skill-registry-v1。

    source_path: str
    # registry.yaml 的绝对路径。

    status: Literal["pending", "ready", "failed"]
    # 注册表等待加载、可供选择或加载失败的状态。

    skills: list[SkillRecord]
    # 当前已登记的 Skill；只有本次 Task 所需项允许暂存正文。


class TaskSkillSelectionState(TypedDict):
    """Team Orchestration 为一次 Subagent 分派生成的 Skill 选择结果。"""

    task_id: str
    # 当前分派对应的真实 Task ID。

    task_type: str
    # 当前 Task 的固定类型。

    role: str
    # 当前 Task 的固定负责角色。

    skill_ids: list[str]
    # 依据注册表和固定 Agent 定义选择出的最小 Skill ID 列表。


class SkillInstructionState(TypedDict):
    """传给单个 Subagent 的已验证 Skill 指令快照。"""

    skill_id: str
    # Skill 的稳定唯一 ID。

    name: str
    # Skill 的中文名称。

    description: str
    # Skill 的简短能力说明。

    content: str
    # 已验证且仅属于当前 Task 的 SKILL.md 正文。

    content_sha256: str
    # 当前指令正文的 SHA-256，用于审计和 checkpoint 一致性检查。


class ModelProfileState(TypedDict):
    """一个可独立路由和审计的 LangChain 模型 Profile。"""

    id: str
    # Profile 的稳定唯一 ID，用于任务路由和 LLM 调用审计。

    provider: str
    # LangChain 规范 Provider 名称；支持主流原生集成、模型路由服务和 Mock。

    model: str
    # Provider 使用的模型名称，不在业务节点中硬编码。

    api_key_env: str | None
    # 保存 API Key 的环境变量名称；Mock 使用 None，绝不保存密钥实际值。

    base_url_env: str | None
    # 保存兼容服务 Base URL 的可选环境变量名称；绝不保存地址实际值。

    options_env: str | None
    # 保存 Provider 专有 JSON 构造参数的可选环境变量名称；绝不保存参数实际值。

    structured_output_method: Literal[
        "auto",
        "function_calling",
        "json_mode",
        "json_schema",
    ]
    # LangChain 结构化输出策略；auto 由对应 Provider 集成选择默认实现。

    temperature: float
    # 当前 Profile 的模型生成温度。

    max_output_tokens: int
    # 当前 Profile 单次结构化输出允许使用的最大 Token 数。

    timeout_seconds: float
    # 当前 Profile 单次模型调用超时时间，单位为秒。


class LLMConfigState(TypedDict):
    """统一 LLM Client 在一次治理运行中的多模型配置状态。"""

    enabled: bool
    # 是否允许调用真实模型；关闭时固定使用 Mock 或确定性回退。

    provider: str
    # 默认 Profile 的 Provider 兼容镜像，供旧 checkpoint 和旧调用方读取。

    model: str
    # 默认 Profile 的模型名称兼容镜像。

    api_key_env: str | None
    # 默认 Profile 的 API Key 环境变量名称兼容镜像。

    base_url_env: str | None
    # 默认 Profile 的 Base URL 环境变量名称兼容镜像。

    options_env: str | None
    # 默认 Profile 的 Provider 专有参数环境变量名称兼容镜像。

    structured_output_method: Literal[
        "auto",
        "function_calling",
        "json_mode",
        "json_schema",
    ]
    # 默认 Profile 的结构化输出方法兼容镜像。

    temperature: float
    # 默认 Profile 的生成温度兼容镜像。

    max_output_tokens: int
    # 默认 Profile 的最大输出 Token 兼容镜像。

    timeout_seconds: float
    # 默认 Profile 的调用超时兼容镜像。

    profiles: list[ModelProfileState]
    # 本次运行允许路由的模型 Profile；顺序保持请求中的声明顺序。

    default_profile_id: str
    # 未声明任务专属路由时使用的默认 Profile ID。

    task_profile_ids: dict[Literal["content", "version", "evidence"], str]
    # 三个固定 Subagent 任务类型到 Profile ID 的可选路由映射。

    fallback_enabled: bool
    # 模型失败后是否允许使用协调 Agent 或确定性逻辑继续。


class LLMCallRecord(TypedDict):
    """一次真实、Mock 或回退模型调用的审计记录。"""

    id: str
    # LLM 调用唯一 ID。

    task_id: str
    # 本次调用所属的 Task ID。

    agent_id: str
    # 发起模型调用的固定 Agent ID。

    message_id: str
    # 触发本次调用的 Team Message ID。

    model_profile_id: str
    # 实际调用或确定性回退使用的模型 Profile ID。

    provider: str
    # 实际使用的模型 Provider 名称。

    model: str
    # 实际调用的模型名称。

    status: Literal["success", "failed", "timeout", "fallback"]
    # 模型调用的最终状态。

    started_at: str
    # 模型调用开始时间，使用带时区的 ISO 8601 格式。

    finished_at: str
    # 模型调用结束时间，使用带时区的 ISO 8601 格式。

    duration_ms: int
    # 模型调用总耗时，单位为毫秒。

    input_tokens: int | None
    # Provider 返回的输入 Token 数；无法获取时为 None。

    output_tokens: int | None
    # Provider 返回的输出 Token 数；无法获取时为 None。

    total_tokens: int | None
    # Provider 返回的总 Token 数；无法获取时为 None。

    error_type: str | None
    # 失败或超时时的异常类型。

    error_message: str | None
    # 已脱敏的简短错误信息，不得包含 API Key 或完整正文。

    fallback_used: bool
    # 本次调用是否最终使用了确定性回退。


class AgentMemberState(TypedDict):
    """协调 Agent 或固定 Subagent 的运行状态。"""

    id: str
    # Agent 的稳定唯一 ID。

    role: Literal["coordinator", "content", "version", "evidence"]
    # Agent 的固定职责，不支持运行时动态招聘。

    status: Literal["idle", "working", "waiting", "failed"]
    # Agent 当前运行状态。

    current_task_id: str | None
    # Agent 当前处理的 Task ID；空闲时为 None。

    tool_names: list[str]
    # Agent 当前允许使用的工具名称。

    skill_ids: list[str]
    # 为后续 Skills 预留的引用；0.4.4 中必须保持为空列表。


class TeamState(TypedDict):
    """固定 Agent Team 的成员和协议配置。"""

    coordinator_id: str
    # 唯一协调 Agent ID。

    members: list[AgentMemberState]
    # 协调者、Content、Version 和 Evidence 四个固定成员。

    protocol_version: str
    # Team Protocol 的结构版本，例如 team-protocol-v1。

    max_parallel_agents: int
    # 同时执行的 Subagent 上限，防止文件数量导致无限并发。


class TeamMessage(TypedDict):
    """固定 Agent 之间传递的最小结构化协议消息。"""

    message_id: str
    # 团队消息唯一 ID。

    task_id: str
    # 消息所属的真实 Task ID。

    sender: str
    # 发送方 Agent ID。

    receiver: str
    # 接收方 Agent ID。

    message_type: Literal[
        "assignment",
        "progress",
        "result",
        "question",
        "error",
    ]
    # 消息用途。

    status: Literal["created", "sent", "validated", "rejected"]
    # 消息当前协议状态。

    summary: str
    # 简短消息内容，不得包含完整文档正文。

    artifact_refs: list[str]
    # 详细输入或输出所在的受控产物引用。

    error: str | None
    # 失败或协议拒绝原因；正常消息为 None。

    created_at: str
    # 消息创建时间，使用带时区的 ISO 8601 格式。


class ContentSubagentInput(TypedDict):
    """Content Subagent 的最小输入状态。"""

    task_id: str
    # 当前 Inventory Task ID。

    document_id: str
    # 等待分析的标准化文档记录 ID。

    content_preview: str
    # 有长度上限的内容预览，不允许传入完整文档正文。

    structure_summary: dict[str, Any]
    # 工作表、段落、表格或页数等结构摘要。

    key_fields: dict[str, Any]
    # 已由确定性程序提取的金额、日期和客户等关键字段。

    artifact_refs: list[str]
    # 完整标准化内容所在的产物引用，Subagent 不直接接收正文。


class VersionSubagentInput(TypedDict):
    """Version Subagent 的最小输入状态。"""

    task_id: str
    # 当前 Version Analysis Task ID。

    comparison_id: str
    # 当前文件对比较记录 ID。

    file_labels: list[str]
    # 两个候选版本的安全显示名称。

    structural_similarity: float
    # 当前确定性结构相似度。

    content_similarity: float
    # 当前确定性内容相似度。

    key_changes: list[str]
    # 确定性比较已发现的关键字段变化。

    ordering_signals: list[str]
    # 支撑版本先后关系的确定性证据。

    artifact_refs: list[str]
    # 当前比较使用的标准化产物引用。


class EvidenceSubagentInput(TypedDict):
    """Evidence Subagent 的最小输入状态。"""

    task_id: str
    # 当前 Evidence Task ID。

    group_id: str
    # 当前证据所属的版本组 ID。

    pdf_evidence_summary: str
    # PDF 来源匹配的简短结构化摘要。

    delivery_evidence_summary: str
    # 本地发送记录匹配的简短结构化摘要。

    artifact_refs: list[str]
    # PDF 匹配和发送证据的产物引用。


class ContentSubagentOutput(BaseModel):
    """Content Subagent 允许返回的结构化结果。"""

    model_config = ConfigDict(extra="forbid")
    # 禁止模型返回摘要和产物引用之外的字段。

    summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
    ]
    # 内容、结构和关键字段的简短中文摘要。

    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    # 详细结果的产物引用，必须经过引用白名单校验。


class VersionSubagentOutput(BaseModel):
    """Version Subagent 允许返回的结构化结果。"""

    model_config = ConfigDict(extra="forbid")
    # 禁止模型返回摘要和产物引用之外的字段。

    summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
    ]
    # 当前版本差异和先后关系的简短中文解释。

    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    # 详细版本解释的产物引用。


class EvidenceSubagentOutput(BaseModel):
    """Evidence Subagent 允许返回的结构化结果。"""

    model_config = ConfigDict(extra="forbid")
    # 禁止模型返回摘要和产物引用之外的字段。

    summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
    ]
    # PDF 来源和客户发送证据的简短中文说明。

    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    # 详细证据分析的产物引用。


class ContentSubagentGraphState(TypedDict):
    """Content Subagent 内部子图状态。"""

    error_context: ErrorContextState
    # Content 节点创建统一恢复错误所需的 Task 和策略上下文。

    input: ContentSubagentInput
    # 已经过 Team Protocol 校验的最小输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一多模型 LLM 配置。

    skill_context: list[SkillInstructionState]
    # 只包含当前 Inventory Task 已绑定 Skill 的指令快照。

    selected_model_profile_id: str
    # ``resolve_model_profile`` 节点为 Content 任务解析出的 Profile ID。

    system_prompt: str
    # 固定职责和只读边界组成的系统提示词，不包含业务正文。

    user_prompt: str
    # 只由内容预览、结构摘要、关键字段和受控引用组成的用户提示词。

    output: ContentSubagentOutput | None
    # Pydantic 校验后的结果；调用前为 None。

    fallback_used: bool
    # 是否使用了确定性内容摘要回退。

    team_messages: Annotated[list[TeamMessage], merge_by_message_id]
    # 本子图产生的 assignment、result 或 error Team Protocol 消息。

    llm_calls: Annotated[list[LLMCallRecord], merge_by_id]
    # 本子图产生的模型调用审计记录。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 模型调用、输出校验或产物保存错误。


class VersionSubagentGraphState(TypedDict):
    """Version Subagent 内部子图状态。"""

    error_context: ErrorContextState
    # Version 节点创建统一恢复错误所需的 Task 和策略上下文。

    input: VersionSubagentInput
    # 已经过 Team Protocol 校验的版本差异输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一多模型 LLM 配置。

    skill_context: list[SkillInstructionState]
    # 只包含当前 Version Analysis Task 已绑定 Skill 的指令快照。

    selected_model_profile_id: str
    # ``resolve_model_profile`` 节点为 Version 任务解析出的 Profile ID。

    system_prompt: str
    # 固定版本解释职责和只读边界组成的系统提示词。

    user_prompt: str
    # 只由差异、相似度、排序信号和受控引用组成的用户提示词。

    output: VersionSubagentOutput | None
    # Pydantic 校验后的版本解释结果。

    fallback_used: bool
    # 是否使用了现有确定性关键修改摘要。

    team_messages: Annotated[list[TeamMessage], merge_by_message_id]
    # 本子图产生的 assignment、result 或 error Team Protocol 消息。

    llm_calls: Annotated[list[LLMCallRecord], merge_by_id]
    # 本子图产生的模型调用审计记录。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 模型调用、输出校验或产物保存错误。


class EvidenceSubagentGraphState(TypedDict):
    """Evidence Subagent 内部子图状态。"""

    error_context: ErrorContextState
    # Evidence 节点创建统一恢复错误所需的 Task 和策略上下文。

    input: EvidenceSubagentInput
    # 已经过 Team Protocol 校验的证据摘要输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一多模型 LLM 配置。

    skill_context: list[SkillInstructionState]
    # 只包含当前 Evidence Task 已绑定 Skill 的指令快照。

    selected_model_profile_id: str
    # ``resolve_model_profile`` 节点为 Evidence 任务解析出的 Profile ID。

    system_prompt: str
    # 固定证据解释职责和只读边界组成的系统提示词。

    user_prompt: str
    # 只由 PDF、发送证据摘要和受控引用组成的用户提示词。

    output: EvidenceSubagentOutput | None
    # Pydantic 校验后的证据解释结果。

    fallback_used: bool
    # 是否使用了确定性证据说明回退。

    team_messages: Annotated[list[TeamMessage], merge_by_message_id]
    # 本子图产生的 assignment、result 或 error Team Protocol 消息。

    llm_calls: Annotated[list[LLMCallRecord], merge_by_id]
    # 本子图产生的模型调用审计记录。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 模型调用、输出校验或产物保存错误。


class FileGovernanceState(TypedDict):
    """一次完整文件版本治理任务使用的顶层状态。

    该状态在主图和子图之间传递只读输入、文件事实、版本关系、人工选择、
    生命周期配置与最终报告。循环队列等临时字段只保留在子图状态中，
    原始业务文件始终保持只读；每个版本组分别产生一个主版本推荐结果。
    """

    run: RunState
    # 本次治理任务的生命周期状态。

    request: RequestState
    # 用户提交的治理范围和判断阈值。

    workspace: WorkspaceState
    # 原始文件、临时产物和报告目录。

    prompt: PromptState
    # 本次运行加载或关闭的 System Prompt 状态。

    hooks: HookConfigState
    # 本次运行的生命周期 Hooks 配置。

    llm: LLMConfigState
    # 本次运行的模型 Provider、生成参数、超时和回退配置。

    team: TeamState
    # 协调 Agent 和三个固定 Subagent 的团队状态。

    skill_registry: SkillRegistryState
    # 顶层加载的 Skill 元数据及当前按 Task 绑定状态。

    memory: MemoryState
    # 短期阶段摘要、长期召回结果及待持久化的安全治理事实。

    context_compact: ContextCompactState
    # Context Compact 配置、最近估算结果和有界摘要索引。

    application_database: ApplicationDatabaseState
    # 七张应用表共用的独立数据库配置和当前连接状态。

    recovery: RecoveryState
    # 当前运行的恢复策略、待处理错误和恢复动作。

    hook_events: Annotated[
        list[HookEvent],
        merge_by_id,
    ]
    # 按事件 ID 合并的 Hook 执行、失败和跳过记录。

    todos: Annotated[
        list[TodoItem],
        merge_by_id,
    ]
    # 面向用户展示的 Todo；状态必须由 tasks 单向推导。

    tasks: Annotated[
        list[TaskItem],
        merge_by_task_id,
    ]
    # 当前治理运行的真实 Task DAG 和执行状态。

    team_messages: Annotated[
        list[TeamMessage],
        merge_by_message_id,
    ]
    # 按 message_id 合并的 Team Protocol 结构化消息。

    llm_calls: Annotated[
        list[LLMCallRecord],
        merge_by_id,
    ]
    # 不包含正文和密钥的模型调用耗时、Token 与错误审计记录。

    human_review: HumanReviewState
    # interrupt 暂停和恢复所需的人工确认数据。

    report: ReportState
    # 最终治理报告。

    files: Annotated[list[FileRecord], merge_by_id]
    # 扫描发现的全部原始文件。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 成功解析的标准化文档记录。

    version_groups: Annotated[list[VersionGroupRecord], merge_by_id]
    # 识别出的相互独立的文档版本组。

    diffs: Annotated[list[DiffRecord], merge_by_id]
    # 候选版本之间的内容差异。

    version_edges: Annotated[list[VersionEdge], merge_by_id]
    # 父版本到子版本以及完全重复关系。

    branches: Annotated[list[BranchRecord], merge_by_id]
    # 识别出的版本分叉。

    version_chains: Annotated[list[VersionChainRecord], merge_by_id]
    # 每个文档组整理后的版本链。

    pdf_exports: Annotated[list[PdfExportRecord], merge_by_id]
    # PDF 与可编辑源文件版本的匹配关系。

    deliveries: Annotated[list[DeliveryRecord], merge_by_id]
    # 文件曾发送给客户或获得确认的证据。

    decisions: Annotated[list[DecisionRecord], merge_by_id]
    # 每个版本组各自的主版本推荐结果。

    node_executions: Annotated[list[NodeExecutionRecord], merge_by_id]
    # 已开始、失败、完成或复用的幂等节点执行记录。

    degradations: Annotated[list[DegradationRecord], merge_by_id]
    # 本次运行应用过的安全降级及其结果影响。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 所有阶段产生的文件级或运行级错误及其恢复生命周期。


class TeamOrchestrationGraphState(TypedDict):
    """团队编排子图使用的 Task、Todo、固定 Team 和分派协议状态。"""

    error_context: ErrorContextState
    # 团队节点创建统一恢复错误所需的 Task 和策略上下文。

    run: RunState
    # 当前顶层治理运行信息，用于生成稳定 Task ID。

    llm: LLMConfigState
    # 固定 Subagent 共用的模型配置。

    team: TeamState
    # 固定团队成员、并发上限和协议版本。

    skill_registry: SkillRegistryState
    # 可按当前分派 Task 选择、加载、绑定并释放的 Skill 注册表。

    skill_selection: TaskSkillSelectionState | None
    # 本次分派生成的最小 Skill 选择；状态同步调用时为 None。

    skill_context: list[SkillInstructionState]
    # 已加载且绑定到当前分派 Task 的 Skill 指令快照。

    task_update: TaskStatusUpdate | None
    # 顶层流程传入的单次状态更新；首次创建 DAG 时可以为 None。

    dispatch_request: ContentSubagentInput | VersionSubagentInput | EvidenceSubagentInput | None
    # 可选 Subagent 分派请求；状态同步调用或请求消费完成后为 None。

    dispatch_result: ContentSubagentOutput | VersionSubagentOutput | EvidenceSubagentOutput | None
    # 当前 Subagent 调用产生的 Pydantic 结构化结果。

    tasks: Annotated[
        list[TaskItem],
        merge_by_task_id,
    ]
    # 按 task_id 合并的真实 Task 列表。

    todos: Annotated[
        list[TodoItem],
        merge_by_id,
    ]
    # 由最新 Task 列表重新生成的用户可见 Todo。

    team_messages: Annotated[
        list[TeamMessage],
        merge_by_message_id,
    ]
    # 当前编排调用读取或产生的 Team Protocol 消息。

    llm_calls: Annotated[
        list[LLMCallRecord],
        merge_by_id,
    ]
    # 当前编排调用读取或产生的模型审计记录。

    errors: Annotated[
        list[ErrorRecord],
        merge_by_id,
    ]
    # Task DAG 校验或状态转换产生的结构化错误。


class InventoryGraphState(TypedDict):
    """文件发现与内容提取子图使用的状态。"""

    error_context: ErrorContextState
    # Inventory 节点创建统一恢复错误所需的 Task 和策略上下文。

    request: RequestState
    # 文件扫描范围、扩展名和数量限制。

    workspace: WorkspaceState
    # 只读输入目录和内容产物目录。

    discovered_paths: list[str]
    # 子图内部暂存的待登记文件路径。

    parse_queue: list[str]
    # 尚未处理的文件 ID 队列。

    current_file_id: str | None
    # 当前正在解析的文件 ID。

    current_raw_content: Annotated[RawExtractedContent | None, UntrackedValue]
    # 当前解析器产生的临时原始内容；UntrackedValue 确保正文不会写入 checkpoint。

    current_document: DocumentRecord | None
    # 当前完成标准化、等待正式写入结果列表的文档记录。

    current_parse_error: str | None
    # 当前文件的解析或标准化错误。

    files: Annotated[list[FileRecord], merge_by_id]
    # 扫描发现并登记的文件。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 成功解析并标准化的文档。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 扫描和解析阶段产生的错误。


class VersionAnalysisGraphState(TypedDict):
    """版本分组、比较、建链和当前推荐子图使用的状态。"""

    error_context: ErrorContextState
    # Version Analysis 节点创建统一恢复错误所需的 Task 和策略上下文。

    run: RunState
    # 当前顶层治理运行信息，用于构造真实 Version Analysis Task ID。

    request: RequestState
    # 分组相似度和自动选择置信度等参数。

    llm: LLMConfigState
    # Version Subagent 后续使用的统一模型配置。

    team: TeamState
    # 用于定位固定 Version Subagent 和协调 Agent 的团队状态。

    skill_registry: SkillRegistryState
    # Version 分派按比较 Task 临时绑定并在返回后释放的 Skill 注册表。

    tasks: Annotated[list[TaskItem], merge_by_task_id]
    # Team Orchestration 校验 Version Subagent 分派所需的真实 Task DAG。

    todos: Annotated[list[TodoItem], merge_by_id]
    # 由 Task 单向生成并随内部编排调用同步的用户进度投影。

    files: Annotated[list[FileRecord], merge_by_id]
    # 参与版本分析的文件记录。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 文件对应的标准化内容记录。

    version_groups: Annotated[list[VersionGroupRecord], merge_by_id]
    # 当前识别出的版本组。

    comparison_jobs: Annotated[list[ComparisonJob], merge_by_id]
    # 子图内部的候选文件对比较任务。

    comparison_queue: list[str]
    # 尚未处理的文件对比较任务 ID 队列。

    current_comparison_id: str | None
    # 当前正在执行的比较任务 ID。

    current_diff: DiffRecord | None
    # 当前比较任务尚未正式写入结果列表的差异草稿。

    current_version_subagent_input: VersionSubagentInput | None
    # 根据当前确定性差异构造、且不包含完整正文的 Version 最小输入。

    current_version_subagent_output: VersionSubagentOutput | None
    # Team Orchestration 返回的可选 Version Pydantic 结构化结果。

    current_comparison_error: str | None
    # 当前文件对比较产生的错误。

    diffs: Annotated[list[DiffRecord], merge_by_id]
    # 已完成的文件内容差异记录。

    version_edges: Annotated[list[VersionEdge], merge_by_id]
    # 推断出的版本先后和重复关系。

    branches: Annotated[list[BranchRecord], merge_by_id]
    # 识别出的版本分叉。

    version_chains: Annotated[list[VersionChainRecord], merge_by_id]
    # 根据版本边生成的可读版本链。

    decisions: Annotated[list[DecisionRecord], merge_by_id]
    # 第一至第三批期间仍由版本分析子图产生的主版本推荐结果。

    human_review: HumanReviewState
    # 第一至第三批期间仍由版本分析子图返回的人工确认状态。

    team_messages: Annotated[list[TeamMessage], merge_by_message_id]
    # Version Subagent 后续产生的 assignment、result 或 error 消息。

    llm_calls: Annotated[list[LLMCallRecord], merge_by_id]
    # Version Subagent 后续产生的模型调用审计记录。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 分组、比较、版本关系建图和当前推荐阶段产生的错误。


class EvidenceGraphState(TypedDict):
    """PDF 来源与本地发送记录匹配子图使用的状态。"""

    error_context: ErrorContextState
    # Evidence 节点创建统一恢复错误所需的 Task 和策略上下文。

    run: RunState
    # 当前治理运行 ID，供证据 Memory 建立安全来源关联。

    request: RequestState
    # PDF 匹配阈值和本地发送日志路径。

    files: Annotated[list[FileRecord], merge_by_id]
    # Inventory 阶段发现的全部文件。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 用于 PDF 和可编辑版本内容匹配的标准化文档。

    version_groups: Annotated[list[VersionGroupRecord], merge_by_id]
    # 用于限制 PDF 来源候选范围的版本组。

    memory: MemoryState
    # Evidence 阶段读取和追加的短期、长期 Memory 缓冲区。

    pdf_candidate_ids: list[str]
    # 子图内部收集到的非重复、已解析 PDF 文件 ID。

    pdf_match_jobs: Annotated[list[PdfMatchJob], merge_by_id]
    # PDF 来源匹配任务及其执行状态。

    delivery_log_entries: list[DeliveryLogEntry]
    # 从本地发送日志加载的原始证据记录。

    pdf_exports: Annotated[list[PdfExportRecord], merge_by_id]
    # PDF 与可编辑源版本的匹配结果。

    deliveries: Annotated[list[DeliveryRecord], merge_by_id]
    # 本地发送记录与文件版本的匹配结果。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # PDF 来源和发送记录匹配阶段产生的错误。


class RecommendationGraphState(TypedDict):
    """结合版本关系和外部证据推荐各版本组主版本的子图状态。"""

    error_context: ErrorContextState
    # Recommendation 节点创建统一恢复错误所需的 Task 和策略上下文。

    run: RunState
    # 当前治理运行 ID，供推荐 Memory 建立安全来源关联。

    request: RequestState
    # 自动推荐阈值及 PDF 匹配阈值。

    files: Annotated[list[FileRecord], merge_by_id]
    # 所有可参与推荐的文件记录。

    version_groups: Annotated[list[VersionGroupRecord], merge_by_id]
    # 相互独立的文档版本组。

    diffs: Annotated[list[DiffRecord], merge_by_id]
    # 候选版本之间可用于解释推荐的关键差异。

    version_edges: Annotated[list[VersionEdge], merge_by_id]
    # 父版本、子版本和重复版本关系。

    branches: Annotated[list[BranchRecord], merge_by_id]
    # 需要降低置信度或人工确认的版本分叉。

    version_chains: Annotated[list[VersionChainRecord], merge_by_id]
    # 每个版本组整理后的完整版本链。

    pdf_exports: Annotated[list[PdfExportRecord], merge_by_id]
    # PDF 与可编辑源版本的匹配证据。

    deliveries: Annotated[list[DeliveryRecord], merge_by_id]
    # 客户发送和确认记录。

    memory: MemoryState
    # 供候选评分读取的历史选择，以及本阶段产生的短期摘要。

    candidate_sets: Annotated[list[RecommendationCandidateSet], merge_by_id]
    # 每个版本组内部使用的推荐候选集合。

    decisions: Annotated[list[DecisionRecord], merge_by_id]
    # 每个版本组的评分、推荐和保留策略。

    human_review: HumanReviewState
    # 需要顶层图执行 interrupt 的版本组与用户选择。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 候选评分、证据规则或推荐验证错误。


class RecoveryGraphState(TypedDict):
    """Error Recovery 子图使用的策略、持久化配置和最小业务共享状态。"""

    run: RunState
    # 当前治理运行及 recovering、waiting_human 等生命周期状态。

    request: RequestState
    # 人工修正输入路径时允许更新的治理请求；其余业务参数保持只读。

    workspace: WorkspaceState
    # 用于校验替换路径和安全读取幂等状态更新产物的工作空间。

    application_database: ApplicationDatabaseState
    # 恢复记录和节点执行记录使用的独立应用数据库配置。

    tasks: Annotated[
        list[TaskItem],
        merge_by_task_id,
    ]
    # 发生错误、正在重试或部分完成的 Task。

    errors: Annotated[
        list[ErrorRecord],
        merge_by_id,
    ]
    # 等待分类或已经恢复的错误记录。

    node_executions: Annotated[
        list[NodeExecutionRecord],
        merge_by_id,
    ]
    # 用于重试检查和已完成结果复用的节点执行记录。

    degradations: Annotated[
        list[DegradationRecord],
        merge_by_id,
    ]
    # 恢复子图产生的安全降级记录。

    recovery: RecoveryState
    # 当前恢复策略、错误队列、跳转目标和人工输入。
