# Score a benchmark run produced by run.ps1. Applies the automated rules from
# prompts.json against each prompt's captured response and prints a pass/fail
# table + summary counts. Optional -CompareTo prints a delta vs another run.
#
# Usage:
#   .\scripts\benchmark\score.ps1 -RunId baseline-20260522-1830
#   .\scripts\benchmark\score.ps1 -RunId p2-nemo-gen-20260522 -CompareTo baseline-20260522-1830

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$CompareTo   = "",
    [string]$PromptsPath = "$PSScriptRoot\prompts.json",
    [string]$ResultsRoot = "$PSScriptRoot\results"
)

$ErrorActionPreference = 'Stop'

$ABSTAIN = "I don't have enough information in the provided sources to answer that."

function Get-RunScores {
    param([string]$Id)

    $dir = Join-Path $ResultsRoot $Id
    if (-not (Test-Path $dir)) { throw "Results dir not found: $dir" }

    $prompts = (Get-Content -Raw $PromptsPath | ConvertFrom-Json).prompts
    $scores  = @()

    foreach ($p in $prompts) {
        $resultPath = Join-Path $dir "$($p.id).json"
        if (-not (Test-Path $resultPath)) {
            $scores += [pscustomobject]@{
                id        = $p.id
                pass      = $false
                latency_s = $null
                notes     = 'NO RESULT FILE'
                ruleMisses= @('no result')
            }
            continue
        }

        $r = Get-Content -Raw $resultPath | ConvertFrom-Json

        $err = $r.runtime.error
        if ($err) {
            $scores += [pscustomobject]@{
                id        = $p.id
                pass      = $false
                latency_s = $r.runtime.latency_s
                notes     = "ERROR: $err"
                ruleMisses= @('runtime error')
            }
            continue
        }

        $citations = @()
        if ($r.response -and $r.response.citations) {
            $citations = @($r.response.citations)
        }
        $answer = ''
        if ($r.response -and $r.response.choices -and $r.response.choices.Count -gt 0) {
            $answer = [string]$r.response.choices[0].message.content
        }

        $misses = @()

        # must_abstain rule -- citation count is irrelevant here. app/server.js
        # returns the retrieved chunks in citations[] regardless of whether the
        # model abstained on them (augmented-grounding lets the model abstain
        # even when matches were found). The only thing that matters is whether
        # the answer text begins with the canonical ABSTAIN sentence.
        if ($p.must_abstain) {
            if (-not ($answer.Trim().StartsWith($ABSTAIN) -or $answer.Trim() -eq $ABSTAIN)) {
                $misses += "did not abstain"
            }
        }

        # must_not_abstain rule
        if ($p.must_not_abstain -and ($answer.Trim().StartsWith($ABSTAIN))) {
            $misses += "abstained on a question that should have been answerable"
        }

        # expected_citations: each rule must be satisfied by at least one citation
        $expected = @()
        if ($p.expected_citations) { $expected = @($p.expected_citations) }
        foreach ($rule in $expected) {
            $fileNeedle    = [string]$rule.fileName_contains
            $sectionNeedle = [string]$rule.section_contains
            $hit = $false
            foreach ($c in $citations) {
                $fileOk    = $true
                $sectionOk = $true
                if ($fileNeedle)    { $fileOk    = ([string]$c.fileName).ToLower().Contains($fileNeedle.ToLower()) }
                if ($sectionNeedle) { $sectionOk = ([string]$c.section).ToLower().Contains($sectionNeedle.ToLower()) }
                if ($fileOk -and $sectionOk) { $hit = $true; break }
            }
            if (-not $hit) {
                $f = if ($fileNeedle) { "file~'$fileNeedle'" } else { 'file=*' }
                $s = if ($sectionNeedle) { "section~'$sectionNeedle'" } else { 'section=*' }
                $misses += "missing citation: $f & $s"
            }
        }

        # latency soft-warn
        $latencyNote = ''
        if ($p.max_latency_s -and $r.runtime.latency_s -gt $p.max_latency_s) {
            $latencyNote = " [slow: $($r.runtime.latency_s)s > $($p.max_latency_s)s]"
        }

        $pass = ($misses.Count -eq 0)
        $scores += [pscustomobject]@{
            id        = $p.id
            pass      = $pass
            latency_s = $r.runtime.latency_s
            notes     = if ($pass) { "ok$latencyNote" } else { "$($misses -join '; ')$latencyNote" }
            ruleMisses= $misses
            currentlyFailingBaseline = [bool]$p.currently_failing
        }
    }

    return $scores
}

$scores = Get-RunScores -Id $RunId

Write-Host ""
Write-Host "=== Run: $RunId ===" -ForegroundColor Cyan
$totalPass = @($scores | Where-Object pass).Count
$total     = $scores.Count
$medianLat = ($scores | Where-Object latency_s | Sort-Object latency_s | Select-Object -Skip ([math]::Floor($total/2)) -First 1).latency_s

$scores | Format-Table @(
    @{ Label = 'PROMPT';    Expression = { $_.id }; Width = 22 },
    @{ Label = 'PASS';      Expression = { if ($_.pass) { 'PASS' } else { 'FAIL' } }; Width = 6 },
    @{ Label = 'LATENCY_S'; Expression = { $_.latency_s }; Width = 10 },
    @{ Label = 'NOTES';     Expression = { $_.notes } }
) -AutoSize

Write-Host ("Totals: {0}/{1} pass.  Median latency: {2}s" -f $totalPass, $total, $medianLat)

if ($CompareTo) {
    Write-Host ""
    Write-Host "=== Delta vs $CompareTo ===" -ForegroundColor Cyan
    $other = Get-RunScores -Id $CompareTo
    $byId  = @{}
    foreach ($o in $other) { $byId[$o.id] = $o }

    $delta = @()
    foreach ($s in $scores) {
        $base = $byId[$s.id]
        $cmp  = if ($base) {
            if ($s.pass -and -not $base.pass) { 'FIXED'   }
            elseif (-not $s.pass -and $base.pass) { 'REGRESSED' }
            elseif ($s.pass -and $base.pass) { 'stable+'  }
            else { 'stable-' }
        } else { 'new' }

        $delta += [pscustomobject]@{
            id          = $s.id
            this        = if ($s.pass) { 'PASS' } else { 'FAIL' }
            compareTo   = if ($base) { if ($base.pass) { 'PASS' } else { 'FAIL' } } else { '-' }
            change      = $cmp
            latency_s   = $s.latency_s
            base_latency= if ($base) { $base.latency_s } else { $null }
        }
    }

    $delta | Format-Table @(
        @{ Label = 'PROMPT';     Expression = { $_.id }; Width = 22 },
        @{ Label = 'THIS';       Expression = { $_.this }; Width = 6 },
        @{ Label = 'BASELINE';   Expression = { $_.compareTo }; Width = 10 },
        @{ Label = 'CHANGE';     Expression = { $_.change }; Width = 10 },
        @{ Label = 'LAT_S';      Expression = { $_.latency_s }; Width = 8 },
        @{ Label = 'BASE_LAT_S'; Expression = { $_.base_latency }; Width = 12 }
    ) -AutoSize

    $fixed     = @($delta | Where-Object change -eq 'FIXED').Count
    $regressed = @($delta | Where-Object change -eq 'REGRESSED').Count
    Write-Host ("Delta: +{0} fixed, -{1} regressed" -f $fixed, $regressed)
}
