from __future__ import annotations

import json
from pathlib import Path

from wxcalls.artifacts import ArtifactWriter, sanitize_filename


def test_artifact_writer_writes_timeline(tmp_path: Path) -> None:
    """Verify timeline events are written as JSON artifacts.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    writer = ArtifactWriter(tmp_path, run_id="run-1")
    writer.record_event("hello", value=1)

    timeline = writer.finalize()

    assert timeline.exists()
    data = json.loads(timeline.read_text(encoding="utf-8"))
    assert data[0]["event"] == "hello"
    assert data[0]["value"] == 1


def test_sanitize_filename_has_safe_fallback() -> None:
    """Verify artifact filename sanitization and fallback naming.

    :returns: None.
    """

    assert sanitize_filename("call 1 / alice") == "call-1-alice"
    assert sanitize_filename(" !!! ") == "artifact"
