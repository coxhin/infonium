# infonium

infonium digests what you read, watch and listen to into a summary and takeaways in your language. Every statement links back to the passage of the source that supports it. One click takes you to the sentence in the article, the lines on the PDF page, or the moment in the recording.

## What it reads

| Source | Add it by | Read through |
| --- | --- | --- |
| Web article | pasting its address, or uploading the page saved from your browser (for a site that turns infonium away) | the page's HTML, and the page as a browser builds it when its HTML holds little text |
| PDF, with a text layer or scanned | uploading the file or pasting its address | [MinerU](https://github.com/opendatalab/MinerU), which also recognises the text of scanned pages |
| Video or podcast page, such as YouTube, Bilibili, TED, Apple Podcasts and SoundCloud | pasting its address | the page's own captions, or speech recognition when it has none |
| Audio or video file | uploading the file or pasting its address | speech recognition |

A PDF opens as its pages, with the text read from them one tab away. A recording opens as a player above its transcript.

## Run it

You need [uv](https://docs.astral.sh/uv/), Node.js 22 or later, [pnpm](https://pnpm.io/), and an API key for an OpenAI-compatible model.

```bash
cp .env.example .env    # put your API key in .env
make install
make dev
```

Open <http://localhost:5173>. Paste an address or upload a file. The source opens in the reader and its digest appears beside it. Click a numbered citation to jump to the quoted passage.

The API listens on port 8710 and the UI on 5173. When one is taken, pass another: `make dev API_PORT=8720 UI_PORT=5180`.

Each kind of source needs one more program:

| To read | Install |
| --- | --- |
| Web pages that scripts build | [Google Chrome](https://www.google.com/chrome/), as the `google-chrome` command, or Chromium, on Linux 5.9 or later; `playwright install` is not needed. The browser must start with its sandbox, which infonium checks before each page and never turns off: on Ubuntu 23.10 and later only a packaged Chrome with an AppArmor profile does, and in a container the seccomp profile must allow user namespaces (not tried). A page whose HTML holds little text is opened in it and read again. Without it, such a page is read from its HTML alone, and one with no text there is not read. Set `INFONIUM_BROWSER` when the command is another or not on the `PATH`. |
| PDFs | [MinerU](https://github.com/opendatalab/MinerU) 3.4.2 with its `core` extra (`mineru[core]==3.4.2`), as the `mineru` command. infonium reads what this version writes, and with another version every PDF fails. Set `INFONIUM_PDF_ENGINE` when the command is not on the `PATH`. |
| Recordings | [FFmpeg](https://ffmpeg.org/), as the `ffmpeg` and `ffprobe` commands on the `PATH`. |
| Recordings without captions | A speech recogniser. [`engines/speech`](engines/speech) starts one on your machine with two commands. |

## Configuration

Settings are environment variables, read from `.env` by `make dev`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Key for the endpoint that serves the model |
| `OPENAI_BASE_URL` | OpenAI | Endpoint, when the model is served elsewhere |
| `INFONIUM_MODEL` | `gpt-6.1-terra` | Model that writes digests |
| `INFONIUM_WIRE_API` | `chat_completions` | API the endpoint speaks: `chat_completions` or `responses` |
| `INFONIUM_LANGUAGE` | `简体中文` | Language digests are written in |
| `INFONIUM_DATA_DIR` | `data` | Where the library and the snapshots of its sources are stored |
| `INFONIUM_PDF_ENGINE` | `mineru` | MinerU command that parses PDFs |
| `INFONIUM_BROWSER` | `google-chrome` | Chrome command, or its path, that renders web pages |
| `INFONIUM_SPEECH_BASE_URL` | `http://127.0.0.1:8750/v1` | Endpoint of the speech recogniser; the default is the one that `engines/speech` starts |
| `INFONIUM_SPEECH_API_KEY` | `unused` | Key for that endpoint; the local recogniser asks for none |
| `INFONIUM_SPEECH_MODEL` | `Systran/faster-whisper-large-v3` | Model the recogniser transcribes with |

## How it works

A source is read into a document: an ordered list of blocks such as headings, paragraphs and tables. A block of a PDF or a recording also knows its places in the source. In a PDF, a place is a box on a page that holds part of the block, usually one line. In a recording, a place is the stretch of time in which part of the block is spoken.

The model reads the numbered blocks and must back every statement with a quote from one of them. infonium then looks each quote up in the document and keeps only the citations it finds there, as anchors: a block plus a character range.

The reader resolves an anchor to that range and highlights it. In a PDF it also outlines the places of the range on the page. In a recording it plays from the place where the range is spoken.

infonium keeps a snapshot of every source it reads: the HTML of a page, the PDF file, or the sound of a recording. The reader shows the snapshot, so a citation still leads to its passage after the source has changed or gone.

## Limits

- A site that turns programs away is not read. When it answers with anything but success, the reading fails with that status. When it answers with a check for a person in place of the page, the reading fails with the site's name, as long as the check is one that infonium knows; a check it does not know may be read as the page.
- A page of fewer than 200 words is taken for a check when its title is one that guards give their checks, or when the address it ends at names a captcha and carries another address to go on to, so a short page that happens to look like that is refused.
- A page whose HTML holds 200 words or more, menus and links included, is read from its HTML and never opened in a browser.
- A page of fewer than 200 words waits while it is opened in a browser, 90 seconds at most: the three such pages of a survey of 30 sites took 5 to 26 seconds.
- Of the HTML and the page the browser builds, the one with more words is kept, even when those words are menus and links.
- When the browser is missing or cannot render a page, the page is read from its HTML alone, and one with no text there fails with the browser's reason.
- A page whose HTML only asks for JavaScript may then be read as that one line.
- A page in the browser may take 3 GiB of memory, counted over the browser and the programs that drive it, and 1 GiB of disk. It may pass the memory bound by about 1.5 GiB, and the disk bound by what it writes in about 2 seconds, before it is ended.
- Nothing but its 90 seconds bounds the processor time a page takes in the browser, on every core the browser uses, and watching it costs infonium about an eighth of a core.
- A page in the browser reaches only public addresses, not this machine, a network one of its interfaces stands in, or the ranges set aside for special uses, but it can have this machine's resolver look up any name.
- Of public addresses, infonium cannot tell those that lead back into your own networks, so a page still reaches your router's public address and the ports it forwards, a company or campus network with public addresses, the networks a VPN routes to, a network's own NAT64 prefix, and services that trust this machine's address.
- The browser is given none of infonium's settings or keys, only `PATH`, the locale, the time zone and a home of its own.
- The other programs infonium starts are given none of them either, nor any variable named like a key, a token, a password or a secret: FFmpeg only `PATH`; the fetcher of video and podcast pages `PATH`, the locale, the proxies and yt-dlp's `YTDLP_` settings; MinerU `PATH`, the locale, the proxies, `HOME`, `XDG_CACHE_HOME`, `LD_LIBRARY_PATH`, its own `MINERU_` settings, the GPU's `CUDA_` settings and the `HF_`, `HUGGINGFACE_` and `MODELSCOPE_` settings of the hubs it fetches its models from. Only `file` is given all of infonium's environment: when infonium loads yt-dlp, once each time infonium starts and each time `--reload` restarts it, Python runs `file` on its own executable, and `file` reads that executable and its magic database and nothing else.
- A rendering does not go through a proxy set with `HTTP_PROXY` or `HTTPS_PROXY`, so where every connection must take such a proxy, a page that scripts build cannot be rendered and is read from its HTML alone; this comes from reading the code and has not been tried.
- If infonium is killed while a page is in the browser, the browser is ended and its folders are removed within half a second. If the program that drives the browser is killed with it, the browser's work folder stays until infonium starts again, and a small folder stays in the system's temporary folder.
- Each time an address of this machine changes, as when a container starts or stops beside it, the browser breaks off what a page is loading, and the page is opened anew once a second passes without a change, in at most five attempts, each begun within its first 25.5 seconds.
- When the network changed through every attempt, the fullest attempt is kept, which may hold only part of the page, and when neither it nor the page's HTML holds text, the reading fails saying that the network changed.
- A PDF without a usable text layer, a scan above all, is read as pictures of its pages. The words of a paragraph or a heading are then recognised together and spread over its lines by their lengths, so a citation may outline a neighbouring line as well, or miss a line that holds only a word of the quote. A caption, a footnote or a reference is outlined whole. The engine chooses one way of reading for a whole file: scanned pages in a file that has a text layer elsewhere are read line by line, which glues and splits more words.
- A code listing is read as a whole in every PDF, and each of its rows is outlined as an even slice of its box.
- On a page that a PDF stores turned, a word may come out split in two when the file's own figures for the turn are not exact, as pypdf's `Transformation().rotate` writes them.
- How well a page read as pictures comes out depends on its writing, and that has been tried on no more than a paragraph of each. Latin, Cyrillic and Greek writing, Chinese and Japanese came out right or nearly, and Korean, Arabic, Thai, Hindi, Tamil, Bengali and Tibetan with a few wrong characters. In writing the engine does not know, Hebrew, Georgian, Armenian, Amharic and Khmer among what was tried, it writes words that are not on the page, and may take so long that the reading is given up.
- MinerU's models want a GPU. On the machine infonium was built on, a PDF of up to ten pages took about 4 to 5.5 GB of its memory, and a scan of 40 pages about 10 GB.
- MinerU reaches a server of its own on 127.0.0.1 through a proxy set with `HTTP_PROXY`, so where one is set, `NO_PROXY` must name 127.0.0.1. Behind a proxy that refused connections, a PDF failed once MinerU had waited for its server as long as `MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS` allows (300 seconds unless set; 30 in the run), and with 127.0.0.1 named in `NO_PROXY` it was read in 16 seconds. That `ALL_PROXY` and a lowercase `http_proxy` do the same comes from reading the code.
- Of a video only the sound is kept. The reader plays it and does not show the picture.
- A WAV file of Vorbis sound, or of AAC sound without ADTS frame headers, is refused, since FFmpeg reads only part of such sound. FFmpeg writes both kinds when told to keep the sound of an Ogg or M4A file in a WAV. An M4A, an Ogg or an uncompressed WAV of the same recording is read.
- An address is fetched for up to ten minutes and up to 2 GiB. A video or podcast page is fetched for up to an hour, and its recording may run up to four hours and hold up to 2 GiB. What goes beyond is not read.
- Speech is recognised in pieces of up to 16 seconds. A citation in a recognised recording plays from the start of the piece that holds the quote. A word at the edge of a piece is lost now and then, and each piece is punctuated by itself.
- The default speech model is less accurate in Chinese than in English.
- infonium serves one person on one machine. It has no accounts and no authentication, so keep its ports off the network.

## Development

```bash
make check       # lint, types, and generated protocol files are current
make test        # the tests that need nothing outside the repository
make protocol    # regenerate protocol/openapi.json and web/src/protocol.d.ts
```

The HTTP protocol is defined by the models in `src/infonium`. `protocol/openapi.json` and `web/src/protocol.d.ts` are generated from them and never edited by hand.

`make test` runs the tests in `tests` without the network, a browser, MinerU, FFmpeg, a speech recogniser or a model: a test that opens a network socket or looks up a name fails. A test that needs one of these programs carries its name as a marker and runs when the marker is named; it is skipped when the program is not found the way infonium finds it:

```bash
uv run pytest -m chrome    # Chrome or Chromium, by INFONIUM_BROWSER
uv run pytest -m ffmpeg    # ffmpeg and ffprobe
uv run pytest -m mineru    # MinerU 3.4.2, by INFONIUM_PDF_ENGINE, on a GPU
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
