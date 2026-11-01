import sys

import pytest

from anki_voice import server


def test_runs_on_macos_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["anki-voice", "serve", "--stdio"])
    with pytest.raises(SystemExit, match="macOS only"):
        server.main()
