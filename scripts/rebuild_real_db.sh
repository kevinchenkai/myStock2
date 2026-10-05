#!/usr/bin/env bash
# 重建 V2 真实库（删库重来）。需要：OpenD 已启动并登录；负责人已授权只读采集。V2 库是派生库，可随时重建。
# 用法：ACC_ID=<富途 acc_id> V1_DB=<V1 库的只读备份> [V1_ML_DB=<V1 ML 库的只读备份>] OPEN_AT=2024-10-01T00:00:00Z \
#       CASHFLOW_FILE=<原始资金流水 jsonl，可选> bash scripts/rebuild_real_db.sh
# 账户号只通过环境变量传入，不写进仓库。
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ACC_ID:?需要 ACC_ID}" "${V1_DB:?需要 V1_DB}" "${OPEN_AT:?需要 OPEN_AT}"
ACCOUNT=${ACCOUNT:-main}
START=${START:-2024-01-01}
END=${END:-$(date +%Y-%m-%d)}
PY=${PY:-python}
MAP=config/local/futu_cashflow_map.yaml

rm -f data/mystock2.db data/mystock2.db-shm data/mystock2.db-wal
$PY -m mystock2 db migrate
$PY -m mystock2 v1 import --v1-db "$V1_DB" --account-id "$ACCOUNT" --archive ${V1_ML_DB:+--v1-ml-db "$V1_ML_DB"}
$PY -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what deals,orders,fees,snapshot --assume-market-currency
if [ -n "${CASHFLOW_FILE:-}" ]; then
  $PY -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what cashflow --cashflow-file "$CASHFLOW_FILE" --cashflow-map "$MAP"
else
  $PY -m mystock2 collect futu --account-id "$ACCOUNT" --acc-id "$ACC_ID" --start "$START" --end "$END" --what cashflow --cashflow-map "$MAP"
fi
$PY -m mystock2 ledger open --account-id "$ACCOUNT" --at "$OPEN_AT"
$PY -m mystock2 ledger reconcile --account-id "$ACCOUNT" || true
echo "账本已重建。行情/汇率/预测另行：collect quotes …、forecast run …"
