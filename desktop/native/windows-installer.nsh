; electron-builder owns installation/uninstallation. Never invoke its default
; terminate/retry path: only the application owner can prove maintenance safe.
!macro customCheckAppRunning
  ${Do}
    ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0
    ; nsProcess documents 603 as no match; all other failures stay unknown.
    ${If} $R0 == 603
      ${nsProcess::FindProcess} "pulsara-interop.exe" $R0
      ${If} $R0 == 603
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
