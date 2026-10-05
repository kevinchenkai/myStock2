"""配置加载与校验（实施方案 WP1.6、NF-06）。

- 私有配置 `config.yaml`（忽略，不提交）；不存在时回退 `config.example.yaml` 并标 `is_example=True`。
- Web 只允许监听回环地址；开发期端口默认 8889（V1 占用 8888，见实施方案 §3.7）。
- 交易密码只来自环境变量 `MYSTOCK2_FUTU_TRADE_PWD`，**不得**写入配置、日志或 repr。
- 缺必填字段明确报错，不静默补默认值。
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_CONFIG = "MYSTOCK2_CONFIG"
ENV_FUTU_PWD = "MYSTOCK2_FUTU_TRADE_PWD"
LOOPBACK_NAMES = {"localhost"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class FutuConfig:
    host: str
    port: int
    trd_env: str  # REAL / SIMULATE；历史成交仅支持 REAL


@dataclass(frozen=True)
class Config:
    db_path: Path
    web: WebConfig
    futu: FutuConfig
    markets: tuple[str, ...]
    is_example: bool = False
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def futu_trade_password(self) -> str | None:
        """仅从环境变量读取；调用方不得记录返回值。"""
        return os.environ.get(ENV_FUTU_PWD) or None


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"配置缺少必填字段：{where}.{key}")
    return d[key]


def parse_config(data: dict[str, Any], *, base: Path | None = None, is_example: bool = False, source: Path | None = None) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("配置文件内容必须是 YAML 映射")
    web = _require(data, "web", "")
    futu = _require(data, "futu", "")
    db = _require(data, "db", "")
    collect = _require(data, "collect", "")
    host = str(_require(web, "host", "web"))
    if not is_loopback(host):
        raise ConfigError(f"web.host 必须是回环地址（得到 {host!r}）：本地服务仅监听 127.0.0.1/::1/localhost（NF-06）")
    port = _require(web, "port", "web")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"web.port 不合法：{port!r}")
    trd_env = str(_require(futu, "trd_env", "futu")).upper()
    if trd_env not in ("REAL", "SIMULATE"):
        raise ConfigError(f"futu.trd_env 必须是 REAL 或 SIMULATE：{trd_env!r}")
    if "trade_pwd" in futu and futu["trade_pwd"]:
        raise ConfigError(f"请勿把交易密码写进配置文件，改用环境变量 {ENV_FUTU_PWD}")
    markets = tuple(str(m).upper() for m in _require(collect, "markets", "collect"))
    bad = [m for m in markets if m not in ("HK", "US")]
    if bad or not markets:
        raise ConfigError(f"collect.markets 只支持 HK/US：{markets!r}")
    path = Path(str(_require(db, "path", "db")))
    if not path.is_absolute():
        path = (base or REPO_ROOT) / path
    return Config(
        db_path=path,
        web=WebConfig(host=host, port=port),
        futu=FutuConfig(host=str(_require(futu, "host", "futu")), port=int(_require(futu, "port", "futu")), trd_env=trd_env),
        markets=markets,
        is_example=is_example,
        source=source,
        raw=data,
    )


def load_config(path: str | Path | None = None, *, root: Path | None = None) -> Config:
    root = root or REPO_ROOT
    chosen = path or os.environ.get(ENV_CONFIG)
    if chosen:
        p = Path(chosen)
        if not p.exists():
            raise ConfigError(f"配置文件不存在：{p}")
        is_example = False
    elif (root / "config.yaml").exists():
        p, is_example = root / "config.yaml", False
    else:
        p, is_example = root / "config.example.yaml", True
        if not p.exists():
            raise ConfigError("找不到 config.yaml 与 config.example.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return parse_config(data, base=root, is_example=is_example, source=p)
