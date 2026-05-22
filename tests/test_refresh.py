"""Tests for the background refresh loop (Slice 7)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest

from prism_mcp.library import LibraryError, RefreshOutcome
from prism_mcp.refresh import RefreshLoop, RefreshLoopConfig


class _FakeLibrary:
    """Test double that replays a script of ``RefreshOutcome`` rows.

    Each call to ``refresh()`` pops the next row from ``script``. If
    the row is an :class:`Exception` it is raised instead — that's how
    we drive the loop's error paths (``LibraryError`` for cold-no-
    cache, generic ``RuntimeError`` for "unexpected"). The class is
    deliberately tiny: the loop only depends on ``refresh()``.
    """

    def __init__(
        self,
        script: list[RefreshOutcome | Exception],
    ) -> None:
        self._script: Iterator[RefreshOutcome | Exception] = iter(script)
        self.calls = 0

    def refresh(self) -> RefreshOutcome:
        self.calls += 1
        try:
            item = next(self._script)
        except StopIteration:
            raise AssertionError(
                "_FakeLibrary script exhausted; tighten the script "
                "or stop the loop sooner"
            ) from None
        if isinstance(item, Exception):
            raise item
        return item


def _outcome(
    *,
    version_before: str | None,
    version_after: str,
    swapped: bool = False,
    not_modified: bool = False,
    offline: bool = False,
) -> RefreshOutcome:
    """Compact builder for ``RefreshOutcome`` in tests."""
    return RefreshOutcome(
        version_before=version_before,
        version_after=version_after,
        swapped=swapped,
        not_modified=not_modified,
        offline=offline,
    )


@pytest.mark.asyncio
async def test_run_once_returns_outcome_on_success() -> None:
    """A successful refresh exposes its outcome via ``last_outcome``."""
    library = _FakeLibrary(
        [_outcome(version_before=None, version_after="1.0.0", swapped=True)]
    )
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    outcome = await loop.run_once()

    assert outcome is not None
    assert outcome.version_after == "1.0.0"
    assert loop.last_outcome is outcome


@pytest.mark.asyncio
async def test_run_once_swallows_library_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Library errors are logged but never crash the loop."""
    library = _FakeLibrary([LibraryError("offline; no cache; VPN")])
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    with caplog.at_level(logging.ERROR):
        outcome = await loop.run_once()

    assert outcome is None
    assert loop.last_outcome is None
    assert any(
        "no cache fallback" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_run_once_swallows_unexpected_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any other exception is captured by an ``exception()`` log call."""
    library = _FakeLibrary([RuntimeError("boom")])
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    with caplog.at_level(logging.ERROR):
        outcome = await loop.run_once()

    assert outcome is None
    assert any(
        "retry" in record.message and "unexpectedly" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_start_and_stop_ticks_at_least_twice() -> None:
    """Run-on-start + one interval tick produces two refreshes."""
    library = _FakeLibrary(
        [
            _outcome(version_before=None, version_after="1.0.0", swapped=True),
            _outcome(
                version_before="1.0.0",
                version_after="1.0.0",
                not_modified=True,
            ),
            _outcome(
                version_before="1.0.0",
                version_after="1.0.0",
                not_modified=True,
            ),
        ]
    )
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(
            interval_seconds=0.01,
            jitter_seconds=0.0,
            run_on_start=True,
        ),
    )

    loop.start()
    # Yield long enough for run-on-start + at least one tick.
    await asyncio.sleep(0.05)
    await loop.stop()

    assert library.calls >= 2
    assert loop.is_running is False


@pytest.mark.asyncio
async def test_stop_is_safe_when_never_started() -> None:
    """``stop()`` on an idle loop is a no-op."""
    library = _FakeLibrary([])
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    await loop.stop()  # must not raise


@pytest.mark.asyncio
async def test_start_twice_returns_same_task() -> None:
    """Re-entering ``start()`` is idempotent."""
    library = _FakeLibrary(
        [
            _outcome(
                version_before=None,
                version_after="1.0.0",
                swapped=True,
            )
        ]
        + [
            _outcome(
                version_before="1.0.0",
                version_after="1.0.0",
                not_modified=True,
            )
        ]
        * 20
    )
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=1.0, jitter_seconds=0.0),
    )

    task_one = loop.start()
    task_two = loop.start()

    try:
        assert task_one is task_two
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_run_once_logs_swap_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A swap fires an INFO log line that names both versions."""
    library = _FakeLibrary(
        [
            _outcome(
                version_before="1.0.0",
                version_after="2.0.0",
                swapped=True,
            )
        ]
    )
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    with caplog.at_level(logging.INFO):
        await loop.run_once()

    assert any(
        "swapped" in record.message
        and "version_before=1.0.0" in record.message
        and "version_after=2.0.0" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_offline_outcome_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A degraded-mode tick logs at WARNING level."""
    library = _FakeLibrary(
        [
            _outcome(
                version_before="1.0.0",
                version_after="1.0.0",
                offline=True,
            )
        ]
    )
    loop = RefreshLoop(
        library=library,  # type: ignore[arg-type]
        config=RefreshLoopConfig(interval_seconds=0.01, jitter_seconds=0.0),
    )

    with caplog.at_level(logging.WARNING):
        await loop.run_once()

    assert any(
        record.levelno == logging.WARNING
        and "fell back to cached" in record.message
        for record in caplog.records
    )
