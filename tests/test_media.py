from __future__ import annotations

from pathlib import Path

import pytest

from wxcalls.exceptions import MediaError
from wxcalls.media import create_silence_wav, detect_marker, generate_marker_tone, inject_marker


def test_marker_tone_is_detected(tmp_path: Path) -> None:
    """Verify generated marker tones are detectable by marker name.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    wav_path = tmp_path / "marker.wav"

    generate_marker_tone("marker-one", wav_path)

    assert detect_marker(wav_path, "marker-one")
    assert not detect_marker(wav_path, "different-marker")


def test_marker_injection_appends_detectable_tone(tmp_path: Path) -> None:
    """Verify marker injection adds a detectable tone to a copy.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    source = tmp_path / "source.wav"
    marked = tmp_path / "marked.wav"
    create_silence_wav(source, seconds=0.2)

    inject_marker(source, marked, "marker-two")

    assert detect_marker(marked, "marker-two")
    assert not detect_marker(source, "marker-two")


def test_detect_marker_rejects_unreadable_wav(tmp_path: Path) -> None:
    """Verify unreadable marker input raises a framework media error.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    path = tmp_path / "not-a-wave.wav"
    path.write_bytes(b"not a wave")

    with pytest.raises(MediaError, match="Unable to read WAV"):
        detect_marker(path, "marker-one")
