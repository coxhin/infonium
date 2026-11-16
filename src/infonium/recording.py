import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from statistics import median_low
from typing import IO, Annotated

from pydantic import BaseModel, Field

from infonium.document import WORD_GAP, MediaType, Seconds, UnreadableSource

# AAC in an MP4 file: Chrome, Firefox and Safari play it in an audio element, its index of
# samples leads a seek to the exact frame, and ffmpeg decodes it for a speech recogniser.
# Most sites already serve this coding, so their sound is kept as it is.
AUDIO_MEDIA_TYPE = "audio/mp4"
AUDIO_FILE = "sound.m4a"
AAC = "aac"
# The profiles of AAC that every one of those browsers plays, by the names ffprobe gives
# them. Of the others, Firefox plays LD as silence and ELD not at all.
PLAYED_PROFILES = frozenset({"LC", "HE-AAC", "HE-AACv2"})

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
# ffprobe reading the first sound of the recording on its standard input; what it is asked
# of that sound follows.
FFPROBE = [
    "ffprobe",
    "-v",
    "error",
    *HELD_TO_RECORDINGS,
    "-i",
    STANDARD_INPUT,
    "-select_streams",
    FIRST_SOUND,
]
# A report is answered from the index of a file, and from no more of the sound than its
# first sixteen packets: enough to show that the sound holds any and, where they are
# decoded, how much sound a packet holds, whatever is trimmed from the first.
FIRST_PACKETS = "%+#16"
REPORT_OF = ["-show_error", "-of", "json", "-read_intervals", FIRST_PACKETS, "-show_entries"]
# ffprobe ends with this status when it cannot read a file. Its report then says why without
# the name the file stands under, which the line it writes of the file begins with.
UNREADABLE_STATUS = 1
SOUND_ENTRIES = "stream=codec_name,profile:packet=size"
COPY_ENTRIES = "stream=start_pts,time_base,sample_rate:frame=nb_samples"
LENGTH_ENTRIES = "format=duration"
TITLE_ENTRIES = "format=format_name:format_tags=title:stream_tags=title"
# The time of every packet of the sound, in ticks of its track's clock, one to a line. Read
# as a report into models, the packets of four hours hold half a gigabyte.
PACKET_TIMES = ["-of", "default=nokey=1:noprint_wrappers=1", "-show_entries", "packet=dts"]
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
# ffmpeg begins a line it writes with the part of itself that writes it and where that part
# lies in its memory, as in "[aac @ 0x5581d0a0] ", which tells a person nothing.
COMPLAINER = re.compile(r"^(?:\[[^\[\]]* @ (?:0x)?[0-9a-f]+\] )+")
# The sound is written alone: without the chapters of its file, which would be a stream of
# their own, and with the index of samples before the sound, so that a player need not read
# to the end of the file before it can play or seek.
SOUND_ALONE = [*FFMPEG, "-y", "-map_chapters", "-1", "-movflags", "+faststart"]
# The timeline of a recording is the one its picture and its captions keep: where the sound
# starts late or breaks off, silence stands in the gap. Sound that is coded anew goes through
# this filter, which puts that silence in.
ON_THE_TIMELINE = "aresample=async=1:first_pts=0"
COPIED = ["-c:a", "copy"]
CODED_ANEW = ["-c:a", AAC, "-af", ON_THE_TIMELINE]
# The sound is decoded and nothing is written. Told so, ffmpeg ends with a failing status
# at the first packet it cannot decode; otherwise it goes on without the packet.
DECODED_FOR_NOTHING = ["-xerror", "-f", "null", "-"]
# The longest ffprobe may take over a file, and ffmpeg over decoding or writing its sound.
PROBE_LIMIT_SECONDS = 60.0
CONVERSION_LIMIT_SECONDS = 1800.0


class ProbeError(BaseModel):
    string: str


class FailedProbe(BaseModel):
    # What ffprobe reports of a file it cannot read.
    error: ProbeError


class SoundStream(BaseModel):
    # The coding of a sound and the profile of that coding. ffprobe leaves unnamed what
    # ffmpeg does not know.
    codec_name: str | None = None
    profile: str | None = None


class SoundPacket(BaseModel):
    size: int


class SoundProbe(BaseModel):
    # The first sound of a file and the first packets of that sound, where the file has them.
    streams: list[SoundStream]
    packets: list[SoundPacket]


class CopiedStream(BaseModel):
    # What the index of a copy tells of its track: when it starts, in ticks of the track's
    # clock, how long a tick lasts in seconds, and how many samples a second the sound has.
    start_pts: int
    time_base: Fraction
    sample_rate: int


class DecodedPacket(BaseModel):
    # How many samples a packet holds once it is decoded.
    nb_samples: int


class CopyProbe(BaseModel):
    # A copy of sound holds that sound: one track. Its first packets are decoded, those of
    # them that can be.
    streams: Annotated[list[CopiedStream], Field(min_length=1)]
    frames: list[DecodedPacket]


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
    # The sound that is kept of a recording. audio_track makes it and vouches for it: its
    # coding is one that browsers play, and it starts at zero, runs without a break and
    # decodes whole, so a count of its samples is a time from its start. Players and
    # recognisers do not agree about a file that tells a late start or a break only by the
    # times of its packets, and at a packet they cannot decode one stops and another goes on
    # without it.
    file: Path
    media_type: MediaType


# One of the programs at work on a recording, which is handed to it as its standard input.
# Started in any other way, it would read whatever this process has for a standard input.
# The program is ended when its user is done with it.
@contextmanager
def started_on(
    file: Path, command: list[str], stdout: int | IO[bytes], stderr: int | IO[bytes]
) -> Iterator[subprocess.Popen[bytes]]:
    with (
        file.open("rb") as recording,
        subprocess.Popen(command, stdin=recording, stdout=stdout, stderr=stderr) as program,
    ):
        try:
            yield program
        finally:
            program.kill()


# What one of the programs does when it is run on a recording, within a time.
def run_on(file: Path, command: list[str], limit: float) -> subprocess.CompletedProcess[bytes]:
    with started_on(file, command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as program:
        written, complaints = program.communicate(timeout=limit)
    return subprocess.CompletedProcess(command, program.returncode, written, complaints)


# A file that ffprobe cannot read is a source with nothing to read. An ffprobe that ends
# without an answer for any other reason fails as a program.
def probed[Report: BaseModel](file: Path, entries: str, report: type[Report]) -> Report:
    done = run_on(file, [*FFPROBE, *REPORT_OF, entries], PROBE_LIMIT_SECONDS)
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


def packet_times(file: Path) -> list[int]:
    listed = run_on(file, [*FFPROBE, *PACKET_TIMES], PROBE_LIMIT_SECONDS)
    listed.check_returncode()
    return [int(time) for time in listed.stdout.split()]


# Whether a copy starts at zero and runs without a break: each of its packets stands within
# one packet's length of where its number puts it after the first, a packet lasting as long
# as the sound it holds. A track whose times jump ahead and later fall back does not run so,
# though it lasts as long as its packets do; nor does one whose packets stand evenly, but
# further apart than they last.
def has_plain_timeline(copy: CopyProbe, times: list[int]) -> bool:
    track = copy.streams[0]
    held = [packet.nb_samples for packet in copy.frames]
    # Where none of the first packets can be decoded, how long a packet lasts is not known.
    if not held:
        return False
    length = float(median_low(held) / (track.sample_rate * track.time_base))
    return track.start_pts == 0 and all(
        abs(time - times[0] - number * length) <= length for number, time in enumerate(times)
    )


def decodes_whole(file: Path) -> bool:
    decoding = run_on(file, [*FFMPEG, *DECODED_FOR_NOTHING], CONVERSION_LIMIT_SECONDS)
    return decoding.returncode == 0


def audio_track(file: Path, directory: Path) -> AudioTrack:
    probe = probed(file, SOUND_ENTRIES, SoundProbe)
    if not probe.streams or not probe.packets:
        raise UnreadableSource("no sound in the recording")
    coded = probe.streams[0]
    if coded.codec_name is None:
        raise UnreadableSource("no sound infonium can read in the recording")
    audio = AudioTrack(file=directory / AUDIO_FILE, media_type=AUDIO_MEDIA_TYPE)
    # Named as a file, the place the sound is written to cannot be read as anything else.
    sound = f"{LOCAL_FILE}:{audio.file}"
    # A copy is kept only where it is all that the kept sound must be. Sound coded anew is
    # that by the way it is made: the filter sets it on the timeline, and the coder writes
    # no packet that cannot be decoded, in a profile that is played.
    if coded.codec_name == AAC and coded.profile in PLAYED_PROFILES:
        run_on(file, [*SOUND_ALONE, *COPIED, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
        copy = probed(audio.file, COPY_ENTRIES, CopyProbe)
        if has_plain_timeline(copy, packet_times(audio.file)) and decodes_whole(audio.file):
            return audio
    run_on(file, [*SOUND_ALONE, *CODED_ANEW, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
    return audio
