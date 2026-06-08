# HTTP CC Bridge Helper
# Usage:
#   . .\http_codex.ps1          # dot-source this file
#   codex-status                 # check bridge status
#   codex-send "refactor utils"  # send a message to Codex, returns reply
#   codex-send "fix bug" -Timeout 600
#   codex-send-async "big task"  # fire-and-forget, poll with backoff
#   codex-save                   # save current project+thread context
#   codex-restore                # restore saved project+thread
#   codex-do "one-off task" -Project "D:/work/repo"  # save → switch project → new thread → run → restore
#   codex-interrupt              # interrupt active turn
#   codex-history 5              # show last N turns
#   codex-projects               # list projects
#   codex-threads                # list threads for current project

$BRIDGE = 'http://127.0.0.1:8765/api'
$CTX_FILE = "$env:TEMP\.codex-context.json"

function _codex-call {
    param([string]$Method, [string]$Path, [string]$Body, [int]$Timeout = 60)

    $inFile = New-TemporaryFile
    $outFile = New-TemporaryFile
    try {
        if ($Body) {
            $Body | Out-File -FilePath $inFile -Encoding utf8 -NoNewline
            & curl.exe -s -X $Method "$BRIDGE$Path" `
                -H 'Content-Type: application/json' `
                -d "@$inFile" --max-time $Timeout -o $outFile
        } else {
            & curl.exe -s "$BRIDGE$Path" --max-time $Timeout -o $outFile
        }
        $json = Get-Content $outFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $json.ok) {
            Write-Warning "Bridge error: $($json.error)"
            return $null
        }
        return $json
    } finally {
        Remove-Item $inFile, $outFile -ErrorAction SilentlyContinue
    }
}

function codex-status {
    $r = _codex-call GET '/status'
    if ($r) { $r.status | Format-List }
}

function codex-projects {
    $r = _codex-call GET '/projects'
    if ($r) { $r.projects | Format-Table Index, cwd, threadCount -AutoSize }
}

function codex-threads {
    $r = _codex-call GET '/threads'
    if ($r) { $r.threads | Format-Table Index, threadId, title -AutoSize -Wrap }
}

function codex-models {
    $r = _codex-call GET '/models'
    if ($r) { $r.models | ForEach-Object { "$($_.model) | $($_.displayName) | default: $($_.defaultReasoningEffort)" } }
}

function codex-history {
    param([int]$Limit = 5)
    $r = _codex-call GET "/history?limit=$Limit"
    if ($r) { Write-Output $r.text }
}

function codex-summary {
    $r = _codex-call GET '/summary'
    if ($r) { Write-Output $r.text }
}

function codex-send {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Message,
        [int]$Timeout = 300,
        [switch]$NoSteer
    )
    $steer = if ($NoSteer) { 'false' } else { 'true' }
    $body = @{text=$Message; timeoutSeconds=$Timeout; steer=$steer} | ConvertTo-Json -Compress
    Write-Host "Sending to Codex..." -ForegroundColor Cyan
    $r = _codex-call POST '/message' -Body $body -Timeout ($Timeout + 10)
    if ($r) {
        Write-Host "`nStatus: $($r.status) | Mode: $($r.mode) | TurnId: $($r.turnId)" -ForegroundColor DarkGray
        Write-Output $r.text
    }
}

function codex-send-async {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Message,
        [switch]$NoSteer
    )
    $steer = if ($NoSteer) { 'false' } else { 'true' }
    $body = @{text=$Message; steer=$steer; async=$true} | ConvertTo-Json -Compress
    Write-Host "Dispatching async..." -ForegroundColor Cyan
    $r = _codex-call POST '/message' -Body $body -Timeout 90
    if ($r) {
        Write-Host "TurnId: $($r.turnId) | Mode: $($r.mode)" -ForegroundColor DarkGray
    }
}

function codex-interrupt {
    $r = _codex-call POST '/interrupt' -Body '{}'
    if ($r) { Write-Output "Interrupted: $($r.interrupted) | Collected text: $($r.collectedText.Length) chars" }
}

function codex-new-thread {
    $r = _codex-call POST '/new' -Body '{}'
    if ($r) { Write-Output "New thread: $($r.thread.threadId)" }
}

# ── Context save/restore ──

function codex-save {
    $r = _codex-call GET '/status'
    if (-not $r) { return }
    $ctx = @{
        project = $r.status.project
        threadId = $r.status.threadId
    }
    $ctx | ConvertTo-Json | Out-File $CTX_FILE -Encoding utf8
    Write-Host "Saved context: $($ctx.project) | $($ctx.threadId)" -ForegroundColor DarkGray
}

function codex-restore {
    if (-not (Test-Path $CTX_FILE)) {
        Write-Warning "No saved context found."
        return
    }
    $ctx = Get-Content $CTX_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "Restoring: $($ctx.project) | $($ctx.threadId)" -ForegroundColor DarkGray

    # Select project
    $body = @{cwd=$ctx.project} | ConvertTo-Json -Compress
    $r = _codex-call POST '/project' -Body $body
    if (-not $r) { return }

    # Select thread
    $body = @{threadId=$ctx.threadId; cwd=$ctx.project} | ConvertTo-Json -Compress
    $r = _codex-call POST '/thread' -Body $body
    if ($r) { Write-Host "Context restored." -ForegroundColor Green }
}

# ── One-off task (save → switch project → new thread → run → restore) ──

function codex-do {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Message,
        [string]$Project = (Get-Location).Path,
        [int]$Timeout = 300
    )
    # 1. Save current context
    codex-save

    # 2. Switch to target project
    $body = @{cwd=$Project} | ConvertTo-Json -Compress
    $r = _codex-call POST '/project' -Body $body
    if (-not $r) { return }

    # 3. New thread
    $r = _codex-call POST '/new' -Body '{}'
    if (-not $r) { return }
    $newThreadId = $r.threadId
    Write-Host "Task thread: $newThreadId" -ForegroundColor DarkGray

    # 4. Run task
    $steer = 'false'
    $body = @{text=$Message; timeoutSeconds=$Timeout; steer=$steer} | ConvertTo-Json -Compress
    Write-Host "Sending to Codex..." -ForegroundColor Cyan
    $r = _codex-call POST '/message' -Body $body -Timeout ($Timeout + 10)
    if ($r) {
        Write-Host "`nStatus: $($r.status) | TurnId: $($r.turnId)" -ForegroundColor DarkGray
        Write-Output $r.text
    }

    # 5. Restore original context
    codex-restore
}

Write-Host "Codex HTTP Bridge helpers loaded. codex-status codex-send codex-send-async codex-do codex-history codex-projects codex-threads codex-save codex-restore codex-interrupt codex-new-thread" -ForegroundColor Green
