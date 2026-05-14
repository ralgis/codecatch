"""Smoke tests — confirm the scaffold is importable.

Real tests land alongside the modules they cover.
"""
from __future__ import annotations


def test_api_module_imports() -> None:
    import api  # noqa: F401
    assert api.__version__


def test_workers_module_imports() -> None:
    import workers  # noqa: F401
    import workers.main  # noqa: F401


def test_extractors_module_imports() -> None:
    import extractors  # noqa: F401
