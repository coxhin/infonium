import subprocess
from pathlib import Path
from statistics import median_low
from typing import Annotated

from pydantic import BaseModel, Field

from infonium.document import WORD_GAP, MediaType, Seconds, UnreadableSource

# AAC in an MP4 file: Chrome, Firefox and Safari play it in an audio element, its index of
# samples leads a seek to the exact frame, and ffmpeg decodes it for a speech recogniser.
# Most sites already serve this coding, so their sound is kept as it is.
AUDIO_MEDIA_TYPE = "audio/mp4"
AUDIO_FILE = "sound.m4a"
AAC = "aac"

# A file that is taken for a recording may be anything, and ffmpeg and ffprobe do what a
# file tells them. So they are pointed at one in a single way. The file stands on their
# standard input, so no name of it reaches them to be read as an option or an address. They
# open nothing but files of this machine. And they read only the containers of a recording,
# each by the name of ffmpeg's reader for it: the kinds of audio and video that a file's
# bytes can be told to be. A file of another kind may be a script that sends its reader to
# other files.
STANDARD_INPUT = "/dev/stdin"
LOCAL_FILE = "file"
OGG = "ogg"
RECORDING_FORMATS = f"aac,aiff,amr,asf,avi,flac,flv,matroska,mov,mp3,mpeg,{OGG},wav"
HELD_TO_RECORDINGS = ["-protocol_whitelist", LOCAL_FILE, "-format_whitelist", RECORDING_FORMATS]
# The sound of a file with several is its first.
FIRST_SOUND = "a:0"
# ffprobe answers from the index of a file, and reads no further into the sound than its
# first sixteen packets: enough to tell how long a packet usually lasts, whatever the first
# one does.
FIRST_PACKETS = "%+#16"
# ffprobe ends with this status when it cannot read a file. Its report then says why without
# the name the file stands under, which the line it writes of the file begins with.
UNREADABLE_STATUS = 1
FFPROBE = [
    "ffprobe",
    "-v",
    "error",
    *HELD_TO_RECORDINGS,
    "-show_error",
    "-of",
    "json",
    "-select_streams",
    FIRST_SOUND,
    "-read_intervals",
    FIRST_PACKETS,
    "-show_entries",
]
SOUND_ENTRIES = "stream=codec_name:packet=size"
TIMELINE_ENTRIES = "stream=start_pts,duration_ts,nb_frames:packet=duration"
LENGTH_ENTRIES = "format=duration"
TITLE_ENTRIES = "format=format_name:format_tags=title:stream_tags=title"
# ffmpeg reading the first sound of the recording on its standard input; what it is to make
# of that sound follows.
FFMPEG = [
    "ffmpeg",
    "-nostdin",
    "-v",
    "error",
    *HELD_TO_RECORDINGS,
    "-i",
    STANDARD_INPUT,
    "-map",
    f"0:{FIRST_SOUND}",
]
# The sound is written alone: without the chapters of its file, which would be a stream of
# their own, and with the index of samples before the sound, so that a player need not read
# to the end of the file before it can play or seek.
SOUND_ALONE = [*FFMPEG, "-y", "-map_chapters", "-1", "-movflags", "+faststart"]
# The timeline of a recording is the one its picture and its captions keep: where the sound
# starts late or breaks off, silence stands in the gap. Players and recognisers do not agree
# about a file that tells this only by the times of its packets, so the sound that is kept
# tells it in samples: it starts at zero and runs without a break. Sound in the coding that
# is kept is copied when the copy does so; other sound is coded anew through this filter,
# which puts the silence in.
ON_THE_TIMELINE = "aresample=async=1:first_pts=0"
COPIED = ["-c:a", "copy"]
CODED_ANEW = ["-c:a", AAC, "-af", ON_THE_TIMELINE]
# A coder leaves out of the length of a track what it trims from its ends, at most this
# many packets. A track that is shorter than its packets by more has packets that overlap,
# and one that is longer by a packet has packets that stand apart.
TRIMMED_PACKETS = 5
# The longest ffprobe may take over a file, and ffmpeg over writing its sound.
PROBE_LIMIT_SECONDS = 60.0
CONVERSION_LIMIT_SECONDS = 1800.0


class ProbeError(BaseModel):
    string: str


class FailedProbe(BaseModel):
    # What ffprobe reports of a file it cannot read.
    error: ProbeError


class SoundStream(BaseModel):
    codec_name: str


class SoundPacket(BaseModel):
    size: int


class SoundProbe(BaseModel):
    # The first sound of a file and the first packets of that sound, where the file has them.
    streams: list[SoundStream]
    packets: list[SoundPacket]


class CopiedStream(BaseModel):
    # What the index of a copy tells of its track, in ticks of the track's clock: when it
    # starts, how long it runs, and how many packets it holds.
    start_pts: int
    duration_ts: int
    nb_frames: int


class CopiedPacket(BaseModel):
    duration: int


class CopyProbe(BaseModel):
    # A copy of sound holds that sound: one track, and packets in it.
    streams: Annotated[list[CopiedStream], Field(min_length=1)]
    packets: Annotated[list[CopiedPacket], Field(min_length=1)]


class FileLength(BaseModel):
    duration: Seconds


class LengthProbe(BaseModel):
    format: FileLength


class StreamTags(BaseModel):
    tags: dict[str, str] = {}


class FileTags(BaseModel):
    # The tags of a file that were asked for, each under the name its kind of file gives it,
    # and ffmpeg's reader for that kind.
    format_name: str
    tags: dict[str, str] = {}


class TagProbe(BaseModel):
    # The tags of a file, and those of its first sound where it has one.
    format: FileTags
    streams: list[StreamTags]


class AudioTrack(BaseModel):
    file: Path
    media_type: MediaType


# What one of the programs does when it is run on a recording, within a time.
def run_on(file: Path, command: list[str], limit: float) -> subprocess.CompletedProcess[bytes]:
    with file.open("rb") as recording:
        return subprocess.run(command, stdin=recording, capture_output=True, timeout=limit)


# A file that ffprobe cannot read is a source with nothing to read. An ffprobe that ends
# without an answer for any other reason fails as a program.
def probed[Report: BaseModel](file: Path, entries: str, report: type[Report]) -> Report:
    done = run_on(file, [*FFPROBE, entries, STANDARD_INPUT], PROBE_LIMIT_SECONDS)
    if done.returncode == UNREADABLE_STATUS:
        failure = FailedProbe.model_validate_json(done.stdout).error.string
        raise UnreadableSource(f"infonium cannot read the recording: {failure}")
    done.check_returncode()
    return report.model_validate_json(done.stdout)


# The title a recording carries in the tags of its file, and no words where it carries none.
# An Ogg file carries its tags in the comments of its sound. In a file of another kind, a
# title on the sound names its track, such as "English".
def own_title(file: Path) -> str:
    probe = probed(file, TITLE_ENTRIES, TagProbe)
    commented = probe.streams if probe.format.format_name == OGG else []
    titles = [
        *probe.format.tags.values(),
        *(title for sound in commented for title in sound.tags.values()),
    ]
    return WORD_GAP.join("".join(titles).split())


# Whether a copy starts at zero and runs as long as its packets last.
def has_plain_timeline(copy: CopyProbe) -> bool:
    stream = copy.streams[0]
    usual = median_low(packet.duration for packet in copy.packets)
    longer = stream.duration_ts - stream.nb_frames * usual
    return stream.start_pts == 0 and -TRIMMED_PACKETS * usual <= longer <= usual


def audio_track(file: Path, directory: Path) -> AudioTrack:
    probe = probed(file, SOUND_ENTRIES, SoundProbe)
    if not probe.streams or not probe.packets:
        raise UnreadableSource("no sound in the recording")
    audio = AudioTrack(file=directory / AUDIO_FILE, media_type=AUDIO_MEDIA_TYPE)
    # Named as a file, the place the sound is written to cannot be read as anything else.
    sound = f"{LOCAL_FILE}:{audio.file}"
    if probe.streams[0].codec_name == AAC:
        run_on(file, [*SOUND_ALONE, *COPIED, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
        if has_plain_timeline(probed(audio.file, TIMELINE_ENTRIES, CopyProbe)):
            return audio
    run_on(file, [*SOUND_ALONE, *CODED_ANEW, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
    return audio
