$body = '{"model":"amd","messages":[{"role":"user","content":"What is Vitis HLS and what does it do?"}]}'
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$r = Invoke-WebRequest -Uri "http://127.0.0.1:3000/v1/chat/completions" -Method POST `
    -ContentType "application/json" -Body $bytes -TimeoutSec 120 -UseBasicParsing
($r.Content | ConvertFrom-Json).choices[0].message.content
