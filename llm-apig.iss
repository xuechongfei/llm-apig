; llm-apig 安装包脚本 —— 由 desktop/build.py 调用（ISCC /DMyAppVersion=x.y.z）
; 手动编译：先用 /D 传版本号，见 desktop/build.py

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#define MyAppName "llm-apig"
#define MyAppExeName "llm-apig.exe"

[Setup]
AppId={{8C1F2B7A-6E5D-4C9A-9B3E-4D2F1A0C5E77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=llm-apig
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=llm-apig-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName} API 网关

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
Source: "dist\llm-apig\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; 清理自启注册项（若用户开启过）
Type: none

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Run', 'llm-apig');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('是否保留用户数据（渠道配置、日志）？' + #13#10 +
              '选择「是」保留，以后重装可继续使用；选择「否」彻底删除。',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      DelTree(ExpandConstant('{userappdata}') + '\llm-apig', True, True, True);
    end;
  end;
end;
