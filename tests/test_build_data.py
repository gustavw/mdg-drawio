"""Regression tests for transactional generated-data installation."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_data


def _fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    *, fail_validation: bool,
) -> None:
    monkeypatch.setattr(build_data, "DATA_DIR", data_dir)

    def fake_run(script: Path, *args: str) -> None:
        if script.name != "build_notation_styles.py":
            return
        assert args[0] == "--output-dir"
        staged = Path(args[1])
        staged.mkdir(parents=True)
        (staged / "new.json").write_text("new", encoding="utf-8")
        if fail_validation:
            raise SystemExit(1)

    monkeypatch.setattr(build_data, "run", fake_run)


def test_failed_staged_build_preserves_previous_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "generated_data"
    data_dir.mkdir()
    (data_dir / "old.json").write_text("old", encoding="utf-8")
    _fake_pipeline(monkeypatch, data_dir, fail_validation=True)

    with pytest.raises(SystemExit):
        build_data.main()

    assert (data_dir / "old.json").read_text(encoding="utf-8") == "old"
    assert not (data_dir / "new.json").exists()


def test_successful_staged_build_replaces_previous_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "generated_data"
    data_dir.mkdir()
    (data_dir / "old.json").write_text("old", encoding="utf-8")
    _fake_pipeline(monkeypatch, data_dir, fail_validation=False)

    build_data.main()

    assert (data_dir / "new.json").read_text(encoding="utf-8") == "new"
    assert not (data_dir / "old.json").exists()
