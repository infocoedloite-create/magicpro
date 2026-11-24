param([switch]$TensorRT, [string]$BindHost = '127.0.0.1', [int]$Port = 7860)
Set-Location $PSScriptRoot
if ($TensorRT) { $env:MAGIC_TRT = '1' }
$env:MAGIC_HOST = $BindHost
$env:MAGIC_PORT = "$Port"
.\.venv\Scripts\python.exe run.py
