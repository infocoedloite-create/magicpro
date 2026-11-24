# Opens a private SSH tunnel to Magic Pro on the Vast.ai GPU and opens it in your browser.
# The page is then http://localhost:7860, which browsers treat as secure, so the webcam works.
# Get the IP and SSH port from Vast.ai > Instances > key icon > "Direct SSH Connect".
param(
  [string]$InstanceIp = '14.99.238.125',
  [int]$SshPort = 6947,
  [int]$LocalPort = 7860,
  [string]$Key = "$HOME\.ssh\vast_magicpro"
)
Write-Host "Tunnel: http://localhost:$LocalPort -> Magic Pro on $InstanceIp. Keep this window open; Ctrl+C to close."
Start-Job -ScriptBlock { param($p) Start-Sleep 3; Start-Process "http://localhost:$p" } -ArgumentList $LocalPort | Out-Null
ssh -i $Key -p $SshPort -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L "${LocalPort}:127.0.0.1:17860" "root@$InstanceIp"
