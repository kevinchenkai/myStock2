"""依赖边界（实施方案 §3.4）：扫描 mystock2 的 import 图，违反即失败。"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "mystock2"

# 子包 -> 允许依赖的其他 mystock2 子包（core 与自身总是允许）
ALLOWED = {
    "core": set(),
    "instruments": {"core"},
    "collectors": {"core", "instruments", "ledger", "market"},   # 采集器把外部数据写入账本/行情
    "ledger": {"core", "instruments"},
    "market": {"core", "instruments"},
    "forecast": {"core", "instruments", "market"},
    "coach": {"core", "instruments", "ledger", "market", "forecast"},
    "scoreboard": {"core", "instruments", "ledger", "market", "coach"},
    "replay": {"core", "instruments", "ledger", "market", "coach", "scoreboard"},
    "assistant": {"core", "instruments", "coach"},   # 不得依赖 ledger（无账本写接口）
    "web": {"core", "instruments", "ledger", "market", "coach", "scoreboard", "replay"},   # 不得依赖 forecast/collectors/assistant
    "cli": None,   # 入口：可依赖任何子包
}


def imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:   # 相对导入：同包内，忽略
                continue
            yield node.module or ""


def subpackage_of(path: Path) -> str | None:
    rel = path.relative_to(PKG)
    return rel.parts[0] if len(rel.parts) > 1 else None


def test_all_subpackages_are_declared():
    actual = {p.name for p in PKG.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert actual <= set(ALLOWED), f"新子包须在 ALLOWED 中声明依赖：{actual - set(ALLOWED)}"


def test_no_forbidden_imports():
    violations = []
    for py in PKG.rglob("*.py"):
        sub = subpackage_of(py)
        if sub is None or ALLOWED.get(sub) is None:
            continue
        for mod in imports_of(py):
            parts = mod.split(".")
            if parts[0] != "mystock2" or len(parts) < 2:
                continue
            target = parts[1]
            if target != sub and target not in ALLOWED[sub]:
                violations.append(f"{py.relative_to(PKG.parent)} 导入 {mod}（{sub} 不得依赖 {target}）")
    assert not violations, "\n".join(violations)


def test_core_has_no_third_party_service_clients():
    banned = {"futu", "yfinance", "requests", "flask"}
    for py in (PKG / "core").rglob("*.py"):
        for mod in imports_of(py):
            assert mod.split(".")[0] not in banned, f"{py.name} 导入 {mod}"


def test_web_never_opens_sqlite_directly_for_writing():
    """AST 检查：web 代码里不得出现对写连接/迁移的名字引用或导入（字符串常量不算）。"""
    forbidden = {"connect_writer", "connect_migrator", "migrate"}
    for py in (PKG / "web").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names |= {a.name.split(".")[-1] for a in node.names}
            assert not (names & forbidden), f"{py.relative_to(PKG.parent)} 引用了写连接/迁移：{names & forbidden}"
