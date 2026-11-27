from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel
from yt_dlp.utils import DownloadError

# A stand-in for yt-dlp, as a discovery asks it for the id of a channel named by its handle:
# yt_dlp.YoutubeDL(options).extract_info(address, download=False), plainly or as a context
# manager. It records the options each YoutubeDL was made with and every address it was asked,
# and answers as a test scripts it: with what extract_info gives, or by raising DownloadError.

Said = Callable[[str], Mapping[str, Any] | None]
# The positional parameters of YoutubeDL.extract_info after the address, in their order.
EXTRACT_PARAMETERS = ("download", "ie_key", "extra_info", "process", "force_generic_extractor")


class Asked(BaseModel):
    address: str
    download: bool
    # How many requests the stand-in sites had heard when yt-dlp was asked.
    after_requests: int


def unscripted(address: str) -> Mapping[str, Any] | None:
    raise DownloadError(f"ERROR: [youtube:tab] {address}: the stand-in was given no answer")


def channel_answer(address: str, channel: str) -> dict[str, Any]:
    # What extract_info gives for a channel's page: the channel's tab, as a list of its videos.
    return {
        "_type": "playlist",
        "id": channel,
        "channel_id": channel,
        "channel": "Night trains",
        "uploader_id": "@nighttrains",
        "title": "Night trains - Videos",
        "webpage_url": address,
        "entries": [],
    }


class YtDlpStandin:
    def __init__(self, heard: Callable[[], int]) -> None:
        self.heard = heard
        self.options: list[dict[str, Any]] = []
        self.asked: list[Asked] = []
        self.said: Said = unscripted

    def forget(self) -> None:
        self.options.clear()
        self.asked.clear()
        self.said = unscripted

    def youtube_dl(self) -> type:
        standin = self

        class StandinYoutubeDL:
            def __init__(self, params: Mapping[str, Any] | None = None, *given: Any) -> None:
                standin.options.append(dict(params or {}))

            def __enter__(self) -> "StandinYoutubeDL":
                return self

            def __exit__(self, *raised: object) -> None:
                return None

            def close(self) -> None:
                pass

            @staticmethod
            def sanitize_info(info: Any, remove_private_keys: bool = False) -> Any:
                return info

            def extract_info(self, url: str, *given: Any, **options: Any) -> Any:
                passed = dict(zip(EXTRACT_PARAMETERS, given, strict=False)) | options
                standin.asked.append(
                    Asked(
                        address=url,
                        download=passed.get("download", True),
                        after_requests=standin.heard(),
                    )
                )
                return standin.said(url)

        return StandinYoutubeDL
