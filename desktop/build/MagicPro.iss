; Magic Pro Desktop installer (Inno Setup 6). Built by desktop/build/build.ps1:
;   ISCC.exe /DAppVersion=1.0.0 /DSourceDir=dist\MagicPro /DOutputDir=dist /DIconFile=dist\cache\magicpro.ico MagicPro.iss
;
; Installs per user (no admin rights), so the app can install its own updates silently:
; the app downloads a newer signed MagicPro-Setup-x.y.z.exe and runs it with /SILENT; this waits
; until the app has closed, replaces the program files and starts Magic Pro again.
; Kept across updates: models\ (face models), python\Lib\site-packages (GPU runtime) and
; everything in %APPDATA%\MagicPro (licence, settings, face library).
;
; Everything the program needs is installed here, on this PC: after copying the files, setup runs
; `python -m desktop.install`, which installs the GPU runtime (ONNX Runtime GPU + CUDA/cuDNN, OpenCV,
; NumPy... exactly desktop/runtime.lock) and downloads the face models (~4 GB, once), with progress
; in this window. The Microsoft C++ runtime DLLs ship inside python\. OBS Studio (virtual camera)
; can be installed too. The face swap then runs only on this PC's NVIDIA GPU and processor.
; Needs Inno Setup 6.3 or newer (ExecAndLogOutput).

#if VER < EncodeVer(6, 3, 0)
  #error Inno Setup 6.3 or newer is needed (winget upgrade JRSoftware.InnoSetup)
#endif

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\MagicPro"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\..\dist\cache\magicpro.ico"
#endif

[Setup]
AppId={{8C1E3B5A-2F4D-4E8B-9A61-5D7C2B9E4F10}
AppName=Magic Pro
AppVersion={#AppVersion}
AppVerName=Magic Pro {#AppVersion}
AppPublisher=Magic Pro
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\MagicPro
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=MagicPro-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\MagicPro.exe
UninstallDisplayName=Magic Pro
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "obs"; Description: "Install OBS Studio (free, from the OBS Project) for the virtual camera in Zoom, Meet, Teams and OBS. Windows asks for permission."; GroupDescription: "Also install:"; Check: not ObsInstalled

[InstallDelete]
; program files are replaced as a whole on every install, so nothing stale is left behind
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\static"
Type: filesandordirs; Name: "{app}\desktop"
Type: filesandordirs; Name: "{app}\python\base"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages.new"

[Files]
; face models are downloaded while installing (desktop/install.py), never packed into the setup
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "\models\*,__pycache__,\python\Lib\site-packages.new,\python\Lib\site-packages.old"

[Dirs]
Name: "{app}\models"

[Icons]
Name: "{autoprograms}\Magic Pro"; Filename: "{app}\MagicPro.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Magic Pro"; Filename: "{app}\MagicPro.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; also runs after a silent update (no skipifsilent), which restarts the app
Filename: "{app}\MagicPro.exe"; Description: "Start Magic Pro"; Flags: nowait postinstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\static"
Type: filesandordirs; Name: "{app}\desktop"

[Code]
const AppMutex = 'MagicProDesktop';

var
  SetupNotes: String;
  SetupError: String;

function ObsInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\obs-studio\bin\64bit\obs64.exe'))
    or RegKeyExists(HKLM64, 'SOFTWARE\OBS Studio') or RegKeyExists(HKCU, 'SOFTWARE\OBS Studio');
end;

// One line of output from `python -m desktop.install`: PROGRESS <pct> <text> | NOTE <text> | ERROR <text>
procedure OnSetupLine(const S: String; const Error, FirstLine: Boolean);
var
  Rest: String;
  Sp, Pct: Integer;
begin
  if Copy(S, 1, 9) = 'PROGRESS ' then begin
    Rest := Copy(S, 10, MaxInt);
    Sp := Pos(' ', Rest);
    if Sp > 0 then begin
      Pct := StrToIntDef(Copy(Rest, 1, Sp - 1), 0);
      WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Min +
        (WizardForm.ProgressGauge.Max - WizardForm.ProgressGauge.Min) * Pct div 100;
      WizardForm.FilenameLabel.Caption := Copy(Rest, Sp + 1, MaxInt);
    end;
  end else if Copy(S, 1, 5) = 'NOTE ' then
    SetupNotes := SetupNotes + Copy(S, 6, MaxInt) + #13#10
  else if Copy(S, 1, 6) = 'ERROR ' then
    SetupError := Copy(S, 7, MaxInt);
end;

// Installs the GPU runtime and the face models on this PC (see desktop/install.py).
procedure InstallRequirements();
var
  ResultCode: Integer;
  Again: Boolean;
begin
  repeat
    Again := False;
    SetupError := '';
    WizardForm.StatusLabel.Caption := 'Installing the GPU runtime and downloading the face models (about 4 GB, once). This can take 5-20 minutes...';
    WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Min;
    if not ExecAndLogOutput(ExpandConstant('{app}\python\python.exe'), '-m desktop.install', ExpandConstant('{app}'),
                            SW_HIDE, ewWaitUntilTerminated, ResultCode, @OnSetupLine) then
      ResultCode := 2;
    if (ResultCode <> 0) and not WizardSilent() then begin
      if SetupError = '' then SetupError := 'The download stopped.';
      Again := MsgBox('Magic Pro could not finish downloading its GPU runtime and face models:' + #13#10 + SetupError + #13#10#13#10 +
                      'Check your internet connection and press Retry. Press Cancel to finish now: Magic Pro then completes this the first time it opens.',
                      mbError, MB_RETRYCANCEL) = IDRETRY;
    end;
  until not Again;
  WizardForm.FilenameLabel.Caption := '';
end;

procedure InstallObs();
var
  ResultCode: Integer;
  Winget: String;
begin
  WizardForm.StatusLabel.Caption := 'Installing OBS Studio (for the virtual camera)...';
  Winget := ExpandConstant('{localappdata}\Microsoft\WindowsApps\winget.exe');
  if not FileExists(Winget) then
    Winget := 'winget.exe';
  if not Exec(Winget,'install --id OBSProject.OBSStudio --exact --silent --accept-package-agreements --accept-source-agreements',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then begin
    if not WizardSilent() then
      if MsgBox('OBS Studio could not be installed automatically. Open its download page?', mbConfirmation, MB_YESNO) = IDYES then
        ShellExec('open', 'https://obsproject.com/download', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    InstallRequirements();
    if WizardIsTaskSelected('obs') then
      InstallObs();
    if (SetupNotes <> '') and not WizardSilent() then
      MsgBox('About this PC''s graphics card:' + #13#10#13#10 + SetupNotes, mbInformation, MB_OK);
  end;
end;

function WaitForApp(Seconds: Integer): Boolean;
var i: Integer;
begin
  i := 0;
  while CheckForMutexes(AppMutex) and (i < Seconds * 2) do begin
    Sleep(500);
    i := i + 1;
  end;
  Result := not CheckForMutexes(AppMutex);
end;

function InitializeSetup(): Boolean;
begin
  // an update started from inside the app: the app is closing, give it time
  Result := WaitForApp(30);
  while (not Result) and (not WizardSilent()) do begin
    if MsgBox('Magic Pro is running. Close it, then press OK to continue.', mbInformation, MB_OKCANCEL) = IDCANCEL then
      Exit;
    Result := WaitForApp(5);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := WaitForApp(5);
  if not Result then
    MsgBox('Close Magic Pro first, then uninstall again.', mbInformation, MB_OK);
end;
