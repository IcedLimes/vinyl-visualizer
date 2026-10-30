; Inno Setup script for the Windows installer. Build the app first
; (pyinstaller packaging/vinyl.spec), then: iscc /DAppVersion=1.0.0 packaging\vinyl.iss
; It installs for the current user only, so it doesn't ask for administrator rights.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E4B0F3C-2D1A-4C8E-9B7F-5A3D2E1C0B9A}
AppName=Vinyl
AppVersion={#AppVersion}
AppVerName=Vinyl {#AppVersion}
AppPublisher=IcedLimes
AppPublisherURL=https://github.com/IcedLimes/vinyl-visualizer
DefaultDirName={localappdata}\Programs\Vinyl
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Vinyl-{#AppVersion}-windows-x64-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Vinyl.exe
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start Vinyl when I sign in (it shows itself while Spotify is open)"

[Files]
Source: "..\dist\Vinyl\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Vinyl"; Filename: "{app}\Vinyl.exe"

[Registry]
; The same entry as "Start at login" in Vinyl's menu
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "Vinyl"; ValueData: """{app}\Vinyl.exe"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\Vinyl.exe"; Description: "Start Vinyl now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\Vinyl.exe"; Parameters: "--quit"; Flags: runhidden; RunOnceId: "QuitVinyl"
