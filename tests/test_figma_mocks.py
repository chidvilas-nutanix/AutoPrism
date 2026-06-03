"""Tests for the curated ``FigmaTreeMapping`` mock loader.

The loader lets ``map_figma_tree`` short-circuit to a hand-authored
JSON file when one exists for a given (file_key, node_id) pair.
These tests pin three things:

1. Hit path: a well-formed JSON returns a fully-validated
   :class:`FigmaTreeMapping`.
2. Miss path: an absent file returns ``None`` (so the tool falls
   through to the live walker).
3. Robustness: corrupt JSON or schema drift logs at WARNING and
   returns ``None`` — never silently serves bad data.

We also confirm the curated mock that ships with the repo
(``mocks/figma_tree/QjBuSKHooZN4GEzA2rJy6P__753_20750.json``)
validates against the current Pydantic model.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from prism_mcp.figma import FigmaTreeMapping, mock_path_for, try_load_mock
from prism_mcp.figma.fetch import ParsedFigmaUrl
from prism_mcp.figma.mocks import _ENV_VAR

_REPO_ROOT_MOCK_KEY = "QjBuSKHooZN4GEzA2rJy6P"
_REPO_ROOT_MOCK_NODE = "753:20750"
_REPO_DRIFT_MOCK_NODE = "752:13805"


def _parsed(file_key: str = "abc123", node_id: str = "1:1") -> ParsedFigmaUrl:
    return ParsedFigmaUrl(
        file_key=file_key,
        node_id=node_id,
        is_branch=False,
        original_url=f"https://www.figma.com/design/{file_key}/x?node-id={node_id.replace(':', '-')}",
    )


# --------------------------------------------------------------------------
# Filename / path helpers.
# --------------------------------------------------------------------------


def test_mock_path_for_uses_underscore_form(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, str(tmp_path))
    parsed = _parsed(file_key="MyFile", node_id="3800:49763")
    assert mock_path_for(parsed) == tmp_path / "MyFile__3800_49763.json"


def test_mock_path_for_falls_back_to_repo_root_when_env_unset(
    monkeypatch,
):
    monkeypatch.delenv(_ENV_VAR, raising=False)
    parsed = _parsed(file_key="x", node_id="1:1")
    # Anchored at the repo's mocks/figma_tree by default.
    assert mock_path_for(parsed).match("mocks/figma_tree/x__1_1.json")


# --------------------------------------------------------------------------
# Hit / miss paths.
# --------------------------------------------------------------------------


def test_try_load_mock_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, str(tmp_path))
    parsed = _parsed()
    assert try_load_mock(parsed) is None


def test_try_load_mock_returns_validated_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, str(tmp_path))
    parsed = _parsed(file_key="demo", node_id="5:5")

    payload = FigmaTreeMapping(summary={"input_nodes": 0}).model_dump(
        mode="json"
    )
    (tmp_path / "demo__5_5.json").write_text(json.dumps(payload))

    loaded = try_load_mock(parsed)
    assert loaded is not None
    assert loaded.summary == {"input_nodes": 0}
    assert loaded.agenda == []


def test_try_load_mock_returns_none_on_corrupt_json(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv(_ENV_VAR, str(tmp_path))
    parsed = _parsed(file_key="demo", node_id="6:6")
    (tmp_path / "demo__6_6.json").write_text("{not valid json")

    with caplog.at_level(logging.WARNING):
        assert try_load_mock(parsed) is None

    assert any(
        "unreadable" in record.message for record in caplog.records
    ), "expected a WARNING about the unreadable mock"


def test_try_load_mock_returns_none_on_schema_drift(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv(_ENV_VAR, str(tmp_path))
    parsed = _parsed(file_key="demo", node_id="7:7")
    (tmp_path / "demo__7_7.json").write_text(
        json.dumps({"agenda": "not a list"})
    )

    with caplog.at_level(logging.WARNING):
        assert try_load_mock(parsed) is None

    assert any(
        "validation" in record.message.lower() for record in caplog.records
    ), "expected a WARNING about schema drift"


# --------------------------------------------------------------------------
# The curated mock that ships with the repo must always be valid.
# --------------------------------------------------------------------------


def _repo_mocks_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "mocks" / "figma_tree"


@pytest.mark.skipif(
    not (
        _repo_mocks_dir()
        / f"{_REPO_ROOT_MOCK_KEY}__{_REPO_ROOT_MOCK_NODE.replace(':', '_')}.json"
    ).exists(),
    reason="curated mock not present in this checkout",
)
def test_curated_repo_mock_validates(monkeypatch):
    """The shipped 753:20750 mock round-trips through the model."""
    monkeypatch.setenv(_ENV_VAR, str(_repo_mocks_dir()))
    parsed = _parsed(
        file_key=_REPO_ROOT_MOCK_KEY,
        node_id=_REPO_ROOT_MOCK_NODE,
    )
    loaded = try_load_mock(parsed)
    assert loaded is not None, (
        "the curated mock should load when the mocks directory is pointed "
        "at the repo's mocks/figma_tree folder"
    )
    assert len(loaded.layout_tree) > 0
    assert len(loaded.agenda) > 0
    assert len(loaded.tokens) > 0
    assert "agenda_size" in loaded.summary
    assert loaded.summary["agenda_size"] == len(loaded.agenda)
    # The root agenda entry must alias the URL's node-id so Cursor can
    # cross-reference the response back to the original request.
    root = loaded.agenda[0]
    assert _REPO_ROOT_MOCK_NODE in (root.aliased_ids or []), (
        "root agenda entry must alias the URL node-id so the mock makes "
        "sense for the requested URL"
    )


@pytest.mark.skipif(
    not (
        _repo_mocks_dir()
        / f"{_REPO_ROOT_MOCK_KEY}__{_REPO_DRIFT_MOCK_NODE.replace(':', '_')}.json"
    ).exists(),
    reason="curated mock not present in this checkout",
)
def test_curated_drift_mock_validates(monkeypatch):
    """The shipped 752:13805 NCM Drift Management mock round-trips."""
    monkeypatch.setenv(_ENV_VAR, str(_repo_mocks_dir()))
    parsed = _parsed(
        file_key=_REPO_ROOT_MOCK_KEY,
        node_id=_REPO_DRIFT_MOCK_NODE,
    )
    loaded = try_load_mock(parsed)
    assert loaded is not None, (
        "the curated drift mock should load when the mocks directory is "
        "pointed at the repo's mocks/figma_tree folder"
    )
    assert len(loaded.layout_tree) > 0
    assert len(loaded.agenda) > 0
    assert len(loaded.tokens) > 0
    assert "agenda_size" in loaded.summary
    assert loaded.summary["agenda_size"] == len(loaded.agenda)
    # The root agenda entry must alias the URL's node-id so Cursor can
    # cross-reference the response back to the original request.
    root = loaded.agenda[0]
    assert _REPO_DRIFT_MOCK_NODE in (root.aliased_ids or []), (
        "root agenda entry must alias the URL node-id so the mock makes "
        "sense for the requested URL"
    )
