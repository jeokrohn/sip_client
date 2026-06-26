from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from wxcalls.exceptions import MediaError
from wxcalls.media import create_silence_wav, detect_marker, generate_marker_tone, inject_marker, marker_frequency


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


def test_detect_marker_rejects_near_silent_tone(tmp_path: Path) -> None:
    """Verify marker detection requires a meaningful signal floor.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    marker = "marker-quiet"
    wav_path = tmp_path / "quiet-marker.wav"
    sample_rate = 16_000
    frequency = marker_frequency(marker)
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(int(sample_rate * 0.5)):
            sample = int(4 * math.sin(2 * math.pi * frequency * index / sample_rate))
            wav.writeframesraw(struct.pack("<h", sample))

    assert not detect_marker(wav_path, marker)


def test_detect_marker_rejects_low_level_frequency_leakage(tmp_path: Path) -> None:
    """Verify incidental marker-frequency energy is not accepted as a marker.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    marker = "marker-leakage"
    wav_path = tmp_path / "leakage.wav"
    sample_rate = 16_000
    marker_tone = marker_frequency(marker)
    dominant_tone = 3_000
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(int(sample_rate * 2.0)):
            dominant_sample = 9_000 * math.sin(2 * math.pi * dominant_tone * index / sample_rate)
            leakage_sample = 900 * math.sin(2 * math.pi * marker_tone * index / sample_rate)
            wav.writeframesraw(struct.pack("<h", int(dominant_sample + leakage_sample)))

    assert not detect_marker(wav_path, marker)


def test_detect_marker_rejects_unreadable_wav(tmp_path: Path) -> None:
    """Verify unreadable marker input raises a framework media error.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    path = tmp_path / "not-a-wave.wav"
    path.write_bytes(b"not a wave")

    with pytest.raises(MediaError, match="Unable to read WAV"):
        detect_marker(path, "marker-one")
