"""Unit tests for two-stage pre-flight guardrails."""

import pytest
from unittest.mock import patch, MagicMock

from tools.guardrails import shallow_check, deep_check
from github import GithubException


# ── shallow_check tests ───────────────────────────────────────────────────────

class TestTokenCheck:
    def test_missing_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        result = shallow_check("owner/repo")
        assert not result.passed
        assert "GITHUB_TOKEN" in result.message

    def test_token_present(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        with patch("tools.guardrails.get_repo", return_value=MagicMock()):
            with patch("tools.guardrails.get_tree", return_value=["app.py"]):
                result = shallow_check("owner/repo")
        assert result.passed


class TestRepoAccessible:
    def test_repo_not_found(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        exc = GithubException(404, {"message": "Not Found"}, {})
        with patch("tools.guardrails.get_repo", side_effect=exc):
            result = shallow_check("owner/missing")
        assert not result.passed
        assert "not found" in result.message.lower()

    def test_invalid_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        exc = GithubException(401, {"message": "Bad credentials"}, {})
        with patch("tools.guardrails.get_repo", side_effect=exc):
            result = shallow_check("owner/repo")
        assert not result.passed
        assert "invalid or expired" in result.message.lower()


class TestPythonRepo:
    def test_no_python_files(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        with patch("tools.guardrails.get_repo", return_value=MagicMock()):
            with patch("tools.guardrails.get_tree", return_value=["README.md"]):
                result = shallow_check("owner/repo")
        assert not result.passed
        assert "Python" in result.message

    def test_has_python_files(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        with patch("tools.guardrails.get_repo", return_value=MagicMock()):
            with patch("tools.guardrails.get_tree", return_value=["app.py", "client.py"]):
                result = shallow_check("owner/repo")
        assert result.passed


class TestRepoSize:
    def test_too_many_files(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        with patch("tools.guardrails.get_repo", return_value=MagicMock()):
            with patch("tools.guardrails.get_tree", return_value=[f"mod_{i}.py" for i in range(501)]):
                result = shallow_check("owner/repo")
        assert not result.passed
        assert "500" in result.message

    def test_within_limit(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        with patch("tools.guardrails.get_repo", return_value=MagicMock()):
            with patch("tools.guardrails.get_tree", return_value=["app.py"]):
                result = shallow_check("owner/repo")
        assert result.passed


# ── deep_check tests ──────────────────────────────────────────────────────────

class TestFlaskOrFastAPI:
    def test_no_entry_point(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1")
        result = deep_check("owner/repo", str(tmp_path))
        assert not result.passed
        assert "Flask" in result.message

    def test_flask_entry_point(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "import os, requests\n"
            "BASE = os.environ.get('STRIPE_BASE_URL')\n"
            "requests.post(BASE + '/v1/charges')\n"
            "app.run(host='0.0.0.0')\n"
        )
        result = deep_check("owner/repo", str(tmp_path))
        assert result.passed

    def test_fastapi_entry_point(self, tmp_path):
        (tmp_path / "main.py").write_text(
            "import os, requests\n"
            "BASE = os.environ.get('STRIPE_BASE_URL')\n"
            "requests.post(BASE + '/v1/charges')\n"
            "uvicorn.run(app, host='0.0.0.0')\n"
        )
        result = deep_check("owner/repo", str(tmp_path))
        assert result.passed


class TestHttpVendors:
    def test_no_http_calls(self, tmp_path):
        (tmp_path / "app.py").write_text("app.run()\n")
        result = deep_check("owner/repo", str(tmp_path))
        assert not result.passed
        assert "HTTP" in result.message

    def test_has_http_calls(self, tmp_path):
        (tmp_path / "client.py").write_text(
            "import os, requests\n"
            "BASE = os.environ.get('STRIPE_BASE_URL')\n"
            "requests.post(BASE + '/v1/charges')\n"
        )
        (tmp_path / "app.py").write_text("app.run()\n")
        result = deep_check("owner/repo", str(tmp_path))
        assert result.passed


class TestEnvVarUrls:
    def test_no_env_var_urls(self, tmp_path):
        (tmp_path / "client.py").write_text(
            "import requests\nrequests.get('https://api.stripe.com/charge')\n"
        )
        (tmp_path / "app.py").write_text("app.run()\n")
        result = deep_check("owner/repo", str(tmp_path))
        assert not result.passed
        assert "env" in result.message.lower()

    def test_has_env_var_url(self, tmp_path):
        (tmp_path / "client.py").write_text(
            "import os, requests\n"
            "BASE = os.environ.get('STRIPE_BASE_URL')\n"
            "requests.post(BASE + '/v1/charges')\n"
        )
        (tmp_path / "app.py").write_text("app.run()\n")
        result = deep_check("owner/repo", str(tmp_path))
        assert result.passed
