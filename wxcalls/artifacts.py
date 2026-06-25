"""Run artifact management for scenario timelines, logs, and media files."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class ArtifactWriter:
    """Writes structured artifacts for a single scenario run.

    :param root: Base artifacts directory.
    :param run_id: Optional stable run identifier.
    """

    root: Path
    run_id: str | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.run_id is None:
            self.run_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_dir(self) -> Path:
        """Directory containing artifacts for this run."""

        return self.root / str(self.run_id)

    def path_for(self, kind: str, name: str, suffix: str) -> Path:
        """Return a stable artifact path and ensure its parent exists.

        :param kind: Artifact class, such as ``media`` or ``logs``.
        :param name: Logical artifact name.
        :param suffix: File suffix including the dot.
        :returns: Absolute or workspace-relative path under the run directory.
        """

        safe_name = sanitize_filename(name)
        directory = self.run_dir / sanitize_filename(kind)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_name}{suffix}"

    def record_event(self, event: str, **data: Any) -> None:
        """Append a structured event to the run timeline.

        :param event: Event type.
        :param data: Event details.
        """

        self.timeline.append(
            {
                "ts": datetime.now(tz=UTC).isoformat(),
                "event": event,
                **data,
            }
        )

    def copy_artifact(self, source: Path, kind: str, name: str) -> Path:
        """Copy an existing file into the run artifact directory.

        :param source: File to copy.
        :param kind: Artifact class.
        :param name: Logical artifact name.
        :returns: Destination path.
        """

        destination = self.path_for(kind, name, source.suffix)
        shutil.copy2(source, destination)
        return destination

    def finalize(self) -> Path:
        """Write the timeline JSON file.

        :returns: Timeline file path.
        """

        path = self.path_for("logs", "timeline", ".json")
        path.write_text(json.dumps(self.timeline, indent=2, sort_keys=True), encoding="utf-8")
        return path


def sanitize_filename(value: str) -> str:
    """Convert a logical name into a filesystem-friendly filename fragment.

    :param value: Source value.
    :returns: Sanitized filename fragment.
    """

    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    sanitized = sanitized.strip("-._")
    return sanitized or "artifact"
