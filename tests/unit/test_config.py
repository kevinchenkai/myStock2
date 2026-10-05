
import pytest
import yaml

from mystock2.core.config import REPO_ROOT, ConfigError, is_loopback, load_config, parse_config

GOOD = {
    "futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"},
    "collect": {"markets": ["HK", "US"]},
    "db": {"path": "data/x.db"},
    "web": {"host": "127.0.0.1", "port": 8889},
}


def mut(**over):
    d = yaml.safe_load(yaml.safe_dump(GOOD))
    for k, v in over.items():
        sect, key = k.split("__")
        d[sect][key] = v
    return d


def test_example_config_loads_and_uses_dev_port():
    cfg = load_config(root=REPO_ROOT)
    assert cfg.web.port == 8889 and cfg.web.host == "127.0.0.1"
    assert cfg.db_path == REPO_ROOT / "data" / "mystock2.db"


def test_non_loopback_web_host_rejected():
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(ConfigError, match="回环"):
            parse_config(mut(web__host=host))
    assert is_loopback("::1") and is_loopback("127.0.0.1") and is_loopback("localhost")


def test_missing_required_field_is_an_error():
    d = mut()
    del d["web"]["port"]
    with pytest.raises(ConfigError, match="web.port"):
        parse_config(d)


def test_invalid_values_rejected():
    with pytest.raises(ConfigError):
        parse_config(mut(web__port=70000))
    with pytest.raises(ConfigError):
        parse_config(mut(futu__trd_env="DEMO"))
    d = mut()
    d["collect"]["markets"] = ["JP"]
    with pytest.raises(ConfigError):
        parse_config(d)


def test_trade_password_in_config_is_rejected_and_env_is_used(monkeypatch):
    d = mut()
    d["futu"]["trade_pwd"] = "secret"
    with pytest.raises(ConfigError, match="环境变量"):
        parse_config(d)
    cfg = parse_config(mut())
    monkeypatch.setenv("MYSTOCK2_FUTU_TRADE_PWD", "pw")
    assert cfg.futu_trade_password() == "pw"
    assert "pw" not in repr(cfg)                      # 不进 repr


def test_explicit_path_and_missing_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(GOOD), encoding="utf-8")
    assert not load_config(p).is_example
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
