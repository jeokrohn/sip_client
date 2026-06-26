"""Media generation, TTS synthesis, tone markers, and WAV analysis."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from wxcalls.exceptions import MediaError

DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_CHANNELS = 1


@dataclass(frozen=True)
class MediaAsset:
    """Prepared audio media ready for playback.

    :param path: WAV file path.
    :param marker: Optional deterministic marker embedded in the audio.
    """

    path: Path
    marker: str | None = None


class MediaFactory:
    """Creates media assets for scenario playback."""

    def __init__(self, work_dir: Path, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        """Create a media factory.

        :param work_dir: Directory for generated media.
        :param sample_rate: Output sample rate for generated WAV assets.
        :returns: None.
        """

        self.work_dir = work_dir
        self.sample_rate = sample_rate
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare_tts(self, text: str, marker: str | None = None, voice: str | None = None) -> MediaAsset:
        """Generate runtime TTS audio and optionally append a marker tone.

        :param text: Text to synthesize with macOS ``say``.
        :param marker: Optional marker identifier.
        :param voice: Optional macOS voice name.
        :returns: Generated WAV media asset.
        """

        name = f"tts-{abs(hash((text, marker, voice))) & 0xFFFFFFFF:x}.wav"
        output = self.work_dir / name
        generate_tts_wav(text=text, output_path=output, marker=marker, voice=voice)
        return MediaAsset(path=output, marker=marker)

    def prepare_wav(self, source: Path, marker: str | None = None) -> MediaAsset:
        """Prepare an existing WAV file, optionally adding a marker tone copy.

        :param source: Source WAV file.
        :param marker: Optional marker identifier to append.
        :returns: Prepared media asset.
        """

        if marker is None:
            return MediaAsset(path=source)
        output = self.work_dir / f"{source.stem}-marked.wav"
        inject_marker(source, output, marker)
        return MediaAsset(path=output, marker=marker)


def generate_tts_wav(
    text: str,
    output_path: Path,
    marker: str | None = None,
    voice: str | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> None:
    """Generate speech with macOS ``say`` and convert it to mono PCM WAV.

    :param text: Text to synthesize.
    :param output_path: Destination WAV path.
    :param marker: Optional marker identifier to append.
    :param voice: Optional macOS voice name.
    :param sample_rate: Output sample rate.
    :returns: None.
    :raises MediaError: If required macOS tools are unavailable or synthesis fails.
    """

    say_path = shutil.which("say")
    afconvert_path = shutil.which("afconvert")
    if say_path is None:
        raise MediaError("macOS 'say' command is required for runtime TTS")
    if afconvert_path is None:
        raise MediaError("macOS 'afconvert' command is required to create PCM WAV TTS assets")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        aiff_path = Path(tmp) / "speech.aiff"
        speech_wav = Path(tmp) / "speech.wav"
        # macOS ``say`` writes AIFF reliably, then ``afconvert`` normalizes it to
        # the mono PCM format expected by the SIP backends and marker detector.
        say_cmd = [say_path, "-o", str(aiff_path)]
        if voice:
            say_cmd.extend(["-v", voice])
        say_cmd.append(text)
        _run_media_command(say_cmd, "TTS synthesis failed")

        convert_cmd = [
            afconvert_path,
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{sample_rate}",
            "-c",
            "1",
            str(aiff_path),
            str(speech_wav),
        ]
        _run_media_command(convert_cmd, "TTS WAV conversion failed")

        if marker:
            inject_marker(speech_wav, output_path, marker, sample_rate=sample_rate)
        else:
            shutil.copy2(speech_wav, output_path)


def generate_marker_tone(
    marker: str,
    output_path: Path,
    duration: float = 0.75,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    amplitude: float = 0.45,
) -> None:
    """Generate a deterministic single-frequency marker tone as PCM WAV.

    :param marker: Marker identifier.
    :param output_path: Destination WAV path.
    :param duration: Tone duration in seconds.
    :param sample_rate: Output sample rate.
    :param amplitude: Tone amplitude from 0.0 to 1.0.
    :returns: None.
    """

    frequency = marker_frequency(marker)
    frame_count = int(duration * sample_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(DEFAULT_CHANNELS)
        wav.setsampwidth(DEFAULT_SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        for index in range(frame_count):
            sample = int(32767 * amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
            wav.writeframesraw(struct.pack("<h", sample))


def create_silence_wav(output_path: Path, seconds: float, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
    """Create a mono PCM silence WAV file.

    :param output_path: Destination WAV path.
    :param seconds: Duration in seconds.
    :param sample_rate: Output sample rate.
    :returns: None.
    """

    frame_count = int(seconds * sample_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(DEFAULT_CHANNELS)
        wav.setsampwidth(DEFAULT_SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frame_count)


def inject_marker(
    source_path: Path,
    output_path: Path,
    marker: str,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> None:
    """Append a deterministic marker tone to an existing WAV file.

    :param source_path: Source WAV file.
    :param output_path: Destination WAV file.
    :param marker: Marker identifier.
    :param sample_rate: Expected output sample rate for generated marker.
    :returns: None.
    :raises MediaError: If source WAV parameters are unsupported.
    """

    with tempfile.TemporaryDirectory() as tmp:
        marker_path = Path(tmp) / "marker.wav"
        generate_marker_tone(marker, marker_path, sample_rate=sample_rate)
        concatenate_wavs([source_path, marker_path], output_path)


def concatenate_wavs(paths: list[Path], output_path: Path) -> None:
    """Concatenate compatible mono PCM WAV files.

    :param paths: Source WAV files in output order.
    :param output_path: Destination WAV file.
    :returns: None.
    :raises MediaError: If input files do not share compatible parameters.
    """

    if not paths:
        raise MediaError("At least one WAV file is required")
    # Preserve the first file's exact WAV parameters so generated and supplied media
    # can be concatenated without changing timing or sample format.
    params = None
    frames: list[bytes] = []
    for path in paths:
        with wave.open(str(path), "rb") as wav:
            current = wav.getparams()
            if current.nchannels != DEFAULT_CHANNELS or current.sampwidth != DEFAULT_SAMPLE_WIDTH:
                raise MediaError(f"Unsupported WAV format for {path}: expected mono 16-bit PCM")
            if params is None:
                params = current
            elif current[:3] != params[:3]:
                raise MediaError("Cannot concatenate WAV files with different channels/rate/width")
            frames.append(wav.readframes(wav.getnframes()))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(b"".join(frames))


def detect_marker(
    path: Path,
    marker: str,
    threshold: float = 0.35,
    minimum_peak: int = 256,
    window_seconds: float = 0.25,
    minimum_duration: float = 0.35,
) -> bool:
    """Detect whether a marker tone is present in a WAV recording.

    :param path: WAV file to analyze.
    :param marker: Marker identifier.
    :param threshold: Minimum normalized tone energy ratio in an analysis window.
    :param minimum_peak: Minimum absolute sample value required before frequency detection.
    :param window_seconds: Duration of each analysis window.
    :param minimum_duration: Minimum sustained marker-like duration.
    :returns: ``True`` if the marker frequency is detected.
    :raises MediaError: If the WAV file cannot be read.
    """

    samples, sample_rate = _read_mono_int16(path)
    if not samples:
        return False
    if max(abs(sample) for sample in samples) < minimum_peak:
        return False
    frequency = marker_frequency(marker)
    return _has_sustained_marker_tone(
        samples=samples,
        sample_rate=sample_rate,
        frequency=frequency,
        threshold=threshold,
        minimum_peak=minimum_peak,
        window_seconds=window_seconds,
        minimum_duration=minimum_duration,
    )


def marker_frequency(marker: str) -> int:
    """Map a marker string to a deterministic audible test frequency.

    :param marker: Marker identifier.
    :returns: Frequency in hertz.
    """

    checksum = sum((index + 1) * ord(char) for index, char in enumerate(marker))
    return 900 + (checksum % 700)


def _read_mono_int16(path: Path) -> tuple[list[int], int]:
    """Read a mono 16-bit PCM WAV into integer samples.

    :param path: WAV path to read.
    :returns: Samples and sample rate.
    :raises MediaError: If the WAV format is unsupported.
    """

    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != DEFAULT_CHANNELS or wav.getsampwidth() != DEFAULT_SAMPLE_WIDTH:
                raise MediaError(f"Unsupported WAV format for marker detection: {path}")
            frames = wav.readframes(wav.getnframes())
            sample_count = len(frames) // DEFAULT_SAMPLE_WIDTH
            samples = list(struct.unpack(f"<{sample_count}h", frames))
            return samples, wav.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise MediaError(f"Unable to read WAV for marker detection: {path}: {exc}") from exc


def _goertzel_power(samples: list[int], sample_rate: int, frequency: float) -> float:
    """Compute single-bin tone power with the Goertzel algorithm.

    :param samples: PCM samples.
    :param sample_rate: Sample rate in hertz.
    :param frequency: Frequency to measure in hertz.
    :returns: Relative tone power for the requested frequency.
    """

    normalized = frequency / sample_rate
    coefficient = 2 * math.cos(2 * math.pi * normalized)
    prev = 0.0
    prev2 = 0.0
    for sample in samples:
        value = sample + coefficient * prev - prev2
        prev2 = prev
        prev = value
    return prev2 * prev2 + prev * prev - coefficient * prev * prev2


def _has_sustained_marker_tone(
    samples: list[int],
    sample_rate: int,
    frequency: float,
    threshold: float,
    minimum_peak: int,
    window_seconds: float,
    minimum_duration: float,
) -> bool:
    """Return whether a marker tone dominates consecutive analysis windows.

    :param samples: PCM samples.
    :param sample_rate: Sample rate in hertz.
    :param frequency: Marker frequency to detect.
    :param threshold: Minimum normalized tone energy ratio.
    :param minimum_peak: Minimum absolute sample value for a candidate window.
    :param window_seconds: Duration of each analysis window.
    :param minimum_duration: Minimum sustained marker-like duration.
    :returns: ``True`` when a sustained marker tone is present.
    """

    window_size = max(1, min(len(samples), int(window_seconds * sample_rate)))
    hop_size = max(1, window_size // 2)
    required_hits = _required_consecutive_hits(
        window_size=window_size,
        hop_size=hop_size,
        sample_rate=sample_rate,
        minimum_duration=minimum_duration,
    )
    consecutive_hits = 0
    last_start = max(0, len(samples) - window_size)
    starts = list(range(0, last_start + 1, hop_size))
    if starts[-1] != last_start:
        starts.append(last_start)

    for start in starts:
        chunk = samples[start : start + window_size]
        if max(abs(sample) for sample in chunk) < minimum_peak:
            consecutive_hits = 0
            continue
        if _normalized_tone_energy_ratio(chunk, sample_rate, frequency) >= threshold:
            consecutive_hits += 1
            if consecutive_hits >= required_hits:
                return True
        else:
            consecutive_hits = 0
    return False


def _required_consecutive_hits(
    window_size: int,
    hop_size: int,
    sample_rate: int,
    minimum_duration: float,
) -> int:
    """Return the number of consecutive windows needed for a sustained marker.

    :param window_size: Analysis window length in samples.
    :param hop_size: Hop length between windows in samples.
    :param sample_rate: Sample rate in hertz.
    :param minimum_duration: Minimum sustained marker-like duration.
    :returns: Required consecutive candidate windows.
    """

    window_duration = window_size / sample_rate
    if minimum_duration <= window_duration:
        return 1
    hop_duration = hop_size / sample_rate
    return math.ceil((minimum_duration - window_duration) / hop_duration) + 1


def _normalized_tone_energy_ratio(samples: list[int], sample_rate: int, frequency: float) -> float:
    """Compute marker-bin energy normalized so a pure tone is close to ``1.0``.

    :param samples: PCM samples.
    :param sample_rate: Sample rate in hertz.
    :param frequency: Frequency to measure in hertz.
    :returns: Normalized tone energy ratio.
    """

    signal_power = sum(sample * sample for sample in samples)
    if signal_power <= 0:
        return 0.0
    tone_power = _goertzel_power(samples, sample_rate, frequency)
    return tone_power / (signal_power * (len(samples) / 2))


def _run_media_command(command: list[str], message: str) -> None:
    """Run an external media command and translate failures.

    :param command: Command argument vector.
    :param message: Error prefix to use when the command fails.
    :returns: None.
    :raises MediaError: If the command cannot run or exits non-zero.
    """

    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise MediaError(f"{message}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise MediaError(f"{message}: {stderr}")
