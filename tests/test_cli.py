"""Tests for the `mdg` CLI shell — verb dispatch, positional-arg convert
syntax, the top-level overview help, and the `notation` verb.

Conversion pipeline correctness itself is covered by test_pipeline.py; this
file is about cli.py's own argument handling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdg_drawio.cli import main
from mdg_drawio.notation import LIBRARIES

_C4_SOURCE = 'use c4\n\nc4.Person(user, "User")\nc4.System(sys1, "System")\n'


def _write_mdg(tmp_path: Path, name: str = "diagram.mdg") -> Path:
    path = tmp_path / name
    path.write_text(_C4_SOURCE, encoding="utf-8")
    return path


# ── convert: positional args, no -i/-o ───────────────────────────────────────


def test_convert_with_explicit_output(tmp_path: Path) -> None:
    src = _write_mdg(tmp_path)
    out = tmp_path / "out.drawio"
    assert main([str(src), str(out), "--force"]) == 0
    assert out.exists()


def test_convert_derives_output_from_input_stem(tmp_path: Path) -> None:
    src = _write_mdg(tmp_path)
    assert main([str(src), "--force"]) == 0
    assert (tmp_path / "diagram.drawio").exists()


def test_convert_rejects_non_mdg_input(tmp_path: Path) -> None:
    bad = tmp_path / "diagram.txt"
    bad.write_text(_C4_SOURCE, encoding="utf-8")
    assert main([str(bad)]) == 1


def test_convert_rejects_non_drawio_output(tmp_path: Path) -> None:
    src = _write_mdg(tmp_path)
    assert main([str(src), str(tmp_path / "out.txt")]) == 1


# ── top-level help ───────────────────────────────────────────────────────────


def test_bare_invocation_shows_overview_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "mdg merge" in out
    assert "mdg derive" in out
    assert "mdg notation" in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_bare_help_flag_shows_overview_and_exits_zero(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    assert main([flag]) == 0
    out = capsys.readouterr().out
    assert "mdg merge" in out
    assert "mdg notation" in out


# ── notation verb ────────────────────────────────────────────────────────────


def test_notation_with_no_library_lists_all_libraries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["notation"]) == 0
    out = capsys.readouterr().out
    for lib in LIBRARIES:
        assert lib in out


def test_notation_with_library_prints_its_palette(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["notation", "c4"]) == 0
    out = capsys.readouterr().out
    assert "c4.Person" in out
    assert 'c4.Person(n1,' in out


def test_notation_rejects_unknown_library() -> None:
    assert main(["notation", "not-a-real-library"]) == 1


def test_notation_json_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["notation", "c4", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert any(entry["function"] == "Person" for entry in data)
