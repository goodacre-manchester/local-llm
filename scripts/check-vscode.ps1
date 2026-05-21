# Check VS Code version
$productJson = Get-ChildItem "$env:LOCALAPPDATA\Programs\Microsoft VS Code\resources\app\product.json" -ErrorAction SilentlyContinue
if ($productJson) {
    $p = Get-Content $productJson.FullName | ConvertFrom-Json
    Write-Host "VS Code version: $($p.version)"
}

# Check installed extensions matching copilot/ollama
$extDir = "$env:USERPROFILE\.vscode\extensions"
Get-ChildItem $extDir -Directory | Where-Object { $_.Name -match 'copilot|ollama|ai-toolkit' } | Select-Object -ExpandProperty Name
