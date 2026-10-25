; electron-builder owns installation/uninstallation. Never invoke its default
; terminate/retry path: only the application owner can prove maintenance safe.
!macro customCheckAppRunning
  ${Do}
    ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0
    ; nsProcess documents 603 as no match; all other failures stay unknown.
    ${If} $R0 == 603
      ${nsProcess::FindProcess} "pulsara-interop.exe" $R0
      ${If} $R0 == 603
        !ifdef BUILD_UNINSTALLER
          ${IfNot} ${isUpdated}
            ; Explicit uninstall checks and holds the guest locks during cleanup.
            ${ExitDo}
          ${EndIf}
        !endif
        ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
          ClearErrors
          ExecWait '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --pulsara-maintenance-check' $R1
          ${IfNot} ${Errors}
          ${AndIf} $R1 == 0
            ${ExitDo}
          ${EndIf}
        ${Else}
          ${ExitDo}
        ${EndIf}
      ${EndIf}
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Pulsara is running, still settling work, or cannot confirm maintenance is safe. Quit Pulsara normally, then retry. Installation will not terminate Pulsara, WSL, or PostgreSQL." /SD IDCANCEL IDRETRY +2
    Quit
  ${Loop}
!macroend

!ifdef BUILD_UNINSTALLER
!include MUI2.nsh
!include nsDialogs.nsh
Var PulsaraRemoveData
Var PulsaraKeepRadio
Var PulsaraAllRadio

!macro customUnInit
  StrCpy $PulsaraRemoveData 0
  ClearErrors
  ${GetParameters} $R0
  ${GetOptions} $R0 "--delete-app-data" $R1
  ${IfNot} ${Errors}
    StrCpy $PulsaraRemoveData 1
  ${EndIf}
!macroend

!macro customUnWelcomePage
  UninstPage custom un.PulsaraOptions un.PulsaraOptionsLeave
!macroend

Function un.PulsaraOptions
  !insertmacro MUI_HEADER_TEXT "卸载 Pulsara" "选择要保留的内容"
  nsDialogs::Create 1018
  Pop $R0
  ${If} $R0 == error
    Abort
  ${EndIf}
  ${NSD_CreateRadioButton} 0 20u 100% 18u "保留个人数据"
  Pop $PulsaraKeepRadio
  ${NSD_CreateLabel} 12u 42u 95% 24u "卸载应用和 WSL 运行资源，保留配置、会话和文件。"
  Pop $R0
  ${NSD_CreateRadioButton} 0 80u 100% 18u "彻底卸载"
  Pop $PulsaraAllRadio
  ${NSD_CreateLabel} 12u 102u 95% 30u "同时删除配置、会话和快速开始文件。"
  Pop $R0
  ${If} $PulsaraRemoveData == 1
    ${NSD_Check} $PulsaraAllRadio
  ${Else}
    ${NSD_Check} $PulsaraKeepRadio
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function un.PulsaraOptionsLeave
  ${NSD_GetState} $PulsaraAllRadio $PulsaraRemoveData
FunctionEnd

!macro customUnInstall
  ; electron-builder also runs the uninstaller during upgrades. Keep that path intact.
  ${IfNot} ${isUpdated}
    StrCpy $R2 "keep"
    ${If} $PulsaraRemoveData == 1
      StrCpy $R2 "all"
    ${EndIf}
    ClearErrors
    ExecWait '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --pulsara-uninstall=$R2' $R1
    ${If} ${Errors}
    ${OrIf} $R1 != 0
      MessageBox MB_OK|MB_ICONEXCLAMATION "清理未完成。请确认 Ubuntu 可用并退出 Pulsara，再重试。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
    ${If} $PulsaraRemoveData == 1
      RMDir /r "$APPDATA\Pulsara"
      ${If} ${FileExists} "$APPDATA\Pulsara"
        MessageBox MB_OK|MB_ICONEXCLAMATION "个人数据未清理完成。请关闭占用它的程序后重试。" /SD IDOK
        SetErrorLevel 1
        Quit
      ${EndIf}
    ${EndIf}
    RMDir /r "$LOCALAPPDATA\pulsara-desktop-updater"
  ${EndIf}
!macroend
!endif
