import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def private_radio_store(tmp_path, monkeypatch):
    """The radio identity and keys shared with other radio apps live under
    MFruit OS's home; tests get their own, never the host's."""
    monkeypatch.setenv("MFRUIT_HOME", str(tmp_path / "mfruit-home"))
    monkeypatch.delenv("WHISPLAY_OS_HOME", raising=False)
