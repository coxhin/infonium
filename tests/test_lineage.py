from hypothesis import given
from hypothesis import strategies as st

from infonium import browser, lineage, media, mineru, recording
from infonium.settings import Settings

# Each program infonium starts, by the rule of what it takes of infonium's environment; and of
# the variables a person may set, those each program takes and some it does not.
RULES = {
    "the renderer": browser.RENDERER_TAKES,
    "the media fetcher": media.FETCHER_TAKES,
    "MinerU": mineru.ENGINE_TAKES,
    "FFmpeg": recording.PROGRAMS_TAKE,
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
