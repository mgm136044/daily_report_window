; Inno Setup script for the packaged build.
;
;     iscc installer.iss /DSourceDir=dist\daily-report /DAppVersion=0.1.0
;
; Per-user, no elevation. That is not a convenience — the scheduled task this
; installs has to run under the user's own interactive token, because the
; Claude Code CLI's credentials are protected against that account and nothing
; else can decrypt them. An installer that asked for administrator would invite
; a machine-wide install that cannot work.

#ifndef SourceDir
  #define SourceDir "dist\daily-report"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "daily-report"
#define AppDisplayName "하루 마감 보고서"
#define AppExe "daily-report.exe"
#define GuiExe "daily-report-gui.exe"

[Setup]
AppId={{8B2F5A31-4C7D-4E9A-9F3B-6D1E8C0A7B54}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppPublisher=daily-report
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppDisplayName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputBaseFilename=daily-report-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; lowest: install into the user's own profile, never Program Files
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppDisplayName}
UninstallDisplayIcon={app}\{#GuiExe}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppDisplayName}"; Filename: "{app}\{#GuiExe}"; \
      Comment: "하루 마감 보고서 상태 확인과 진단"
Name: "{group}\{#AppDisplayName} 설정"; Filename: "{app}\{#GuiExe}"; \
      Parameters: "setup"; Comment: "설치 마법사 다시 열기"

[Run]
; Opens the setup wizard, because nothing is configured yet — gui.py decides
; which window to show by looking for config.toml and .env. Someone who just
; ran an installer should land on the next step, not on an empty dashboard.
Filename: "{app}\{#GuiExe}"; Description: "설정 마법사 열기"; \
          Flags: nowait postinstall skipifsilent

[UninstallRun]
; Before the files go, take the scheduled task with them.
;
; Without this, uninstalling leaves a task pointing at an executable that no
; longer exists. It does not stop — it fires at 04:05 every night, fails to
; start, and records the failure, forever, in a place nobody thinks to look.
;
; One entry, unconditional. The question about data is asked from [Code]
; below, at uninstall time — see CurUninstallStepChanged for why it cannot be
; a Check: on a second entry here.
Filename: "{app}\{#AppExe}"; Parameters: "uninstall"; \
          Flags: runhidden waituntilterminated; RunOnceId: "RemoveScheduledTask"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

{ Ask about the data at uninstall time, and only then.

  This started as two run entries chosen by `Check: ShouldPurge`. That does
  not work, and the way it fails is quiet: **Inno evaluates Check on an
  uninstall-run entry while it is writing the uninstall log, which happens
  during installation.** So the question appeared when the program was being
  *installed*, asking about a removal that had not happened, and whichever
  answer it got was baked into the log. Uninstalling then asked nothing and
  ran whatever had been recorded — reported as "데이터까지 지운다고 체크했는데
  설정이 그대로 남아 있다", which is exactly what it would do.

  CurUninstallStepChanged is the documented place for uninstall-time logic.
  usUninstall runs before the files are deleted, so the executable that does
  the work is still there.

  Silent uninstalls keep the data. There is nobody to ask, and of the two
  answers only one is reversible. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  if UninstallSilent then
    Exit;
  if MsgBox(
      '설정과 기록도 함께 삭제할까요?' + #13#10 + #13#10 +
      '삭제하면 다음이 사라집니다:' + #13#10 +
      '    · Notion 연결 토큰 (.env)' + #13#10 +
      '    · config.toml 설정' + #13#10 +
      '    · 실행 이력, 수집 산출물, 지난 보고서' + #13#10 +
      '      (수집 산출물에는 프롬프트 원문이 들어 있습니다)' + #13#10 + #13#10 +
      '[아니요] 를 고르면 그대로 남습니다. 다시 설치하면 기존 Notion' + #13#10 +
      '데이터베이스를 이어서 쓰고, 설정을 다시 하지 않아도 됩니다.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    Exec(ExpandConstant('{app}\{#AppExe}'), 'uninstall --purge', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if ResultCode <> 0 then
      MsgBox('설정과 기록을 지우지 못했습니다 (종료 코드 ' + IntToStr(ResultCode) + ').' + #13#10 +
             '직접 지우려면 이 폴더를 삭제하세요:' + #13#10 +
             ExpandConstant('{localappdata}\daily-report'),
             mbError, MB_OK);
  end;
end;
