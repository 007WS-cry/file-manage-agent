from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from app.state.reducers import merge_by_id

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
    """用户提交的文件治理范围和判断参数。"""

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
        "llm",
        "validation",
        "unknown",
    ]
    # 错误类别。

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


class FileGovernanceState(TypedDict):
    """一次完整文件版本治理任务使用的顶层状态。

    该状态在主图和子图之间传递只读输入、文件事实、版本关系、人工选择
    与最终报告。循环队列等临时字段只保留在子图状态中，原始业务文件始终
    保持只读；每个版本组分别产生一个主版本推荐结果。
    """

    run: RunState
    # 本次治理任务的生命周期状态。

    request: RequestState
    # 用户提交的治理范围和判断阈值。

    workspace: WorkspaceState
    # 原始文件、临时产物和报告目录。

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

    decisions: Annotated[list[DecisionRecord], merge_by_id]
    # 每个文档组各自的主版本推荐结果。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 所有阶段产生的文件级或运行级错误。


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
    """版本分组、比较、建链和推荐子图使用的状态。"""

    request: RequestState
    # 分组相似度和自动选择置信度等参数。

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
    # 每个版本组的主版本推荐结果。

    human_review: HumanReviewState
    # 需要返回顶层图的待人工确认版本组和用户选择。

    errors: Annotated[list[ErrorRecord], merge_by_id]
    # 比较、建链和推荐阶段产生的错误。
