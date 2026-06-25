from __future__ import annotations

from pathlib import Path

from wxcalls.media import create_silence_wav, detect_marker, generate_marker_tone, inject_marker


def test_marker_tone_is_detected(tmp_path: Path) -> None:
    wav_path = tmp_path / "marker.wav"

    generate_marker_tone("marker-one", wav_path)

    assert detect_marker(wav_path, "marker-one")
    assert not detect_marker(wav_path, "different-marker")


def test_marker_injection_appends_detectable_tone(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    marked = tmp_path / "marked.wav"
    create_silence_wav(source, seconds=0.2)

    inject_marker(source, marked, "marker-two")

    assert detect_marker(marked, "marker-two")
    assert not detect_marker(source, "marker-two")
