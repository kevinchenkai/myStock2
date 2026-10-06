"""视图发现与配置（实施方案 WP3.1、3.5）。

一个视图 = 一个文件夹 `<view_id>/{view.yaml, query.py, panel.js}`；启动时扫描所有视图目录，**新增视图只需新增一个文件夹**，
不改核心代码。`config/views.yaml` 控制启用/隐藏/顺序/默认参数。

坏视图（YAML 错、缺 `run`、含禁止的导入）不会拖垮整个应用：记为 `ViewProblem`，在 `/api/views` 里可见，其余视图照常工作。
`query.run(conn, params) -> dict` 约定为纯函数：只收只读连接，不得写库、不联网、不调用采集/训练。除了只读连接保证写入必败，
这里再对 `query.py` 做一次**静态导入检查**（「视图模块受代码审查」的机械部分）。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

BUILTIN_VIEWS_DIR = Path(__file__).resolve().parent / "views"

# 视图 query.py 不得导入的模块（前缀匹配）与不得引用的名字
FORBIDDEN_IMPORTS = (
    "mystock2.forecast", "mystock2.collectors", "mystock2.assistant", "yfinance", "futu", "requests", "urllib.request",
    "http.client", "socket", "subprocess", "sqlite3.dbapi2", "ftplib", "smtplib",
    "importlib", "ctypes", "multiprocessing", "os", "shutil",                 # 动态导入／绕过静态检查的途径（审核 W-01）
)
FORBIDDEN_NAMES = ("connect_writer", "connect_migrator", "migrate", "__import__")
# 取当前时间必须经 params["_now"]（common.now_of），保持纯函数、可固定时钟；连接只能用框架传入的只读连接
FORBIDDEN_CALLS = ("utc_now", "now", "utcnow", "connect", "exec", "eval", "compile", "open")

DATA_MODES = ("daily", "cached", "realtime")


class ViewError(ValueError):
    pass


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str = "str"                    # str | int | bool
    default: Any = ""
    choices: tuple | None = None
    description: str = ""
    min: int | None = None
    max: int | None = None

    def coerce(self, raw: str | None) -> Any:
        if raw is None:
            return self.default
        try:
            if self.type == "int":
                v: Any = int(raw)
                if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
                    raise ValueError
            elif self.type == "bool":
                if raw.lower() not in ("1", "0", "true", "false", "yes", "no", ""):
                    raise ValueError
                v = raw.lower() in ("1", "true", "yes")
            else:
                v = raw
        except ValueError:
            raise ViewError(f"参数 {self.name} 的值不合法：{raw!r}") from None
        if self.choices is not None and v not in self.choices:
            raise ViewError(f"参数 {self.name} 必须是 {list(self.choices)} 之一：{raw!r}")
        return v

    def public(self) -> dict:
        return {"name": self.name, "type": self.type, "default": self.default, "choices": list(self.choices) if self.choices is not None else None,
                "description": self.description}


@dataclass
class ViewSpec:
    id: str
    title: str
    description: str
    order: float
    data_mode: str
    stale_after_hours: float
    params: dict[str, ParamSpec]
    dir: Path
    run: Callable
    has_panel: bool
    origin: str = "builtin"


@dataclass(frozen=True)
class ViewProblem:
    view: str
    message: str

    def public(self) -> dict:
        return {"view": self.view, "message": self.message}


@dataclass
class ViewEntry:
    """配置应用之后的视图：是否启用/隐藏、默认参数、最终顺序。"""
    spec: ViewSpec
    enabled: bool = True
    hidden: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)
    position: int = 0

    def coerce_params(self, args: Any) -> dict[str, Any]:
        out = {}
        for name, p in self.spec.params.items():
            raw = args.get(name) if args is not None else None
            out[name] = p.coerce(raw) if raw is not None else self.defaults.get(name, p.default)
        return out


# ------------------------------------------------------------------ 静态检查
def lint_query_source(path: Path) -> list[str]:
    """机械检查 query.py：禁止导入联网/采集/训练/子进程模块，禁止出现写连接与迁移名字，禁止直接取当前时间。"""
    problems: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"query.py 语法错误：{exc}"]
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mods = [node.module or ""]
            mods += [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            for bad in FORBIDDEN_IMPORTS:
                if m == bad or m.startswith(bad + "."):
                    problems.append(f"禁止导入 {m}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"禁止引用 {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            problems.append(f"禁止引用 {node.attr}")
        if isinstance(node, ast.alias) and node.name.split(".")[-1] in FORBIDDEN_NAMES + FORBIDDEN_CALLS:
            problems.append(f"禁止导入 {node.name}")                          # 含 `utc_now as clock` 这类改名导入
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in FORBIDDEN_CALLS:
                problems.append(f"禁止直接调用 {name}()：当前时间请用 common.now_of(params)；数据库只用框架传入的只读连接")
    return sorted(set(problems))


# ------------------------------------------------------------------ 发现
def _parse_params(raw: Any, view_id: str) -> dict[str, ParamSpec]:
    out: dict[str, ParamSpec] = {}
    for name, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            raise ViewError(f"params.{name} 必须是映射")
        typ = spec.get("type", "str")
        if typ not in ("str", "int", "bool"):
            raise ViewError(f"params.{name}.type 不支持：{typ!r}")
        choices = spec.get("choices")
        out[name] = ParamSpec(name, typ, spec.get("default", "" if typ == "str" else None), tuple(choices) if choices is not None else None,
                              str(spec.get("description", "")), spec.get("min"), spec.get("max"))
    return out


def load_view(folder: Path, origin: str = "builtin") -> ViewSpec:
    vid = folder.name
    meta_path, query_path = folder / "view.yaml", folder / "query.py"
    if not meta_path.is_file() or not query_path.is_file():
        raise ViewError("缺少 view.yaml 或 query.py")
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    if not isinstance(meta, dict):
        raise ViewError("view.yaml 必须是映射")
    if meta.get("id", vid) != vid:
        raise ViewError(f"view.yaml 的 id（{meta.get('id')}）必须与文件夹名（{vid}）一致")
    if not vid.replace("_", "").isalnum() or vid[0].isdigit():
        raise ViewError("视图 id 只能由字母、数字、下划线组成且不以数字开头")
    mode = meta.get("data_mode", "cached")
    if mode not in DATA_MODES:
        raise ViewError(f"data_mode 必须是 {DATA_MODES}：{mode!r}")
    lint = lint_query_source(query_path)
    if lint:
        raise ViewError("query.py 未通过静态检查：" + "；".join(lint))
    mod_name = f"mystock2_web_views.{vid}"
    spec = importlib.util.spec_from_file_location(mod_name, query_path)
    if spec is None or spec.loader is None:
        raise ViewError("无法加载 query.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(mod_name, None)
        raise ViewError(f"query.py 加载失败：{type(exc).__name__}: {exc}") from exc
    run = getattr(module, "run", None)
    if not callable(run):
        raise ViewError("query.py 必须定义 run(conn, params) -> dict")
    return ViewSpec(
        id=vid, title=str(meta.get("title", vid)), description=str(meta.get("description", "")), order=float(meta.get("order", 1000)),
        data_mode=mode, stale_after_hours=float(meta.get("stale_after_hours", 72)), params=_parse_params(meta.get("params"), vid),
        dir=folder, run=run, has_panel=(folder / "panel.js").is_file(), origin=origin)


def discover_views(dirs: list[Path]) -> tuple[dict[str, ViewSpec], list[ViewProblem]]:
    found: dict[str, ViewSpec] = {}
    problems: list[ViewProblem] = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            problems.append(ViewProblem(str(d), "视图目录不存在"))
            continue
        origin = "builtin" if d.resolve() == BUILTIN_VIEWS_DIR.resolve() else "extra"
        for folder in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
            if not (folder / "view.yaml").exists() and not (folder / "query.py").exists():
                continue                                             # 不是视图文件夹（如 __pycache__）
            try:
                spec = load_view(folder, origin)
            except ViewError as exc:
                problems.append(ViewProblem(folder.name, str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 — 坏的 view.yaml（语法错、order 非数字…）不得拖垮整个应用（审核 W-02）
                problems.append(ViewProblem(folder.name, f"视图定义无法读取：{type(exc).__name__}: {exc}"))
                continue
            if spec.id in found:
                problems.append(ViewProblem(spec.id, f"视图 id 重复（{found[spec.id].dir} 与 {folder}）；保留先发现者"))
                continue
            found[spec.id] = spec
    return found, problems


# ------------------------------------------------------------------ config/views.yaml
def load_views_config(source: Path | str | dict | None) -> dict:
    if source is None:
        return {}
    if isinstance(source, dict):
        return source
    p = Path(source)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ViewError(f"{p} 必须是映射")
    return data


def apply_config(specs: dict[str, ViewSpec], cfg: dict) -> tuple[list[ViewEntry], list[ViewProblem]]:
    """顺序＝配置列表中的位置；未列出的视图按 view.yaml 的 order 排在其后（默认启用，新增文件夹即出现）。"""
    problems: list[ViewProblem] = []
    items = cfg.get("views") or []
    if isinstance(items, dict):
        items = [{"id": k, **(v or {})} for k, v in items.items()]
    if not isinstance(items, list):
        raise ViewError("views.yaml 的 views 必须是列表")
    entries: dict[str, ViewEntry] = {}
    pos = 0
    for it in items:
        if not isinstance(it, dict) or "id" not in it:
            problems.append(ViewProblem("views.yaml", f"忽略无 id 的条目：{it!r}"))
            continue
        vid = it["id"]
        if vid not in specs:
            problems.append(ViewProblem(str(vid), "views.yaml 中配置了不存在的视图"))
            continue
        if vid in entries:
            problems.append(ViewProblem(vid, "views.yaml 中重复配置，保留第一条"))
            continue
        defaults = dict(it.get("params") or {})
        for k in defaults:
            if k not in specs[vid].params:
                problems.append(ViewProblem(vid, f"views.yaml 为未声明的参数 {k} 给了默认值，已忽略"))
        checked = {}
        for k, v in defaults.items():
            if k not in specs[vid].params:
                continue
            try:                                                   # 配置里的默认值同样要过类型/范围/可选值校验（审核 P3）
                checked[k] = specs[vid].params[k].coerce(v)
            except (ViewError, ValueError, TypeError) as exc:
                problems.append(ViewProblem(vid, f"views.yaml 中参数 {k} 的默认值不合法（{exc}），已忽略"))
        defaults = checked
        entries[vid] = ViewEntry(specs[vid], bool(it.get("enabled", True)), bool(it.get("hidden", False)), defaults, pos)
        pos += 1
    rest = sorted((s for s in specs.values() if s.id not in entries), key=lambda s: (s.order, s.id))
    for s in rest:
        entries[s.id] = ViewEntry(s, True, False, {}, pos)
        pos += 1
    return sorted(entries.values(), key=lambda e: e.position), problems
