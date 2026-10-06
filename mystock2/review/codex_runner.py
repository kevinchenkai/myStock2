"""调用本机 Codex CLI（`codex exec`，用 ChatGPT/Codex 订阅登录，不走 API Key）做一次性文本评价。

做法参照 model-bridge 技能的 codex 分支：只读沙箱、空的临时工作目录、关闭联网搜索、不保留会话；提示词走 stdin，
不出现在进程列表里。模型与 effort 由调用方指定，不因不可用而换模型。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_EFFORT = "medium"
CODEX_FALLBACKS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
)


class CodexError(RuntimeError):
    pass


def find_codex(override: str | None = None) -> str:
    env = os.environ.get("MYSTOCK2_CODEX_BIN")
    candidates = [override or env] if (override or env) else [shutil.which("codex"), str(Path.home() / ".local/bin/codex"), *CODEX_FALLBACKS]
    for c in dict.fromkeys(x for x in candidates if x):
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.absolute())
    raise CodexError("找不到 Codex CLI：请安装并登录（ChatGPT 桌面版自带），或在 config.yaml 的 review.codex_bin 指定路径")


def _events(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def run_codex(prompt: str, *, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, timeout: float = 900, codex_bin: str | None = None) -> dict:
    """返回 {ok, text, error, duration_s, exit_code, usage}。失败不抛异常（调用方记回执），找不到 CLI 才抛 CodexError。"""
    cli = find_codex(codex_bin)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mystock2-review-") as td:
        ans = Path(td) / "answer.txt"
        cmd = [cli, "exec", "--skip-git-repo-check", "--ephemeral", "--cd", td, "-m", model, "-c", f'model_reasoning_effort="{effort}"',
               "-c", 'web_search="disabled"', "--sandbox", "read-only", "--json", "--output-last-message", str(ans), "-"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                cwd=td, env=dict(os.environ), start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                out, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                out, err = proc.communicate()
        text = ans.read_text(encoding="utf-8") if ans.is_file() and not ans.is_symlink() else ""
    failure, usage = None, {}
    for v in _events(out or ""):
        if v.get("type") in ("error", "turn.failed"):
            failure = str(v.get("message") or v.get("error") or "Codex 报错")
        if v.get("type") == "turn.completed":
            failure = None
            if isinstance(v.get("usage"), dict):
                usage = v["usage"]
        item = v.get("item") or {}
        if v.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and not text and isinstance(item.get("text"), str):
            text = item["text"]
    dur = round(time.monotonic() - t0, 1)
    if timed_out:
        return {"ok": False, "text": "", "error": f"超时（{int(timeout)} 秒），已终止", "duration_s": dur, "exit_code": proc.returncode, "usage": usage}
    if proc.returncode != 0 and not text.strip():
        first = next((ln.strip() for ln in (err or "").splitlines() if ln.strip()), "")
        return {"ok": False, "text": "", "error": failure or f"Codex 退出码 {proc.returncode}：{first[:300]}", "duration_s": dur, "exit_code": proc.returncode, "usage": usage}
    if not text.strip():
        return {"ok": False, "text": "", "error": failure or "Codex 没有返回内容", "duration_s": dur, "exit_code": proc.returncode, "usage": usage}
    return {"ok": True, "text": text.strip(), "error": None, "duration_s": dur, "exit_code": proc.returncode, "usage": usage}
