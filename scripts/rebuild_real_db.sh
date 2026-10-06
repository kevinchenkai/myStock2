#!/usr/bin/env bash
# 重建 V2 真实库（删库重来）。需要：OpenD 已启动并登录；负责人已授权只读采集。V2 账本是派生库，可重建；
# 但库里也有**补不回**的数据（V2 自己归档的小时线超过 60 天的部分、前向预测/操作单/人类计划/暴露日志等前向记录），
# 所以本脚本：先校验全部输入 → 拿例行更新的锁 → 备份到 backups/ → 有前向记录时拒绝 → 才删库重建。
#
# 用法：CONFIRM=yes ACC_ID=<富途 acc_id> V1_DB=<V1 库的只读备份> [V1_ML_DB=<V1 ML 库的只读备份>] OPEN_AT=2024-10-20T00:00:00Z \
#       [CASHFLOW_FILE=<原始资金流水 jsonl>] [PY=/opt/anaconda3/envs/mk/bin/python] bash scripts/rebuild_real_db.sh
# 删除的是当前配置（MYSTOCK2_CONFIG 或 config.yaml）里的 db.path，不是写死的路径；配置回退到 example 时拒绝。
# 有前向记录仍要重建：另加 DROP_FORWARD_RECORDS=yes（备份照做）。账户号只通过环境变量传入，不写进仓库。
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ACC_ID:?需要 ACC_ID}" "${V1_DB:?需要 V1_DB}" "${OPEN_AT:?需要 OPEN_AT（已定开账点 2024-10-20T00:00:00Z）}"
ACCOUNT=${ACCOUNT:-main}
START=${START:-2024-01-01}
END=${END:-$(date +%Y-%m-%d)}
PY=${PY:-python}
MAP=config/local/futu_cashflow_map.yaml

die() { echo "拒绝：$*" >&2; exit 2; }

# ---- 1. 校验全部输入（任何一项不满足都不碰库）
[ -r "$V1_DB" ] || die "V1_DB 不存在或不可读：$V1_DB"
[ -z "${V1_ML_DB:-}" ] || [ -r "$V1_ML_DB" ] || die "V1_ML_DB 不存在或不可读：$V1_ML_DB"
[ -r "$MAP" ] || die "缺少资金流水类型映射：$MAP"
[ -z "${CASHFLOW_FILE:-}" ] || [ -r "$CASHFLOW_FILE" ] || die "CASHFLOW_FILE 不存在：$CASHFLOW_FILE"
CFG_JSON=$("$PY" -m mystock2 config-show) || die "无法读取配置（$PY -m mystock2 config-show 失败）"
DB_PATH=$(printf '%s' "$CFG_JSON" | "$PY" -c 'import json,sys; c=json.load(sys.stdin); print(c["db_path"])')
IS_EXAMPLE=$(printf '%s' "$CFG_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["is_example"])')
[ "$IS_EXAMPLE" = "False" ] || die "配置回退到了 config.example.yaml：不在模板配置上删库"
[ "${CONFIRM:-}" = "yes" ] || die "将删除并重建 ${DB_PATH}；确认后加 CONFIRM=yes 重新执行"

# ---- 2. 与例行更新互斥：持有 data/logs/update.lock 直到脚本结束（flock 锁跟随打开的文件描述，子进程上锁后由本 shell 的 fd 9 保持）
mkdir -p data/logs backups
exec 9>>data/logs/update.lock
"$PY" -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null || die "例行更新正在运行（data/logs/update.lock 被占用）"

# ---- 3. 备份；有前向记录（补不回）时拒绝
if [ -f "$DB_PATH" ]; then
  BK="backups/mystock2_before_rebuild_$(date +%Y%m%d_%H%M%S).db"
  "$PY" - "$DB_PATH" "$BK" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
PYEOF
  echo "已备份：$BK"
  FORWARD=$("$PY" - "$DB_PATH" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
out = []
for t, where in (("ticket", ""), ("intent", ""), ("intent_exposure", ""), ("protocol_freeze", ""), ("comparison_batch", ""),
                 ("veto_packet", ""), ("llm_call", ""), ("prediction_version", " WHERE source_tag='forward'")):
    if t in have and c.execute(f"SELECT COUNT(*) FROM {t}{where}").fetchone()[0]:
        out.append(t)
print(",".join(out))
PYEOF
)
  if [ -n "$FORWARD" ] && [ "${DROP_FORWARD_RECORDS:-}" != "yes" ]; then
    die "库里有补不回的前向记录（${FORWARD}）；确需重建另加 DROP_FORWARD_RECORDS=yes（备份已在 ${BK}）"
  fi
fi

# ---- 4. 删库重建
rm -f "$DB_PATH" "$DB_PATH-shm" "$DB_PATH-wal"
"$PY" -m mystock2 db migrate
"$PY" -m mystock2 v1 import --v1-db "$V1_DB" --account-id "$ACCOUNT" --archive ${V1_ML_DB:+--v1-ml-db "$V1_ML_DB"}
"$PY" -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what deals,orders,fees,snapshot --assume-market-currency
if [ -n "${CASHFLOW_FILE:-}" ]; then
  "$PY" -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what cashflow --cashflow-file "$CASHFLOW_FILE" --cashflow-map "$MAP"
else
  "$PY" -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what cashflow --cashflow-map "$MAP"
fi
"$PY" -m mystock2 ledger open --account-id "$ACCOUNT" --at "$OPEN_AT"
rc=0
"$PY" -m mystock2 ledger reconcile --account-id "$ACCOUNT" || rc=$?
echo "账本已重建（对账退出码 ${rc}；非 0 表示有差异，见上方输出）。行情/汇率/预测另行：collect quotes …、forecast run …"
exit "$rc"
