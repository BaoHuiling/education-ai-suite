; SPDX-FileCopyrightText: (C) 2026 Intel Corporation
; SPDX-License-Identifier: Apache-2.0

!include nsDialogs.nsh

!define SC_USER_DATA_DIR "$LOCALAPPDATA\SmartClassroom"

!macro customPageAfterChangeDir
  Function scShowFixedInstallDir
    FindWindow $0 "#32770" "" $HWNDPARENT
    GetDlgItem $1 $0 1019
    SendMessage $1 0x00CF 1 0
    GetDlgItem $1 $0 1001
    EnableWindow $1 0
  FunctionEnd

  !insertmacro skipPageIfUpdated
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW scShowFixedInstallDir
  !define MUI_DIRECTORYPAGE_TEXT_TOP "Smart Classroom will be installed in the following folder. The folder follows the installation option chosen on the previous page: your user profile for Only for me, Program Files for Anyone who uses this computer."
  !insertmacro MUI_PAGE_DIRECTORY
!macroend

!macro scUseCurrentUserContext
  ${if} $installMode == "all"
    SetShellVarContext current
  ${endif}
!macroend

!macro scRestoreContext
  ${if} $installMode == "all"
    SetShellVarContext all
  ${endif}
!macroend

!macro customUnWelcomePage
  !insertmacro MUI_UNPAGE_WELCOME

  Var scDeleteUserData
  Var scDeleteUserDataBox

  Function un.scUninstallOptionsShow
    !insertmacro scUseCurrentUserContext
    StrCpy $2 "${SC_USER_DATA_DIR}"
    !insertmacro scRestoreContext
    ${IfNot} ${FileExists} "$2\*.*"
      Abort
    ${EndIf}

    !insertmacro MUI_HEADER_TEXT "Uninstall Options" "Choose whether to keep your Smart Classroom data."
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
      Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 24u "Your settings, Python environments, downloaded models, sessions and content search data are stored in:"
    Pop $0
    ${NSD_CreateText} 0 28u 100% 12u "$2"
    Pop $0
    SendMessage $0 0x00CF 1 0
    ${NSD_CreateCheckbox} 0 52u 100% 12u "&Delete this data as well"
    Pop $scDeleteUserDataBox
    ${If} $scDeleteUserData == ${BST_CHECKED}
      ${NSD_Check} $scDeleteUserDataBox
    ${EndIf}
    ${NSD_CreateLabel} 0 72u 100% 24u "Leave it unchecked to keep the data for a later installation."
    Pop $0

    nsDialogs::Show
  FunctionEnd

  Function un.scUninstallOptionsLeave
    ${NSD_GetState} $scDeleteUserDataBox $scDeleteUserData
  FunctionEnd

  UninstPage custom un.scUninstallOptionsShow un.scUninstallOptionsLeave
!macroend

!macro customUnInstall
  ${ifNot} ${isUpdated}
  ${andIf} $scDeleteUserData == ${BST_CHECKED}
    !insertmacro scUseCurrentUserContext
    RMDir /r "${SC_USER_DATA_DIR}"
    ${if} ${FileExists} "${SC_USER_DATA_DIR}\*.*"
      MessageBox MB_OK|MB_ICONEXCLAMATION \
        "Some files in ${SC_USER_DATA_DIR} are in use and were not deleted. Delete the folder manually after restarting Windows." \
        /SD IDOK
    ${endif}
    !insertmacro scRestoreContext
  ${endIf}
!macroend
