"""Unit tests for the deterministic AST scanner."""

import pytest
from pathlib import Path
from tools.ast_scanner import scan_directory, scan_result_to_dict


def _write(tmp_path, filename, content):
    f = tmp_path / filename
    f.write_text(content, encoding="utf-8")
    return f


class TestAstScanner:
    def test_detects_requests_get(self, tmp_path):
        _write(tmp_path, "client.py", (
            "import os, requests\n"
            "BASE = os.environ.get('STRIPE_BASE_URL')\n"
            "requests.get(BASE + '/v1/charges')\n"
        ))
        result = scan_directory(str(tmp_path))
        findings = scan_result_to_dict(result)
        assert len(findings["http_calls"]) >= 1
        assert any(c["method"].upper() == "GET" for c in findings["http_calls"])

    def test_detects_env_var(self, tmp_path):
        _write(tmp_path, "client.py", (
            "import os, requests\n"
            "BASE = os.environ.get('VENDOR_URL')\n"
            "requests.post(BASE + '/send')\n"
        ))
        result = scan_directory(str(tmp_path))
        findings = scan_result_to_dict(result)
        assert "VENDOR_URL" in findings["env_vars"]

    def test_no_http_calls_in_empty_file(self, tmp_path):
        _write(tmp_path, "utils.py", "def helper(): return 42\n")
        result = scan_directory(str(tmp_path))
        findings = scan_result_to_dict(result)
        assert findings["http_calls"] == []

    def test_skips_venv(self, tmp_path):
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        _write(venv, "vendor.py", (
            "import requests\n"
            "requests.get('https://api.stripe.com')\n"
        ))
        result = scan_directory(str(tmp_path))
        findings = scan_result_to_dict(result)
        assert findings["http_calls"] == []
