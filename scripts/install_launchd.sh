#!/usr/bin/env bash
# 安装/卸载 myStock2 例行更新的 launchd 任务（用户级 LaunchAgents，可随时卸载）。
#   bash scripts/install_launchd.sh install     # 生成 plist 并加载
#   bash scripts/install_launchd.sh uninstall   # 卸载并删除 plist
#   bash scripts/install_launchd.sh status
# 时间为本机时区（当前为 PDT）。更新命令自带增量检查：同一目标（最近已收盘交易日）已成功完成且数据无陈旧时，
# 后面的备份时间点直接跳过（不连富途、不拉行情）；上次失败/陈旧/OpenD 没开时，备份时间点会自动重试。
# Mac 睡眠时 launchd 在唤醒后补跑，所以多个时间点也覆盖「合盖/睡眠错过」的情况。
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="${PY:-/opt/anaconda3/envs/mk/bin/python}"        # 与 V1 共用的默认环境
DEST="$HOME/Library/LaunchAgents"
LOGDIR="$REPO/data/logs"
PHASES=(hk pre us)
label() { echo "com.mystock2.update.$1"; }

case "${1:-}" in
  install)
    mkdir -p "$DEST" "$LOGDIR"
    for phase in "${PHASES[@]}"; do
      L="$(label "$phase")"; P="$DEST/$L.plist"
      "$PY" - "$phase" "$L" "$REPO" "$PY" "$LOGDIR" > "$P" <<'PYEOF'
import sys
from xml.sax.saxutils import escape
phase, label, repo, py, logdir = sys.argv[1:6]
# 周一至周五（1-5）；周六（6）补一次上周五的港股/美股收尾。时间：本机 PDT。
SCHEDULE = {
    "hk":  {"1-5": ["02:15", "03:30", "05:30", "08:00"], "6": ["10:00"]},     # 港股 16:00 HKT 收盘＝本机 01:00（冬令 00:00）
    "pre": {"1-5": ["05:45", "06:05", "06:20"]},                              # 美股 09:30 ET 开盘＝本机 06:30
    "us":  {"1-5": ["14:00", "14:45", "16:00", "18:00", "21:00"], "6": ["10:30"]},   # 美股 16:00 ET 收盘＝本机 13:00
}[phase]
items = []
for days, times in SCHEDULE.items():
    wds = range(1, 6) if days == "1-5" else [int(days)]
    for wd in wds:
        for t in times:
            h, m = t.split(":")
            items.append(f"    <dict><key>Weekday</key><integer>{wd}</integer><key>Hour</key><integer>{int(h)}</integer><key>Minute</key><integer>{int(m)}</integer></dict>")
args = "".join(f"<string>{escape(a)}</string>" for a in (py, "-m", "mystock2", "update", "--phase", phase, "--notify"))
print(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>WorkingDirectory</key><string>{escape(repo)}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StartCalendarInterval</key><array>
{chr(10).join(items)}
  </array>
  <key>StandardOutPath</key><string>{escape(logdir)}/launchd_{phase}.out.log</string>
  <key>StandardErrorPath</key><string>{escape(logdir)}/launchd_{phase}.err.log</string>
  <key>RunAtLoad</key><false/>
</dict></plist>''')
PYEOF
      plutil -lint "$P" >/dev/null
      launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$P"
      echo "已加载 ${L}（$(grep -c Weekday "$P") 个时间点）"
    done
    ;;
  uninstall)
    for phase in "${PHASES[@]}"; do
      L="$(label "$phase")"
      launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
      rm -f "$DEST/$L.plist"; echo "已卸载 ${L}"
    done
    ;;
  status)
    for phase in "${PHASES[@]}"; do launchctl list | grep "$(label "$phase")" || echo "$(label "$phase") 未加载"; done
    ;;
  *) echo "用法：$0 install|uninstall|status"; exit 2;;
esac
