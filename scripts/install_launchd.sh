#!/usr/bin/env bash
# 安装/卸载 myStock2 例行更新的 launchd 任务（用户级 LaunchAgents，可随时卸载）。
#   bash scripts/install_launchd.sh install     # 生成 plist 并加载
#   bash scripts/install_launchd.sh uninstall   # 卸载并删除 plist
#   bash scripts/install_launchd.sh status
# 时间为本机时区（当前为 PDT）：港股 16:00 HKT 收盘＝本机 01:00（冬令 00:00），美股 13:00 PT 收盘。Mac 睡眠时，唤醒后补跑。
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="${PY:-/opt/anaconda3/envs/mk/bin/python}"        # 与 V1 共用的默认环境
DEST="$HOME/Library/LaunchAgents"
LOGDIR="$REPO/data/logs"
# 阶段:时:分（周一至周五）
JOBS=("hk:2:15" "pre:6:15" "us:14:0")

label() { echo "com.mystock2.update.$1"; }

case "${1:-}" in
  install)
    mkdir -p "$DEST" "$LOGDIR"
    for j in "${JOBS[@]}"; do
      IFS=: read -r phase hour minute <<<"$j"
      L="$(label "$phase")"; P="$DEST/$L.plist"
      {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        echo '<plist version="1.0"><dict>'
        echo "  <key>Label</key><string>$L</string>"
        echo "  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>mystock2</string><string>update</string><string>--phase</string><string>$phase</string><string>--notify</string></array>"
        echo "  <key>WorkingDirectory</key><string>$REPO</string>"
        echo "  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>"
        echo "  <key>StartCalendarInterval</key><array>"
        for wd in 1 2 3 4 5; do
          echo "    <dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>"
        done
        echo "  </array>"
        echo "  <key>StandardOutPath</key><string>$LOGDIR/launchd_$phase.out.log</string>"
        echo "  <key>StandardErrorPath</key><string>$LOGDIR/launchd_$phase.err.log</string>"
        echo "  <key>RunAtLoad</key><false/>"
        echo '</dict></plist>'
      } > "$P"
      launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$P"
      echo "已加载 $L：周一至周五 $(printf '%02d:%02d' "$hour" "$minute")（本机时区）"
    done
    ;;
  uninstall)
    for j in "${JOBS[@]}"; do
      IFS=: read -r phase _ _ <<<"$j"; L="$(label "$phase")"
      launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
      rm -f "$DEST/$L.plist"; echo "已卸载 $L"
    done
    ;;
  status)
    for j in "${JOBS[@]}"; do IFS=: read -r phase _ _ <<<"$j"; launchctl list | grep "$(label "$phase")" || echo "$(label "$phase") 未加载"; done
    ;;
  *) echo "用法：$0 install|uninstall|status"; exit 2;;
esac
