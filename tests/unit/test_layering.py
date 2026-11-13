"""계층 가드(Gate3 F10): `bot/formatting`은 순수 렌더링 모듈이다. `ipobot.*`(서비스·DB·봇 결합)에 의존하지 않는다."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import ipobot.bot.formatting as formatting

FORMATTING = Path(formatting.__file__)
BOT_INIT = FORMATTING.parent / "__init__.py"
SRC = FORMATTING.parents[2]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module)
    return names


def test_formatting_imports_no_ipobot_modules():
    imported = _imported_modules(FORMATTING)
    offending = {m for m in imported if m == "ipobot" or m.startswith(("ipobot.", "."))}
    assert offending == set()


def test_bot_package_init_is_empty():
    assert BOT_INIT.read_text(encoding="utf-8").strip() == ""


def test_importing_formatting_loads_no_service_db_or_app_modules():
    code = (
        "import sys, ipobot.bot.formatting\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('ipobot.services', 'ipobot.db', 'ipobot.bot.app')))\n"
        "print(','.join(bad))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env)
    assert out.stdout.strip() == ""
