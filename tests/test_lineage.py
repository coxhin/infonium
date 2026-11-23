import ast
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from infonium import browser, lineage, media, mineru, recording
from infonium.settings import Settings

# The ways the standard library starts a program, by the module that offers them, and the
# methods of an event loop that do, whatever they are called on. Outside lineage no module of
# infonium calls one. The check reads calls by the names a module imports, so it does not see
# a program started through a starter kept in a variable or handed to another function
# (functools.partial, a callback), reached by getattr or importlib, or started by a library
# (yt-dlp, Playwright, Python's platform module), nor the processes multiprocessing starts.
PROGRAM_STARTERS = {
    "subprocess": re.compile(r"Popen|run|call|check_call|check_output|getoutput|getstatusoutput"),
    "os": re.compile(r"exec\w*|spawn\w*|posix_spawnp?|system|popen"),
    "asyncio": re.compile(r"create_subprocess_\w+"),
    "asyncio.subprocess": re.compile(r"create_subprocess_\w+"),
    "pty": re.compile(r"spawn"),
}
LOOP_STARTERS = re.compile(r"subprocess_exec|subprocess_shell")
EVERY_NAME = "*"
# A program the starters' test starts: it writes the environment it was started with, whole,
# to the file it is given.
WRITES_ITS_ENVIRONMENT = (
    "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1] + '.part', 'w')); "
    "os.replace(sys.argv[1] + '.part', sys.argv[1])"
)
STARTER_LIMIT_SECONDS = 30.0
STARTER_WATCH_SECONDS = 0.01

# Each program infonium starts, by the rule of what it takes of infonium's environment; and of
# the variables a person may set, those each program takes and some it does not.
RULES = {
    "the renderer": browser.RENDERER_TAKES,
    "the media fetcher": media.FETCHER_TAKES,
    "MinerU": mineru.ENGINE_TAKES,
    "FFmpeg": recording.FFMPEG_TAKES,
}
LOCALE = ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "TZ")
PROXIES = ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")
TAKEN = {
    "the renderer": ("PATH", *LOCALE),
    "the media fetcher": (
        "PATH", *LOCALE, *PROXIES, "YTDLP_NO_PLUGINS", "YTDLP_NO_LAZY_EXTRACTORS",
    ),
    "MinerU": (
        "PATH", *LOCALE, *PROXIES, "HOME", "XDG_CACHE_HOME", "LD_LIBRARY_PATH",
        "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "CUDA_CACHE_PATH", "MINERU_MODEL_SOURCE",
        "MINERU_TOOLS_CONFIG_JSON", "MINERU_VIRTUAL_VRAM_SIZE", "HF_HOME", "HF_HUB_CACHE",
        "HF_ENDPOINT", "HF_HUB_OFFLINE", "HUGGINGFACE_HUB_CACHE", "MODELSCOPE_CACHE",
    ),
    "FFmpeg": ("PATH",),
}  # fmt: skip
NOT_TAKEN = {
    "the renderer": (
        "HOME", "TMPDIR", "XDG_CONFIG_HOME", *PROXIES, "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES",
        "YTDLP_NO_PLUGINS", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    ),
    "the media fetcher": (
        "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "appdata", "SSL_CERT_FILE",
        "SSLKEYLOGFILE", "PYTHONPATH", "TERM", "NO_COLOR", "LD_LIBRARY_PATH",
        "CUDA_VISIBLE_DEVICES", "MINERU_MODEL_SOURCE",
    ),
    "MinerU": (
        "TMPDIR", "PYTHONPATH", "LD_PRELOAD", "TERM", "XDG_CONFIG_HOME", "SSL_CERT_FILE",
        "SSLKEYLOGFILE", "PYTORCH_CUDA_ALLOC_CONF", "TORCH_HOME", "TRITON_CACHE_DIR",
        "OMP_NUM_THREADS", "TRANSFORMERS_CACHE", "YTDLP_NO_PLUGINS", "DBUS_SESSION_BUS_ADDRESS",
    ),
    "FFmpeg": (
        "HOME", "TMPDIR", *LOCALE, *PROXIES, "FFREPORT", "AV_LOG_FORCE_COLOR", "FONTCONFIG_FILE",
        "SSLKEYLOGFILE", "XDG_CACHE_HOME", "YTDLP_NO_PLUGINS",
    ),
}  # fmt: skip
GIVEN_NAMES = ("HOME", "TMPDIR", "PATH", "MINERU_TASK_RESULT_TIMEOUT_SECONDS")
# Words by which a name says it holds a secret, and the starts of names that MinerU's rule
# takes, among which such a name may stand.
SECRET_WORDS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASS",
    "PWD",
    "CREDENTIALS",
    "AUTH",
)
TAKEN_FAMILIES = ("MINERU_", "CUDA_", "HF_", "HUGGINGFACE_", "MODELSCOPE_", "LC_")
# infonium's own settings, by the names Settings reads them under in either case, and those of
# the model client it holds.
SETTING_NAMES = [f"{Settings.model_config['env_prefix']}{field}" for field in Settings.model_fields]
MODEL_CLIENT_PREFIX = "OPENAI_"
MODEL_CLIENT_SETTINGS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_LOG", "AZURE_OPENAI_ENDPOINT")

values = st.text()
name_parts = st.text(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"), max_size=8)
secret_words = st.sampled_from(SECRET_WORDS).flatmap(
    lambda word: st.sampled_from([word, word.lower(), word.capitalize()])
)
# A name that only the secret it names should keep from a program: within a family that
# MinerU's rule takes, or the name of a proxy.
secret_names = st.one_of(
    st.builds(
        lambda family, before, word, after: f"{family}{before}{word}{after}",
        st.sampled_from(TAKEN_FAMILIES),
        name_parts,
        secret_words,
        name_parts,
    ),
    st.builds(lambda word: f"{word}_proxy", secret_words),
)
settings_of_infonium = st.one_of(
    st.sampled_from(SETTING_NAMES).flatmap(
        lambda name: st.sampled_from([name.upper(), name.lower()])
    ),
    st.sampled_from(MODEL_CLIENT_SETTINGS),
    st.builds(lambda name: f"{MODEL_CLIENT_PREFIX}{name}", name_parts),
)


@given(
    program=st.sampled_from(sorted(RULES)),
    data=st.data(),
    given_by_infonium=st.dictionaries(st.sampled_from(GIVEN_NAMES), values),
)
def test_a_program_is_given_what_it_takes_and_what_infonium_gives_it(
    program: str, data: st.DataObject, given_by_infonium: dict[str, str]
) -> None:
    names = [*TAKEN[program], *NOT_TAKEN[program]]
    environment = data.draw(st.fixed_dictionaries(dict.fromkeys(names, values)))
    received = lineage.program_environment(environment, RULES[program], given_by_infonium)
    expected = {**{name: environment[name] for name in TAKEN[program]}, **given_by_infonium}
    assert received == expected, (
        f"{program} was given {sorted(received)}, where it takes {sorted(TAKEN[program])} of "
        f"infonium's environment and infonium gives it {sorted(given_by_infonium)}"
    )


@given(
    secrets=st.lists(secret_names, min_size=1),
    settings=st.lists(settings_of_infonium, min_size=1),
    value=values,
)
def test_no_key_and_no_setting_of_infoniums_reaches_any_program(
    secrets: list[str], settings: list[str], value: str
) -> None:
    untaken = [name for name in secrets if not mineru.ENGINE_TAKES.fullmatch(name)]
    assert not untaken, (
        f"the test's names {untaken} are not taken by MinerU's rule, so they do not show that "
        "the secret they name alone keeps them out: the test's families are out of date"
    )
    environment = {"PATH": value, **dict.fromkeys([*secrets, *settings], value)}
    for program, taken in RULES.items():
        received = lineage.program_environment(environment, taken, {})
        reached = sorted(set(received) - {"PATH"})
        assert not reached, f"{program} was given {reached} of infonium's environment"
        assert "PATH" in received, f"{program} was not given the PATH it is found on"


class ProgramStarts(ast.NodeVisitor):
    # The places where a module calls a way to start a program, by line and by the dotted name
    # of what it calls, as the module's imports name it.
    def __init__(self) -> None:
        self.bound: dict[str, str] = {}
        self.found: list[tuple[int, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.partition(".")[0]
            self.bound[alias.asname or top] = alias.name if alias.asname else top

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == EVERY_NAME and node.module in PROGRAM_STARTERS:
                self.found.append((node.lineno, f"{node.module}.{EVERY_NAME}"))
            self.bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Call(self, node: ast.Call) -> None:
        called = self.dotted(node.func) or ""
        module, _, name = called.rpartition(".")
        starters = PROGRAM_STARTERS.get(module)
        on_a_loop = isinstance(node.func, ast.Attribute) and LOOP_STARTERS.fullmatch(node.func.attr)
        if (starters and starters.fullmatch(name)) or on_a_loop:
            self.found.append((node.lineno, called or ast.unparse(node.func)))
        self.generic_visit(node)

    def dotted(self, node: ast.expr) -> str | None:
        match node:
            case ast.Name(id=name):
                return self.bound.get(name)
            case ast.Attribute(value=value, attr=attr):
                base = self.dotted(value)
                return f"{base}.{attr}" if base else None
            case _:
                return None


def program_starts(module: Path) -> list[str]:
    starts = ProgramStarts()
    starts.visit(ast.parse(module.read_text("utf-8")))
    return [f"{module.name}:{line} calls {name}" for line, name in starts.found]


def test_only_lineage_starts_a_program() -> None:
    lineage_module = Path(lineage.__file__)
    assert program_starts(lineage_module), (
        "the test finds no start of a program in lineage, which starts them all: it cannot tell "
        "where a program is started"
    )
    elsewhere = [
        start
        for module in sorted(lineage_module.parent.rglob("*.py"))
        if module != lineage_module
        for start in program_starts(module)
    ]
    assert not elsewhere, (
        f"{elsewhere}: a program is started outside lineage; start it with lineage.started, "
        "lineage.started_in_own_session or lineage.HeldProgram, given its rule"
    )


def environments_received(
    written: Path, taken: re.Pattern[str], given: dict[str, str]
) -> Iterator[tuple[str, dict[str, str]]]:
    # The environment that each starter of lineage gives a program, as the program wrote it.
    command = [sys.executable, "-c", WRITES_ITS_ENVIRONMENT]
    with lineage.started(
        [*command, str(written / "started")],
        taken,
        given,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
    ) as program:
        program.wait(timeout=STARTER_LIMIT_SECONDS)
    yield "lineage.started", json.loads((written / "started").read_text())
    session_command = [*command, str(written / "session")]
    with lineage.started_in_own_session(session_command, taken, given) as program:
        program.wait(timeout=STARTER_LIMIT_SECONDS)
    yield "lineage.started_in_own_session", json.loads((written / "session").read_text())
    with lineage.HeldProgram([*command, str(written / "held")], taken, given) as held:
        began = time.monotonic()
        while not held.has_ended():
            if time.monotonic() - began > STARTER_LIMIT_SECONDS:
                pytest.fail("a program started by lineage.HeldProgram did not end")
            time.sleep(STARTER_WATCH_SECONDS)
    yield "lineage.HeldProgram", json.loads((written / "held").read_text())


def test_a_started_program_is_given_only_the_environment_built_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "standin-key-7f3a91"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("ZZ_UNTAKEN", key)
    monkeypatch.setenv("ZZ_TAKEN", "taken")
    taken = re.compile("PATH|ZZ_TAKEN|OPENAI_API_KEY")
    given_by_infonium = {"ZZ_GIVEN": "given"}
    received = dict(environments_received(tmp_path, taken, given_by_infonium))
    assert len(received) == 3, f"not every starter started its program: {sorted(received)}"
    for starter, environment in received.items():
        leaked = sorted(name for name, value in environment.items() if key in value)
        assert not leaked, f"{starter} gave its program {leaked} of infonium's environment"
        assert environment.get("ZZ_TAKEN") == "taken", f"{starter} left out what its rule takes"
        assert environment.get("ZZ_GIVEN") == "given", (
            f"{starter} left out what infonium gives its program"
        )
