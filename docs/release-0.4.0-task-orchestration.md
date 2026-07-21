# 0.3.0 完整 Docker 运行步骤

本流程使用：

- 初始业务文件：`data/input`
- 所有运行产物：`.artifacts`
- 人工确认结果：`review_response.json`
- Docker 镜像：`file-manage-agent:0.3.0`
- Prompt 和 Hooks：启用，用于验证 0.3.0 新功能

## 1. 清理并初始化 `.artifacts`

只清理 `.artifacts`，不会修改 `data/input`、`review_response.json` 或其他项目文件。

```powershell
Set-Location "F:\Agent\file-manage-agent"

$projectRoot = (Get-Location).Path
$inputRoot = Join-Path $projectRoot "data\input"
$artifactRoot = Join-Path $projectRoot ".artifacts"
$contentRoot = Join-Path $artifactRoot "content"
$checkpointRoot = Join-Path $artifactRoot "checkpoints"
$reportRoot = Join-Path $artifactRoot "reports"
$runtimeRoot = Join-Path $artifactRoot "runtime"

$expectedArtifactRoot = Join-Path $projectRoot ".artifacts"

if (
    [System.IO.Path]::GetFullPath($artifactRoot) -ne
    [System.IO.Path]::GetFullPath($expectedArtifactRoot)
) {
    throw "拒绝清理非预期目录：$artifactRoot"
}

if (-not (Test-Path -LiteralPath $inputRoot -PathType Container)) {
    throw "输入目录不存在：$inputRoot"
}

if (Test-Path -LiteralPath $artifactRoot -PathType Container) {
    Get-ChildItem -LiteralPath $artifactRoot -Force |
        Remove-Item -Recurse -Force
}

New-Item -ItemType Directory -Force -Path @(
    $contentRoot,
    $checkpointRoot,
    $reportRoot,
    $runtimeRoot
) | Out-Null
```

最终顶层目录结构：

```text
.artifacts/
├── content/                  # 标准化内容和中间产物
├── checkpoints/             # SQLite checkpoint
├── runtime/                 # CLI JSON 结果和标准错误输出
└── reports/                 # 最终 Markdown 报告
```

运行过程中，`content` 下可能继续创建：

```text
.artifacts/content/
├── normalized/              # 标准化文档内容
└── intermediate/            # 可选中间 JSON 产物
```

## 2. 检查初始业务文件

所有待治理文件必须提前放入：

```text
F:\Agent\file-manage-agent\data\input
```

执行检查：

```powershell
$inputFiles = @(
    Get-ChildItem -LiteralPath $inputRoot -Recurse -File
)

if ($inputFiles.Count -eq 0) {
    throw "data/input 中没有业务文件"
}

$supportedFiles = @(
    $inputFiles | Where-Object {
        @(".xlsx", ".docx", ".pdf") -contains $_.Extension.ToLowerInvariant()
    }
)

if ($supportedFiles.Count -eq 0) {
    throw "没有找到受支持的 .xlsx、.docx 或 .pdf 文件"
}

Write-Host "输入文件总数：$($inputFiles.Count)"
Write-Host "受支持文件数：$($supportedFiles.Count)"

$supportedFiles |
    Select-Object FullName, Length, LastWriteTime |
    Format-Table -AutoSize
```

## 3. 创建 Docker 请求文件

在项目根目录手工创建：

```text
F:\Agent\file-manage-agent\docker_request.json
```

内容如下：

```json
{
  "request": {
    "root_directory": "/data/input",
    "recursive": true,
    "allowed_extensions": [
      ".xlsx",
      ".docx",
      ".pdf"
    ],
    "max_files": 500,
    "grouping_similarity_threshold": 0.72,
    "auto_select_threshold": 1.0,
    "pdf_match_threshold": 0.82,
    "delivery_log_path": null,
    "use_llm_summary": false
  },
  "workspace": {
    "input_root": "/data/input",
    "input_readonly": true,
    "artifact_root": "/data/artifacts/content",
    "report_root": "/data/artifacts/reports"
  },
  "prompt": {
    "enabled": true,
    "version": "file-governance-v1",
    "source_path": "/app/resources/prompts/file_governance_system_v1.md",
    "dynamic_rules": [
      "原始业务文件必须保持只读。",
      "低置信度主版本选择必须交由人工确认。"
    ]
  },
  "hooks": {
    "enabled": true,
    "before_run": [
      "validate_request_envelope_hook",
      "enrich_run_state_hook",
      "initialize_tool_audit_hook"
    ],
    "before_model": [],
    "after_model": [],
    "after_run": [
      "validate_report_result_hook",
      "flush_tool_audit_hook",
      "cleanup_run_resources_hook"
    ],
    "default_failure_policy": "block",
    "failure_policies": {
      "initialize_tool_audit_hook": "ignore",
      "flush_tool_audit_hook": "ignore",
      "cleanup_run_resources_hook": "ignore"
    }
  },
  "checkpoint": {
    "backend": "sqlite",
    "database_path": "/data/artifacts/checkpoints/file-governance.sqlite3"
  }
}
```

这里将 `auto_select_threshold` 设置为 `1.0`，用于提高进入人工确认流程的概率。如果运行结果不需要人工确认，程序会直接生成最终报告。

文件必须保存为 UTF-8 无 BOM。

执行校验：

```powershell
$requestFile = Join-Path $projectRoot "docker_request.json"

if (-not (Test-Path -LiteralPath $requestFile -PathType Leaf)) {
    throw "请求文件不存在：$requestFile"
}

$requestBytes = [System.IO.File]::ReadAllBytes($requestFile)

if (
    $requestBytes.Length -ge 3 -and
    $requestBytes[0] -eq 0xEF -and
    $requestBytes[1] -eq 0xBB -and
    $requestBytes[2] -eq 0xBF
) {
    throw "docker_request.json 必须保存为 UTF-8 无 BOM"
}

$requestText = [System.IO.File]::ReadAllText(
    $requestFile,
    [System.Text.Encoding]::UTF8
)

$requestObject = $requestText | ConvertFrom-Json

if ($null -eq $requestObject.prompt) {
    throw "docker_request.json 缺少 prompt 对象"
}

if ($null -eq $requestObject.hooks) {
    throw "docker_request.json 缺少 hooks 对象"
}

Write-Host "docker_request.json 校验通过"
```

## 4. 构建 0.3.0 Docker 镜像

检查 Docker：

```powershell
docker version

if ($LASTEXITCODE -ne 0) {
    throw "Docker 不可用，请确认 Docker Desktop 已启动"
}
```

构建镜像：

```powershell
docker build `
    --build-arg APP_VERSION=0.3.0 `
    --tag file-manage-agent:0.3.0 `
    .

if ($LASTEXITCODE -ne 0) {
    throw "Docker 镜像构建失败"
}

docker image inspect file-manage-agent:0.3.0 | Out-Null
```

Dockerfile 会复制：

```text
resources/prompts/file_governance_system_v1.md
```

进入镜像。继续验证镜像版本和 Prompt：

```powershell
$verifyCode = @'
from pathlib import Path
import app
from app.state.factories import DEFAULT_PROMPT_SOURCE_PATH

assert app.__version__ == "0.3.0"
assert Path("/app/resources/prompts/file_governance_system_v1.md").is_file()
assert Path(DEFAULT_PROMPT_SOURCE_PATH).is_file()

print(f"应用版本：{app.__version__}")
print(f"默认 Prompt：{DEFAULT_PROMPT_SOURCE_PATH}")
'@

docker run `
    --rm `
    --entrypoint python `
    file-manage-agent:0.3.0 `
    -c $verifyCode

if ($LASTEXITCODE -ne 0) {
    throw "镜像版本或 Prompt 资源验证失败"
}
```

预期输出包含：

```text
应用版本：0.3.0
默认 Prompt：/app/resources/prompts/file_governance_system_v1.md
```

## 5. 第一次运行

第一次运行结果固定写入：

```text
.artifacts/runtime/first-run.json
```

标准错误写入：

```text
.artifacts/runtime/first-run.stderr.txt
```

执行：

```powershell
$firstRunFile = Join-Path $runtimeRoot "first-run.json"
$firstRunErrorFile = Join-Path $runtimeRoot "first-run.stderr.txt"
$checkpointPath = "/data/artifacts/checkpoints/file-governance.sqlite3"

$firstArguments = @(
    "run",
    "--rm",
    "--mount",
    "type=bind,src=$inputRoot,dst=/data/input,readonly",
    "--mount",
    "type=bind,src=$artifactRoot,dst=/data/artifacts",
    "--mount",
    "type=bind,src=$requestFile,dst=/config/request.json,readonly",
    "file-manage-agent:0.3.0",
    "run",
    "/config/request.json",
    "--checkpoint-backend",
    "sqlite",
    "--checkpoint-path",
    $checkpointPath
)

$firstProcess = Start-Process `
    -FilePath "docker.exe" `
    -ArgumentList $firstArguments `
    -NoNewWindow `
    -Wait `
    -PassThru `
    -RedirectStandardOutput $firstRunFile `
    -RedirectStandardError $firstRunErrorFile

$firstError = [System.IO.File]::ReadAllText(
    $firstRunErrorFile,
    [System.Text.Encoding]::UTF8
)

if ($firstProcess.ExitCode -ne 0) {
    throw "第一次 Docker 运行失败：`n$firstError"
}

$firstRunText = [System.IO.File]::ReadAllText(
    $firstRunFile,
    [System.Text.Encoding]::UTF8
)

try {
    $firstRun = $firstRunText | ConvertFrom-Json
}
catch {
    throw "第一次运行没有返回合法 JSON：`n$firstRunText`n$firstError"
}

$firstRun | ConvertTo-Json -Depth 20
```

检查结果：

```powershell
if ([string]::IsNullOrWhiteSpace([string]$firstRun.thread_id)) {
    throw "第一次运行没有返回 thread_id"
}

Write-Host "thread_id：$($firstRun.thread_id)"
Write-Host "运行状态：$($firstRun.status)"
Write-Host "报告路径：$($firstRun.report_path)"
```

可能出现的状态：

- `waiting_human`：需要继续完成人工确认。
- `completed`：已完成，可以直接跳到第 10 步。
- `partial`：存在非致命警告，可以直接跳到第 10 步。
- `failed`：已生成失败报告，可以跳到第 10 步查看原因。

如果状态不是 `waiting_human`，跳到第 10 步。

## 6. 显示人工审核内容

本步骤重新读取磁盘中的 `first-run.json`，不依赖前面 PowerShell 会话里的临时变量。

```powershell
$firstRunText = [System.IO.File]::ReadAllText(
    $firstRunFile,
    [System.Text.Encoding]::UTF8
)

$firstRun = $firstRunText | ConvertFrom-Json

$reviewInterrupt = $firstRun.interrupts |
    Where-Object {
        $null -ne $_ -and
        $_.kind -eq "file_governance_review"
    } |
    Select-Object -First 1

if ($null -eq $reviewInterrupt) {
    throw "第一次运行结果中没有 file_governance_review"
}

$reviewGroups = @(
    $reviewInterrupt.groups |
    Where-Object {
        $null -ne $_ -and
        -not [string]::IsNullOrWhiteSpace([string]$_.group_id)
    }
)

if ($reviewGroups.Count -eq 0) {
    throw "第一次运行结果中没有有效的待审核版本组"
}

foreach ($group in $reviewGroups) {
    Write-Host ""
    Write-Host "版本组：$($group.label)"
    Write-Host "group_id：$($group.group_id)"
    Write-Host "推荐 file_id：$($group.recommended_file_id)"
    Write-Host "置信度：$($group.confidence)"
    Write-Host "推荐理由："

    @($group.reasons) | ForEach-Object {
        Write-Host "  - $_"
    }

    Write-Host "候选文件："

    @($group.candidates) |
        Select-Object file_id, file_name, score |
        Format-Table -AutoSize
}
```

## 7. 用户创建 `review_response.json`

根据第 6 步展示的真实 `group_id` 和 `file_id`，在项目根目录手工创建：

```text
F:\Agent\file-manage-agent\review_response.json
```

格式：

```json
{
  "selections": {
    "实际group_id": "该组中用户选择的实际file_id",
    "另一个实际group_id": "对应组中用户选择的实际file_id"
  },
  "review_note": "已逐组核对并确认主版本"
}
```

注意：

- 每个待审核版本组必须恰好出现一次。
- 选择的 `file_id` 必须来自该组的 `candidates`。
- 不得使用示例中的占位 ID。
- 文件必须由用户确认后手工创建。
- 文件必须保存为 UTF-8 无 BOM。
- 不要把该文件放进 `.artifacts`。

## 8. 校验人工确认文件

该步骤可以在关闭并重新打开 PowerShell 后独立执行。

```powershell
Set-Location "F:\Agent\file-manage-agent"

$projectRoot = (Get-Location).Path
$inputRoot = Join-Path $projectRoot "data\input"
$artifactRoot = Join-Path $projectRoot ".artifacts"
$runtimeRoot = Join-Path $artifactRoot "runtime"
$reportRoot = Join-Path $artifactRoot "reports"

$firstRunFile = Join-Path $runtimeRoot "first-run.json"
$reviewFile = Join-Path $projectRoot "review_response.json"

if (-not (Test-Path -LiteralPath $firstRunFile -PathType Leaf)) {
    throw "第一次运行结果不存在：$firstRunFile"
}

if (-not (Test-Path -LiteralPath $reviewFile -PathType Leaf)) {
    throw "人工确认文件不存在：$reviewFile"
}
```

检查 `review_response.json` 是否为 UTF-8 无 BOM：

```powershell
$reviewBytes = [System.IO.File]::ReadAllBytes($reviewFile)

if (
    $reviewBytes.Length -ge 3 -and
    $reviewBytes[0] -eq 0xEF -and
    $reviewBytes[1] -eq 0xBB -and
    $reviewBytes[2] -eq 0xBF
) {
    throw "review_response.json 必须保存为 UTF-8 无 BOM"
}
```

重新读取两个 JSON 文件：

```powershell
$firstRunText = [System.IO.File]::ReadAllText(
    $firstRunFile,
    [System.Text.Encoding]::UTF8
)

$reviewText = [System.IO.File]::ReadAllText(
    $reviewFile,
    [System.Text.Encoding]::UTF8
)

$firstRun = $firstRunText | ConvertFrom-Json
$reviewResponse = $reviewText | ConvertFrom-Json
```

重新提取版本组：

```powershell
$reviewInterrupt = $firstRun.interrupts |
    Where-Object {
        $null -ne $_ -and
        $_.kind -eq "file_governance_review"
    } |
    Select-Object -First 1

if ($null -eq $reviewInterrupt) {
    throw "first-run.json 中没有人工审核中断信息"
}

$reviewGroups = @(
    $reviewInterrupt.groups |
    Where-Object {
        $null -ne $_ -and
        -not [string]::IsNullOrWhiteSpace([string]$_.group_id)
    }
)

if ($reviewGroups.Count -eq 0) {
    throw "first-run.json 中没有有效版本组"
}

if ($null -eq $reviewResponse.selections) {
    throw "review_response.json 缺少 selections 对象"
}

$selectionProperties = @(
    $reviewResponse.selections.PSObject.Properties |
    Where-Object {
        $null -ne $_ -and
        -not [string]::IsNullOrWhiteSpace([string]$_.Name)
    }
)

$expectedGroupIds = @(
    $reviewGroups | ForEach-Object {
        [string]$_.group_id
    }
)

$actualGroupIds = @(
    $selectionProperties | ForEach-Object {
        [string]$_.Name
    }
)

$missingGroupIds = @(
    $expectedGroupIds | Where-Object {
        $_ -notin $actualGroupIds
    }
)

$extraGroupIds = @(
    $actualGroupIds | Where-Object {
        $_ -notin $expectedGroupIds
    }
)

if ($missingGroupIds.Count -gt 0) {
    throw "人工确认缺少版本组：$($missingGroupIds -join ', ')"
}

if ($extraGroupIds.Count -gt 0) {
    throw "人工确认包含未知版本组：$($extraGroupIds -join ', ')"
}

if ($selectionProperties.Count -ne $reviewGroups.Count) {
    throw "人工确认数量与待审核版本组数量不一致"
}
```

校验每个 `file_id`：

```powershell
foreach ($group in $reviewGroups) {
    $groupId = [string]$group.group_id
    $selectionProperty =
        $reviewResponse.selections.PSObject.Properties[$groupId]

    if ($null -eq $selectionProperty) {
        throw "版本组 $groupId 没有人工选择"
    }

    $selectedFileId = [string]$selectionProperty.Value

    if ([string]::IsNullOrWhiteSpace($selectedFileId)) {
        throw "版本组 $groupId 的人工选择为空"
    }

    $candidateIds = @(
        $group.candidates |
        Where-Object {
            $null -ne $_ -and
            -not [string]::IsNullOrWhiteSpace([string]$_.file_id)
        } |
        ForEach-Object {
            [string]$_.file_id
        }
    )

    if ($selectedFileId -notin $candidateIds) {
        throw "版本组 $groupId 选择了不属于该组的文件：$selectedFileId"
    }
}

Write-Host "review_response.json 校验通过"
```

## 9. 恢复人工确认后的运行

`thread_id` 从 `.artifacts/runtime/first-run.json` 重新读取。

```powershell
$threadId = [string]$firstRun.thread_id

if ([string]::IsNullOrWhiteSpace($threadId)) {
    throw "first-run.json 缺少 thread_id"
}

$checkpointPath = "/data/artifacts/checkpoints/file-governance.sqlite3"
$resumeRunFile = Join-Path $runtimeRoot "resume-run.json"
$resumeErrorFile = Join-Path $runtimeRoot "resume-run.stderr.txt"

$resumeArguments = @(
    "run",
    "--rm",
    "--mount",
    "type=bind,src=$inputRoot,dst=/data/input,readonly",
    "--mount",
    "type=bind,src=$artifactRoot,dst=/data/artifacts",
    "--mount",
    "type=bind,src=$reviewFile,dst=/config/review_response.json,readonly",
    "file-manage-agent:0.3.0",
    "resume",
    "/config/review_response.json",
    "--thread-id",
    $threadId,
    "--checkpoint-path",
    $checkpointPath
)

$resumeProcess = Start-Process `
    -FilePath "docker.exe" `
    -ArgumentList $resumeArguments `
    -NoNewWindow `
    -Wait `
    -PassThru `
    -RedirectStandardOutput $resumeRunFile `
    -RedirectStandardError $resumeErrorFile

$resumeError = [System.IO.File]::ReadAllText(
    $resumeErrorFile,
    [System.Text.Encoding]::UTF8
)

if ($resumeProcess.ExitCode -ne 0) {
    throw "Docker 恢复失败：`n$resumeError"
}

$resumeRunText = [System.IO.File]::ReadAllText(
    $resumeRunFile,
    [System.Text.Encoding]::UTF8
)

try {
    $finalResult = $resumeRunText | ConvertFrom-Json
}
catch {
    throw "恢复运行没有返回合法 JSON：`n$resumeRunText`n$resumeError"
}

$finalResult | ConvertTo-Json -Depth 20
```

检查：

```powershell
if ($finalResult.status -eq "waiting_human") {
    throw "恢复后仍处于 waiting_human，请检查人工确认是否覆盖全部版本组"
}

Write-Host "恢复后状态：$($finalResult.status)"
Write-Host "恢复后报告：$($finalResult.report_path)"
```

## 10. 读取最终结果和 Markdown 报告

本步骤同样可以在新的 PowerShell 会话中独立运行。

```powershell
Set-Location "F:\Agent\file-manage-agent"

$projectRoot = (Get-Location).Path
$artifactRoot = Join-Path $projectRoot ".artifacts"
$runtimeRoot = Join-Path $artifactRoot "runtime"
$reportRoot = Join-Path $artifactRoot "reports"

$firstRunFile = Join-Path $runtimeRoot "first-run.json"
$resumeRunFile = Join-Path $runtimeRoot "resume-run.json"

if (-not (Test-Path -LiteralPath $firstRunFile -PathType Leaf)) {
    throw "第一次运行结果不存在：$firstRunFile"
}

$firstRunText = [System.IO.File]::ReadAllText(
    $firstRunFile,
    [System.Text.Encoding]::UTF8
)

$firstRun = $firstRunText | ConvertFrom-Json

if ($firstRun.status -eq "waiting_human") {
    if (-not (Test-Path -LiteralPath $resumeRunFile -PathType Leaf)) {
        throw "第一次运行需要人工确认，但恢复结果不存在：$resumeRunFile"
    }

    $resumeRunText = [System.IO.File]::ReadAllText(
        $resumeRunFile,
        [System.Text.Encoding]::UTF8
    )

    $finalResult = $resumeRunText | ConvertFrom-Json
}
else {
    $finalResult = $firstRun
}
```

定位宿主机报告：

```powershell
if ([string]::IsNullOrWhiteSpace([string]$finalResult.report_path)) {
    throw "运行结束但没有返回报告路径"
}

$reportFileName = (
    ([string]$finalResult.report_path) -split "[/\\]"
)[-1]

$hostReportFile = Join-Path $reportRoot $reportFileName

if (-not (Test-Path -LiteralPath $hostReportFile -PathType Leaf)) {
    throw "最终报告不存在：$hostReportFile"
}

Write-Host "最终状态：$($finalResult.status)"
Write-Host "最终报告：$hostReportFile"

Get-Content -Raw -Encoding UTF8 -LiteralPath $hostReportFile
```

## 11. 验证 `.artifacts` 最终结构

检查四个顶层目录均存在：

```powershell
$expectedArtifactEntries = @(
    "content",
    "checkpoints",
    "runtime",
    "reports"
)

$missingArtifactEntries = @(
    $expectedArtifactEntries | Where-Object {
        -not (
            Test-Path `
                -LiteralPath (Join-Path $artifactRoot $_) `
                -PathType Container
        )
    }
)

if ($missingArtifactEntries.Count -gt 0) {
    throw "缺少 .artifacts 目录：$($missingArtifactEntries -join ', ')"
}

$unexpectedArtifactEntries = @(
    Get-ChildItem -LiteralPath $artifactRoot -Force |
    Where-Object {
        $_.Name -notin $expectedArtifactEntries
    }
)

if ($unexpectedArtifactEntries.Count -gt 0) {
    $unexpectedArtifactEntries | Select-Object FullName
    throw ".artifacts 顶层出现了预期之外的内容"
}
```

确认 `reports` 只包含 Markdown 报告：

```powershell
$invalidReportEntries = @(
    Get-ChildItem -LiteralPath $reportRoot -Force |
    Where-Object {
        $_.PSIsContainer -or
        $_.Extension.ToLowerInvariant() -ne ".md"
    }
)

if ($invalidReportEntries.Count -gt 0) {
    $invalidReportEntries | Select-Object FullName
    throw ".artifacts/reports 中出现了报告以外的内容"
}
```

检查 checkpoint：

```powershell
$checkpointFile = Join-Path `
    $artifactRoot `
    "checkpoints\file-governance.sqlite3"

if (-not (Test-Path -LiteralPath $checkpointFile -PathType Leaf)) {
    throw "SQLite checkpoint 不存在：$checkpointFile"
}
```

输出完整结构：

```powershell
Write-Host ""
Write-Host ".artifacts 最终内容："

Get-ChildItem -LiteralPath $artifactRoot -Recurse -Force |
    Select-Object FullName, Length, LastWriteTime |
    Format-Table -AutoSize
```

最终结构应类似：

```text
.artifacts/
├── content/
│   ├── normalized/
│   │   └── *.json
│   └── intermediate/         # 仅在生成中间产物时出现
├── checkpoints/
│   ├── file-governance.sqlite3
│   ├── file-governance.sqlite3-shm   # 可能存在
│   └── file-governance.sqlite3-wal   # 可能存在
├── runtime/
│   ├── first-run.json
│   ├── first-run.stderr.txt
│   ├── resume-run.json               # 需要人工确认时存在
│   └── resume-run.stderr.txt         # 需要人工确认时存在
└── reports/
    └── <run_id>.md
```

`data/input` 始终只读挂载，所有 checkpoint、标准化内容、运行结果和最终报告都位于 `.artifacts`。