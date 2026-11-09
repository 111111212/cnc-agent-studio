#!/usr/bin/env python3
"""提交前密钥闸门：发现疑似真实凭据就拒绝提交。

用法：
    python scripts/check_secrets.py --staged    # pre-commit / CI 用，只查暂存区
    python scripts/check_secrets.py             # 查工作区全部已跟踪文件

设计原则：
- 只拦「看起来像真实凭据」的串，文档里的占位符/示例不误伤；
- 命中时只报「文件:行号 + 类型」，绝不把匹配内容再打印一遍；
- 退出码 0=干净，1=拦截，2=脚本自身异常（同样拒绝提交）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 高置信度模式。注意：本文件里的模式串本身不会被自己命中，
# 因为 "sk-" 后面紧跟着 "[" 而不是 20 个连续字母数字。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI 风格 key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub 令牌", re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}\b")),
    ("Slack 令牌", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("私钥头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 明文", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{24,}")),
    ("硬编码凭据赋值", re.compile(
        r"""(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|panel[_-]?token)"""
        r"""\b\s*[:=]\s*["']([^"'\s]{16,})["']""")),
]

# 明显的占位符 / 代码标识符 / 模板变量，不算泄露
PLACEHOLDER = re.compile(
    r"(?i)(your|xxx|placeholder|example|dummy|changeme|fake|sample|test|demo|"
    r"replace|填写|占位|示例|配置|os\.getenv|getenv|settings\.|self\.|<|\$\{)"
)

# 这些文件名一旦被暂存就必须拦下（正常情况它们应被 .gitignore 挡住）
SENSITIVE_NAME = re.compile(
    r"(?:^|/)\.env(?:\.|$)|\.pem$|\.p12$|\.pfx$|\.key$|"
    r"credentials\.json$|cookies\.txt$|id_rsa$|.*secret.*\.json$"
)
ALLOWED_ENV_FILES = {".env.example"}

# 扫描器自身与测试文件里会拼接构造样本，跳过避免自锁
SKIP_PATHS = {"scripts/check_secrets.py", "tests/test_secrets.py"}


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", errors="replace")


def _findings(text: str, path: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, rx in PATTERNS:
            for m in rx.finditer(line):
                val = m.group(1) if m.lastindex else m.group(0)
                if PLACEHOLDER.search(val):
                    continue
                out.append((path, lineno, label))
    return out


def _content(path: str, staged: bool) -> str:
    if staged:
        r = subprocess.run(["git", "show", f":{path}"], cwd=ROOT,
                           capture_output=True)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan(staged: bool) -> tuple[list[tuple[str, int, str]], list[str], int]:
    names = (_git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines()
             if staged else _git("ls-files").splitlines())
    hits: list[tuple[str, int, str]] = []
    blocked_files: list[str] = []
    for name in names:
        name = name.strip()
        if not name or name in ALLOWED_ENV_FILES:
            continue
        if SENSITIVE_NAME.search(name):
            blocked_files.append(name)
            continue
        if name in SKIP_PATHS:
            continue
        text = _content(name, staged)
        if text:
            hits.extend(_findings(text, name))
    return hits, blocked_files, len(names)


def main() -> int:
    ap = argparse.ArgumentParser(description="提交前密钥扫描")
    ap.add_argument("--staged", action="store_true", help="只检查暂存区")
    args = ap.parse_args()
    try:
        hits, blocked, total = scan(args.staged)
    except Exception as exc:  # 脚本出错时同样拒绝提交，fail-closed
        print(f"[secrets] 扫描器异常，拒绝提交：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if not hits and not blocked:
        print(f"[secrets] 已检查 {total} 个文件，未发现凭据。")
        return 0

    print("[secrets] 发现疑似真实凭据，提交被拒绝：", file=sys.stderr)
    for f in blocked:
        print(f"  {f}  <- 敏感文件名不应入库（请放进本机 .env）", file=sys.stderr)
    for path, line, label in hits[:30]:
        print(f"  {path}:{line}  <- {label}", file=sys.stderr)
    print("  处理：把值移到本机 .env（已被 .gitignore 忽略），仓库里只留空值占位符。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
