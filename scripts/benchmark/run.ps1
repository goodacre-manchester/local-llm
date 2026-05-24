# Run the benchmark prompts in scripts/benchmark/prompts.json against the RAG
# server's OpenAI-compatible endpoint. Captures full response (incl. citations[])
# + wall-clock latency per prompt, one JSON file each, under results/<RunId>/.
#
# Usage:
#   .\scripts\benchmark\run.ps1 -RunId baseline-20260522-1830
#   .\scripts\benchmark\run.ps1 -RunId p2-nemo-gen-20260522 -ProfileOverride "nemotron-3-nano:30b-a3b-q4_K_M"
#   .\scripts\benchmark\run.ps1 -RunId p3a-nemo-parse-20260522 -CollectionOverride "ieee-nemo-parse-tas"
#
# The HTTP call goes via `wsl bash curl` rather than Invoke-WebRequest because
# the rag-server runs in WSL on `network_mode: host` and isn't always reachable
# from Windows PowerShell directly (depends on WSL networking mode + firewall).
# WSL bash + curl works regardless.
#
# Overrides apply uniformly across all prompts whose collection matches the
# original collection (so an `amd`-collection prompt isn't rerouted to an
# `ieee-nemo-parse-tas` variant). To override only a subset, edit prompts.json.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$PromptsPath        = "$PSScriptRoot\prompts.json",
    [string]$ResultsRoot        = "$PSScriptRoot\results",
    [string]$BaseUrl            = "http://127.0.0.1:3000",
    [string]$CollectionOverride = "",
    [string]$ProfileOverride    = "",
    # Override only prompts whose default collection matches this (e.g. "ieee").
    # Useful when comparing ieee->ieee-nemo-parse-tas without also rerouting amd.
    [string]$OverrideOnlyCollection = "",
    [int]$TimeoutSec            = 700,
    [string]$ApiKey             = ""
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $PromptsPath)) {
    Write-Error "prompts.json not found at $PromptsPath"
    exit 1
}

$resultsDir = Join-Path $ResultsRoot $RunId
if (Test-Path $resultsDir) {
    Write-Error "Results dir already exists: $resultsDir -- pick a different -RunId"
    exit 1
}
New-Item -ItemType Directory -Path $resultsDir -Force | Out-Null

$promptDoc = Get-Content -Raw $PromptsPath | ConvertFrom-Json
$prompts   = $promptDoc.prompts

Write-Host "Run ID        : $RunId"
Write-Host "Prompts file  : $PromptsPath"
Write-Host "Prompts count : $($prompts.Count)"
Write-Host "Base URL      : $BaseUrl  (called via wsl bash + curl)"
if ($CollectionOverride) {
    if ($OverrideOnlyCollection) {
        Write-Host "Collection    : override $OverrideOnlyCollection -> $CollectionOverride (other collections untouched)"
    } else {
        Write-Host "Collection    : override ALL -> $CollectionOverride"
    }
}
if ($ProfileOverride)    { Write-Host "Profile       : override -> $ProfileOverride" }
Write-Host "Results dir   : $resultsDir"
Write-Host ""

# Convert Windows path to WSL path. Does NOT require the file to exist -- needed
# because we convert paths for response/stderr files that curl will create.
function ConvertTo-WslPath {
    param([string]$Path)
    $abs = [System.IO.Path]::GetFullPath($Path)
    $drive = ($abs -replace '^([A-Za-z]):.*', '$1').ToLower()
    return ($abs -replace '^[A-Za-z]:', "/mnt/$drive").Replace('\', '/')
}

$summary = [ordered]@{
    runId       = $RunId
    startedAt   = (Get-Date).ToString('o')
    baseUrl     = $BaseUrl
    promptsFile = $PromptsPath
    overrides   = [ordered]@{
        collection             = $CollectionOverride
        profile                = $ProfileOverride
        overrideOnlyCollection = $OverrideOnlyCollection
    }
    prompts     = @()
}

foreach ($p in $prompts) {
    $collection = $p.collection
    $promptProfile = $p.profile

    $doCollectionOverride = $false
    if ($CollectionOverride) {
        if (-not $OverrideOnlyCollection -or $OverrideOnlyCollection -eq $collection) {
            $collection = $CollectionOverride
            $doCollectionOverride = $true
        }
    }
    if ($ProfileOverride) { $promptProfile = $ProfileOverride }

    $modelField = if ($promptProfile) { "$collection!$promptProfile" } else { $collection }

    $body = @{
        model    = $modelField
        messages = @(@{ role = 'user'; content = $p.question })
        stream   = $false
    } | ConvertTo-Json -Depth 5 -Compress

    # Write request body to a temp file (Windows-side); pass its WSL path to curl.
    $bodyFile     = Join-Path $resultsDir "$($p.id).request.json"
    $responseFile = Join-Path $resultsDir "$($p.id).response.json"
    $stderrFile   = Join-Path $resultsDir "$($p.id).stderr.txt"
    [System.IO.File]::WriteAllText($bodyFile, $body, [System.Text.UTF8Encoding]::new($false))

    $wslBody = ConvertTo-WslPath $bodyFile
    $wslResp = ConvertTo-WslPath $responseFile
    $wslErr  = ConvertTo-WslPath $stderrFile

    $authHeader = if ($ApiKey) { "-H 'Authorization: Bearer $ApiKey'" } else { "" }
    # Single-quoted PowerShell string so PS does NOT interpolate $vars; bash sees
    # the command verbatim. The body file path is the only thing that varies and
    # we substitute it explicitly before invoking. Curl exits non-zero on HTTP
    # errors (because of --fail-with-body / -fsS), and that exit propagates
    # through `wsl.exe` to PowerShell's $LASTEXITCODE.
    $curlCmd = "curl -fsS -X POST '$BaseUrl/v1/chat/completions' -H 'Content-Type: application/json' $authHeader --max-time $TimeoutSec --data @'$wslBody' -o '$wslResp' 2> '$wslErr'"

    Write-Host ("[{0}] model={1}  q={2}" -f $p.id, $modelField, ($p.question.Substring(0, [Math]::Min(60, $p.question.Length))))

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & "C:\Windows\System32\wsl.exe" -e bash -lc $curlCmd | Out-Null
    $curlExit = $LASTEXITCODE
    $sw.Stop()
    $latencyS = [Math]::Round($sw.Elapsed.TotalSeconds, 2)

    $err = $null
    $response = $null

    if ($curlExit -ne 0) {
        $stderr = if (Test-Path $stderrFile) { Get-Content -Raw $stderrFile } else { '' }
        $err = "curl exit=$curlExit; stderr=$stderr"
    } else {
        try {
            $response = Get-Content -Raw $responseFile | ConvertFrom-Json
        } catch {
            $err = "response not valid JSON: $($_.Exception.Message)"
        }
    }

    $result = [ordered]@{
        prompt   = $p
        runtime  = [ordered]@{
            modelField           = $modelField
            collection           = $collection
            profile              = $promptProfile
            collectionOverridden = $doCollectionOverride
            latency_s            = $latencyS
            curlExit             = $curlExit
            error                = $err
        }
        response = $response
    }

    $outPath = Join-Path $resultsDir "$($p.id).json"
    # PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM, which trips
    # python json.load() and other strict UTF-8 readers. Write raw bytes
    # without BOM instead.
    [System.IO.File]::WriteAllText(
        $outPath,
        ($result | ConvertTo-Json -Depth 20),
        [System.Text.UTF8Encoding]::new($false)
    )

    # Clean up intermediate files now that we've captured what we need.
    Remove-Item -Force -ErrorAction SilentlyContinue $bodyFile, $responseFile, $stderrFile

    $statusBit = if ($err) { 'ERR' } else { 'ok' }
    Write-Host ("    -> {0,4}  {1,7}s  -> {2}" -f $statusBit, $latencyS, $outPath)

    $summary.prompts += [ordered]@{
        id         = $p.id
        modelField = $modelField
        latency_s  = $latencyS
        error      = $err
        file       = (Split-Path -Leaf $outPath)
    }
}

$summary.finishedAt = (Get-Date).ToString('o')
[System.IO.File]::WriteAllText(
    (Join-Path $resultsDir 'summary.json'),
    ($summary | ConvertTo-Json -Depth 10),
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host ""
Write-Host "Done. Score with:  .\scripts\benchmark\score.ps1 -RunId $RunId"
