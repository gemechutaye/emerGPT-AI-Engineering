"""Independent assertions may reuse existing provider doubles; no provider credentials."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api/tests"))


@pytest.fixture(autouse=True)
def no_paid_provider_configuration(monkeypatch):
    from emer.settings import settings

    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
