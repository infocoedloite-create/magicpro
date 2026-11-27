<#
Build Magic Pro Desktop for Windows: dist\MagicPro (the program folder) and dist\MagicPro-Setup-<version>.exe.

  .\desktop\build\build.ps1                 # normal build (~17 MB installer; it downloads the GPU runtime and models while installing)
  .\desktop\build\build.ps1 -Relock         # resolve requirements-desktop.txt again (new package versions)
  .\desktop\build\build.ps1 -Relock -TensorRT   # ...and add TensorRT 10 from pypi.nvidia.com
  .\desktop\build\build.ps1 -Protect pyc    # ship .pyc instead of obfuscating (no PyArmor licence)
  .\desktop\build\build.ps1 -Protect none   # ship .py sources (development only)
  .\desktop\build\build.ps1 -WithRuntime    # also put the GPU runtime in the build (offline install / testing; ~3 GB)

Protection (default 'pyarmor'): the Python of app\ and desktop\ is PyArmor-obfuscated so the shipped
files hold no readable source. That needs PyArmor registered with a paid licence
(`py -3.12 -m pyarmor.cli reg <regfile.zip>`); without one the build falls back to .pyc and warns.
See docs/desktop.md > Protecting the code.

Needs: Windows 10/11 x64, Python 3.12 (py -3.12), internet, Inno Setup 6.3+ for the installer
(winget install JRSoftware.InnoSetup), and `py -3.12 -m pip install pyarmor` for obfuscation.
Run tools\desktop_keys.py once before the first build.
#>
param(
  [switch]$Relock,
  [switch]$TensorRT,
  [switch]$WithRuntime,
  [ValidateSet('pyarmor', 'pyc', 'none')]
  [string]$Protect = 'pyarmor',
  [switch]$NoInstaller,
  [string]$PythonVersion = '3.12.10'
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Dist = Join-Path $Root 'dist'
$Out = Join-Path $Dist 'MagicPro'
$Cache = Join-Path $Dist 'cache'
$PyDir = Join-Path $Out 'python'
New-Item -ItemType Directory -Force $Cache | Out-Null

# $ErrorActionPreference='Continue' so a tool writing to stderr (pip, pyarmor) isn't treated as a
# failure; we judge success by the exit code only
function Invoke-Py { $ErrorActionPreference = 'Continue'; & py -3.12 @args; if ($LASTEXITCODE -ne 0) { throw "py -3.12 $args failed ($LASTEXITCODE)" } }
function Invoke-Checked([string]$exe) { $ErrorActionPreference = 'Continue'; & $exe @args; if ($LASTEXITCODE -ne 0) { throw "$exe failed ($LASTEXITCODE)" } }

$Version = (Select-String -Path (Join-Path $Root 'desktop\version.py') -Pattern 'VERSION = "([\d.]+)"').Matches[0].Groups[1].Value
$cfg = Get-Content (Join-Path $Root 'desktop\desktop.json') -Raw | ConvertFrom-Json
if (-not $cfg.license_pubkey -or -not $cfg.release_pubkey) {
  throw 'desktop\desktop.json has no public keys yet. Run once: py -3.12 tools\desktop_keys.py'
}
Write-Host "== Magic Pro Desktop $Version (licence server $($cfg.server))"

# 1. the lock: exact package versions + hashes for users' PCs
$Lock = Join-Path $Root 'desktop\runtime.lock'
if ($Relock -or -not (Test-Path $Lock)) {
  Write-Host '== resolving runtime.lock'
  if ($TensorRT) { Invoke-Py (Join-Path $Root 'desktop\build\lock.py') --tensorrt } else { Invoke-Py (Join-Path $Root 'desktop\build\lock.py') }
}

# 2. embedded Python (kept between builds; site-packages too unless the lock changed)
if (-not (Test-Path (Join-Path $PyDir 'python.exe'))) {
  $zip = Join-Path $Cache "python-$PythonVersion-embed-amd64.zip"
  if (-not (Test-Path $zip)) {
    Write-Host "== downloading Python $PythonVersion (embeddable) from python.org"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $zip -UseBasicParsing
  }
  New-Item -ItemType Directory -Force $PyDir | Out-Null
  Expand-Archive $zip -DestinationPath $PyDir -Force
}
# Microsoft C++ runtime next to python.exe (app-local, allowed by the VC++ redistributable licence), so
# ONNX Runtime, OpenCV and NumPy load on PCs without the Visual C++ Redistributable installed
foreach ($dll in 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
                 'concrt140.dll', 'vcomp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
  $src = Join-Path $env:WINDIR "System32\$dll"
  if (Test-Path $src) { Copy-Item $src $PyDir -Force } else { Write-Warning "$dll not found on this PC; install the Visual C++ Redistributable (x64) here and build again" }
}
# search path: the stdlib zip, base packages (web server, cryptography, pip), the program folder;
# "import site" adds python\Lib\site-packages (the GPU runtime) after them
Set-Content -Path (Join-Path $PyDir 'python312._pth') -Encoding ascii -Value @('python312.zip', '.', 'base', '..', 'import site')

# 3. base packages: in the installer, replaced on every update
$Base = Join-Path $PyDir 'base'
if (Test-Path $Base) { Remove-Item -Recurse -Force $Base }
Write-Host '== base packages'
$constraints = Join-Path $Cache 'constraints.txt'
(Get-Content $Lock) | Where-Object { $_ -match '^[a-z0-9]' } | ForEach-Object { ($_ -split ' ')[0] } | Set-Content -Encoding ascii $constraints
Invoke-Py -m pip install --disable-pip-version-check --no-warn-script-location --only-binary=:all: --quiet `
  --target $Base -r (Join-Path $Root 'desktop\requirements-base.txt') -c $constraints

# 4. the GPU runtime only with -WithRuntime (normally each PC downloads it on first run)
$Site = Join-Path $PyDir 'Lib\site-packages'
if ($WithRuntime) {
  $rid = 'rt-' + (Get-FileHash $Lock -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
  $mark = Join-Path $Site '.magicpro-runtime'
  if (-not ((Test-Path $mark) -and ((Get-Content $mark) -eq $rid))) {
    Write-Host '== GPU runtime (large download)'
    if (Test-Path $Site) { Remove-Item -Recurse -Force $Site }
    Invoke-Py -m pip install --disable-pip-version-check --no-warn-script-location --only-binary=:all: --no-deps `
      --target $Site -r $Lock
    Set-Content -Path $mark -Value $rid -NoNewline -Encoding ascii
  }
} elseif (Test-Path $Site) {
  Remove-Item -Recurse -Force $Site
}

# 5. program files
Write-Host '== program files'
Invoke-Py (Join-Path $Root 'desktop\build\stage.py') $Out --mode $Protect --python (Join-Path $PyDir 'python.exe')

# 6. icon + MagicPro.exe (C# compiler that ships with Windows)
$Ico = Join-Path $Cache 'magicpro.ico'
if (-not (Test-Path $Ico)) { Invoke-Py (Join-Path $Root 'desktop\build\make_icon.py') $Ico }
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
Invoke-Checked $csc /nologo /target:winexe /optimize+ "/out:$(Join-Path $Out 'MagicPro.exe')" "/win32icon:$Ico" `
  /reference:System.Windows.Forms.dll /reference:System.Core.dll (Join-Path $Root 'desktop\build\launcher.cs')

# 7. smoke test: the activation screen's modules load with the embedded Python
Write-Host '== smoke test'
Push-Location $Out
try { Invoke-Checked (Join-Path $PyDir 'python.exe') -c 'import desktop.gate, desktop.setup, uvicorn, cryptography' }
finally { Pop-Location }

# 8. installer
if ($NoInstaller) { Write-Host "Done: $Out"; return }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  Write-Warning "Inno Setup 6 isn't installed, so no installer was made. Install it (winget install JRSoftware.InnoSetup) and run this again. The program folder is ready: $Out"
  return
}
Invoke-Checked $iscc /Q "/DAppVersion=$Version" "/DSourceDir=$Out" "/DOutputDir=$Dist" "/DIconFile=$Ico" (Join-Path $Root 'desktop\build\MagicPro.iss')
$setup = Join-Path $Dist "MagicPro-Setup-$Version.exe"
Write-Host ("Done: {0} ({1:N0} MB)" -f $setup, ((Get-Item $setup).Length / 1MB))
Write-Host "Next: upload it, then: py -3.12 tools\desktop_release.py `"$setup`" --url https://<where you uploaded it> --notes `"...`""
