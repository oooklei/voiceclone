; VoiceClone 注册表发现契约（voiceclone-server-protocol §2）
; recorder 读取 HKCU\Software\VoiceClone\SidecarPath（REG_SZ = sidecar 目录）来
; 发现已安装的打包侧车。安装时写入、卸载时删除。
; install_mode 默认 currentUser（per-user），故写 HKCU。

!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\VoiceClone" "SidecarPath" "$INSTDIR\resources\sidecar"
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  DeleteRegValue HKCU "Software\VoiceClone" "SidecarPath"
!macroend