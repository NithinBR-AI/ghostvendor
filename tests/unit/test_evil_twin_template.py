"""Unit tests for evil_twin_template.py — ensures generated twin harness uses asyncio.Lock."""

import ast
import pytest
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "src" / "tools" / "evil_twin_template.py"


def _template_source() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


class TestEvilTwinTemplate:
    def test_uses_asyncio_lock_not_threading(self):
        source = _template_source()
        assert "asyncio.Lock()" in source, "Template must use asyncio.Lock()"
        assert "threading.Lock()" not in source, "Template must not use threading.Lock()"

    def test_async_with_not_bare_with(self):
        source = _template_source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                pytest.fail(
                    f"Line {node.lineno}: bare 'with' found — all lock usage must be 'async with'"
                )

    def test_parses_as_valid_python(self):
        source = _template_source()
        try:
            ast.parse(source)
        except SyntaxError as e:
            pytest.fail(f"evil_twin_template.py has a syntax error: {e}")

    def test_imports_asyncio(self):
        source = _template_source()
        assert "import asyncio" in source
