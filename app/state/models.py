from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.state.reducers import merge_by_id, merge_by_message_id, merge_by_task_id

"""本模块定义文件版本治理的顶层状态、子图状态和业务记录结构。"""


class RunState(TypedDict):
    """一次文件版本治理运行的生命周期状态。"""

    run_id: str
    # 本次运行的唯一标识。

    status: Literal[
        "created",
        "running",
        "waiting_human",
        "completed",
        "partial",
        "failed",
    ]
    # 当前运行状态；存在非致命文件错误时最终状态为 partial。

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
    # 确定性规则或 LLM 生成的差异摘要。

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


class ErrorRecord(TypedDict):
    """节点执行过程中产生的结构化错误。"""

    id: str
    # 错误记录唯一 ID。

    stage: str
    # 错误发生的主流程阶段或子图名称。

    node_name: str
    # 发生错误的节点函数名。

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
        "unknown",
    ]
    # 错误类别；protocol 表示 Team Message 或分派契约错误。

    message: str
    # 可供日志和报告展示的错误说明。

    related_file_id: str | None
    # 与错误相关的文件 ID；非文件错误时为 None。

    fatal: bool
    # 是否导致整个治理任务无法继续。


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
        "completed",
        "failed",
        "skipped",
    ]
    # Task 当前真实执行状态。

    dependencies: list[str]
    # 普通 Task 启动前须依赖成功终结；Report Task 可在依赖进入任一终态后收口。

    assigned_role: Literal[
        "coordinator",
        "content",
        "version",
        "evidence",
    ]
    # 当前 Task 的固定负责角色；0.4.3 由 Team Orchestration 实际选择并调用。

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

    status: Literal[
        "running",
        "completed",
        "failed",
        "skipped",
    ]
    # 目标 Task 需要进入的新状态。

    output_refs: list[str]
    # 本阶段新产生的状态记录或产物引用。

    error: str | None
    # 失败或阻断原因；成功和正常跳过时为 None。

    updated_at: str
    # 本次状态变更发生时间，使用带时区的 ISO 8601 格式。


class LLMConfigState(TypedDict):
    """统一 LLM Client 在一次治理运行中的配置状态。"""

    enabled: bool
    # 是否允许调用真实模型；关闭时固定使用 Mock 或确定性回退。

    provider: Literal["openai", "mock"]
    # 当前模型 Provider；支持 OpenAI Provider 和 Mock Provider。

    model: str
    # Provider 使用的模型名称，由配置提供，不在业务节点中硬编码。

    api_key_env: str | None
    # 保存 API Key 的环境变量名称；Mock Provider 使用 None，绝不保存密钥实际值。

    temperature: float
    # 模型生成温度；版本治理摘要建议使用较低温度。

    max_output_tokens: int
    # 单次模型结构化输出允许使用的最大 Token 数。

    timeout_seconds: float
    # 单次模型调用超时时间，单位为秒。

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
    # 为后续 Skills 预留的引用；0.4.3 中必须保持为空列表。


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

    input: ContentSubagentInput
    # 已经过 Team Protocol 校验的最小输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一 LLM 配置。

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

    input: VersionSubagentInput
    # 已经过 Team Protocol 校验的版本差异输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一 LLM 配置。

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

    input: EvidenceSubagentInput
    # 已经过 Team Protocol 校验的证据摘要输入。

    team: TeamState
    # 用于校验 assignment、result 和 error 消息的固定团队状态。

    llm: LLMConfigState
    # 当前运行使用的统一 LLM 配置。

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

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 所有阶段产生的文件级或运行级错误。


class TeamOrchestrationGraphState(TypedDict):
    """团队编排子图使用的 Task、Todo、固定 Team 和分派协议状态。"""

    run: RunState
    # 当前顶层治理运行信息，用于生成稳定 Task ID。

    llm: LLMConfigState
    # 固定 Subagent 共用的模型配置。

    team: TeamState
    # 固定团队成员、并发上限和协议版本。

    task_update: TaskStatusUpdate | None
    # 顶层流程传入的单次状态更新；首次创建 DAG 时可以为 None。

    dispatch_request: (
        ContentSubagentInput
        | VersionSubagentInput
        | EvidenceSubagentInput
        | None
    )
    # 可选 Subagent 分派请求；状态同步调用或请求消费完成后为 None。

    dispatch_result: (
        ContentSubagentOutput
        | VersionSubagentOutput
        | EvidenceSubagentOutput
        | None
    )
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

    current_raw_content: RawExtractedContent | None
    # 当前解析器产生的临时原始内容。

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

    request: RequestState
    # 分组相似度和自动选择置信度等参数。

    llm: LLMConfigState
    # Version Subagent 后续使用的统一模型配置。

    team: TeamState
    # 用于定位固定 Version Subagent 和协调 Agent 的团队状态。

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

    request: RequestState
    # PDF 匹配阈值和本地发送日志路径。

    files: Annotated[list[FileRecord], merge_by_id]
    # Inventory 阶段发现的全部文件。

    documents: Annotated[list[DocumentRecord], merge_by_id]
    # 用于 PDF 和可编辑版本内容匹配的标准化文档。

    version_groups: Annotated[list[VersionGroupRecord], merge_by_id]
    # 用于限制 PDF 来源候选范围的版本组。

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

    candidate_sets: Annotated[list[RecommendationCandidateSet], merge_by_id]
    # 每个版本组内部使用的推荐候选集合。

    decisions: Annotated[list[DecisionRecord], merge_by_id]
    # 每个版本组的评分、推荐和保留策略。

    human_review: HumanReviewState
    # 需要顶层图执行 interrupt 的版本组与用户选择。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 候选评分、证据规则或推荐验证错误。
