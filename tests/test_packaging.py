"""The two places this app declares its dependencies must agree.

`requirements.txt` is what the build and the dev venv install; the `dependencies`
list in `pyproject.toml` is what `pip install waikiki` resolves. Packaging for
PyPI created that duplication, and a Dependabot PR moves one file at a time — so
without this the wheel on PyPI could pin something different from the app we
test and ship, and nothing would say so.
"""
from __future__ import annotations

import pathlib
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    out = []
    for line in (_ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            out.append(line)
    return sorted(out)


def _pyproject() -> list[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return sorted(data["project"]["dependencies"])


def test_pyproject_dependencies_match_requirements():
    req, proj = _requirements(), _pyproject()
    assert proj == req, (
        "requirements.txt and pyproject.toml disagree — the wheel would pin "
        f"something the app does not.\n  only in requirements: {set(req) - set(proj)}"
        f"\n  only in pyproject:     {set(proj) - set(req)}")


def test_the_packaged_version_matches_the_app_version():
    """A wheel that says 1.0.2 while the app says 1.0.3 is a support nightmare."""
    import waikiki

    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert data["project"]["version"] == waikiki.__version__
