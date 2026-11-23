import itertools
import json
import statistics
import subprocess
from pathlib import Path
from typing import Any

import pytest

from infonium import recording
from infonium.document import UnreadableSource

pytestmark = pytest.mark.ffmpeg

SECONDS = 3.0
# How far the length of the kept sound may stray from the sound made: a coder pads its last
# frame, of 1024 samples for AAC, under 0.03 s.
LENGTH_TOLERANCE = 0.1
SOUND = f"sine=frequency=440:sample_rate=44100:duration={SECONDS}"
PICTURE = f"testsrc=size=64x48:rate=5:duration={SECONDS}"
REFUSED_WAV = "infonium cannot read this WAV file"
# Sound whose packets jump two seconds ahead after its 60th frame.
GAP = "asetpts='if(gte(N,60),PTS+2/TB,PTS)'"
# Each recording as FFmpeg makes it, and how long its sound runs from its first packet to its
# last: a gap in the times of its packets is kept as silence.
RECORDINGS = [
    ("sound.m4a", ("-c:a", "aac"), SECONDS),
    ("sound.mp3", ("-c:a", "libmp3lame"), SECONDS),
    ("sound.ogg", ("-c:a", "libvorbis"), SECONDS),
    ("sound.opus", ("-c:a", "libopus"), SECONDS),
    ("sound.flac", ("-c:a", "flac"), SECONDS),
    ("sound.wav", ("-c:a", "pcm_s16le"), SECONDS),
    ("mp3.wav", ("-c:a", "libmp3lame", "-f", "wav"), SECONDS),
    ("late.m4a", ("-af", "asetpts=PTS+2/TB", "-c:a", "aac"), SECONDS),
    ("gap.m4a", ("-af", GAP, "-c:a", "aac"), SECONDS + 2),
    ("gap.mka", ("-af", GAP, "-c:a", "aac"), SECONDS + 2),
]


def made(folder: Path, name: str, *output: str, inputs: tuple[str, ...] = (SOUND,)) -> Path:
    # A recording FFmpeg makes from its own test sources, with the options given for its output.
    file = folder / name
    sources = [option for source in inputs for option in ("-f", "lavfi", "-i", source)]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *sources, *output, str(file)], check=True)
    return file


def rewritten(source: Path, name: str, *output: str) -> Path:
    file = source.with_name(name)
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(source), *output, str(file)]
    subprocess.run(command, check=True)
    return file


def kept_sound(file: Path) -> dict[str, Any]:
    # The one stream of the kept sound, how long it lasts, and the steps between its packets.
    shown = subprocess.run(
        [
            "ffprobe", "-v", "error", "-of", "json", "-show_entries",
            "format=duration:stream=codec_type,codec_name,profile,start_time:packet=dts_time",
            str(file),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    report = json.loads(shown.stdout)
    [stream] = report["streams"]
    times = [float(packet["dts_time"]) for packet in report["packets"]]
    steps = [later - earlier for earlier, later in itertools.pairwise(times)]
    return {**stream, "duration": float(report["format"]["duration"]), "steps": steps}


@pytest.mark.parametrize(("name", "output", "runs"), RECORDINGS)
def test_the_sound_of_a_recording_is_kept_whole_from_zero_without_a_break(
    tmp_path: Path, name: str, output: tuple[str, ...], runs: float
) -> None:
    audio = recording.audio_track(made(tmp_path, name, *output), tmp_path)
    sound = kept_sound(audio.file)
    assert (sound["codec_type"], sound["codec_name"]) == ("audio", "aac"), (
        f"the sound kept of {name} is not AAC alone: {sound['codec_name']}"
    )
    assert sound["profile"] in recording.PLAYED_PROFILES, f"browsers do not play {sound}"
    assert float(sound["start_time"]) == 0, f"the sound kept of {name} does not start at zero"
    assert max(sound["steps"]) <= 2 * statistics.median(sound["steps"]), (
        f"the sound kept of {name} breaks off for {max(sound['steps']):.3f} s"
    )
    assert abs(sound["duration"] - runs) <= LENGTH_TOLERANCE, (
        f"the sound kept of {name} lasts {sound['duration']} s, where it runs {runs} s"
    )
    assert abs(recording.audio_length(audio) - runs) <= LENGTH_TOLERANCE, (
        "the length infonium reads of the kept sound is not the sound's"
    )


def test_of_a_video_only_the_sound_is_kept(tmp_path: Path) -> None:
    given = made(tmp_path, "talk.mp4", "-c:v", "mpeg4", "-c:a", "aac", inputs=(PICTURE, SOUND))
    audio = recording.audio_track(given, tmp_path)
    assert kept_sound(audio.file)["codec_type"] == "audio", "a video's picture was kept"


@pytest.mark.parametrize(("source", "coded"), [("sound.ogg", "libvorbis"), ("sound.m4a", "aac")])
def test_a_wav_whose_sound_does_not_mark_its_frames_is_refused(
    tmp_path: Path, source: str, coded: str
) -> None:
    given = made(tmp_path, source, "-c:a", coded)
    wav = rewritten(given, "kept.wav", "-c:a", "copy", "-f", "wav")
    with pytest.raises(UnreadableSource) as raised:
        recording.audio_track(wav, tmp_path)
    assert str(raised.value).startswith(REFUSED_WAV), (
        f"a WAV of {coded} sound, which marks no frames, was not refused saying why"
    )


@pytest.mark.parametrize(
    ("name", "output", "inputs"),
    [
        ("picture.mp4", ("-c:v", "mpeg4"), (PICTURE,)),
        ("unsounded.m4a", ("-c:a", "aac", "-frames:a", "0"), (SOUND,)),
    ],
)
def test_a_file_without_sound_is_refused(
    tmp_path: Path, name: str, output: tuple[str, ...], inputs: tuple[str, ...]
) -> None:
    silent = made(tmp_path, name, *output, inputs=inputs)
    with pytest.raises(UnreadableSource) as raised:
        recording.audio_track(silent, tmp_path)
    assert str(raised.value) == "no sound in the recording", f"wrongly refused: {raised.value}"


def test_a_recording_is_called_by_its_own_title(tmp_path: Path) -> None:
    titled = made(tmp_path, "sound.m4a", "-c:a", "aac", "-metadata", "title=Night trains")
    assert recording.own_title(titled) == "Night trains", "the recording's own title was lost"
