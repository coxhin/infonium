# infonium

infonium digests what you read, watch and listen to into a summary and takeaways in your language. Every statement links back to the passage of the source that supports it. One click takes you to the sentence in the article, the lines on the PDF page, or the moment in the recording.

## What it reads

| Source | Add it by | Read through |
| --- | --- | --- |
| Web article | pasting its address | the page's HTML |
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

Web articles need nothing more. Each other kind of source needs one more program:

| To read | Install |
| --- | --- |
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
| `INFONIUM_SPEECH_BASE_URL` | `http://127.0.0.1:8750/v1` | Endpoint of the speech recogniser; the default is the one that `engines/speech` starts |
| `INFONIUM_SPEECH_API_KEY` | `unused` | Key for that endpoint; the local recogniser asks for none |
| `INFONIUM_SPEECH_MODEL` | `Systran/faster-whisper-large-v3` | Model the recogniser transcribes with |

## How it works

A source is read into a document: an ordered list of blocks such as headings, paragraphs and tables. A block of a PDF or a recording also knows its places in the source. In a PDF, a place is a box on a page that holds part of the block, usually one line. In a recording, a place is the stretch of time in which part of the block is spoken.

The model reads the numbered blocks and must back every statement with a quote from one of them. infonium then looks each quote up in the document and keeps only the citations it finds there, as anchors: a block plus a character range.

The reader resolves an anchor to that range and highlights it. In a PDF it also outlines the places of the range on the page. In a recording it plays from the place where the range is spoken.

infonium keeps a snapshot of every source it reads: the HTML of a page, the PDF file, or the sound of a recording. The reader shows the snapshot, so a citation still leads to its passage after the source has changed or gone.

## Limits

- A web page that scripts build in the browser, or that turns programs away, is not read.
- A PDF without a usable text layer, a scan above all, is read as pictures of its pages. The words of a paragraph or a heading are then recognised together and spread over its lines by their lengths, so a citation may outline a neighbouring line as well, or miss a line that holds only a word of the quote. A caption, a footnote or a reference is outlined whole. The engine chooses one way of reading for a whole file: scanned pages in a file that has a text layer elsewhere are read line by line, which glues and splits more words.
- A code listing is read as a whole in every PDF, and each of its rows is outlined as an even slice of its box.
- How well a page read as pictures comes out depends on its writing, and that has been tried on no more than a paragraph of each. Latin, Cyrillic and Greek writing, Chinese and Japanese came out right or nearly, and Korean, Arabic, Thai, Hindi, Tamil, Bengali and Tibetan with a few wrong characters. In writing the engine does not know, Hebrew, Georgian, Armenian, Amharic and Khmer among what was tried, it writes words that are not on the page, and may take so long that the reading is given up.
- MinerU's models want a GPU. On the machine infonium was built on, a PDF of up to ten pages took about 4 to 5.5 GB of its memory, and a scan of 40 pages about 10 GB.
- Of a video only the sound is kept. The reader plays it and does not show the picture.
- An address is fetched for up to ten minutes and up to 2 GiB. A video or podcast page is fetched for up to an hour, and its recording may run up to four hours and hold up to 2 GiB. What goes beyond is not read.
- Speech is recognised in pieces of up to 16 seconds. A citation in a recognised recording plays from the start of the piece that holds the quote. A word at the edge of a piece is lost now and then, and each piece is punctuated by itself.
- The default speech model is less accurate in Chinese than in English.
- infonium serves one person on one machine. It has no accounts and no authentication, so keep its ports off the network.

## Development

```bash
make check       # lint, types, and generated protocol files are current
make protocol    # regenerate protocol/openapi.json and web/src/protocol.d.ts
```

The HTTP protocol is defined by the models in `src/infonium`. `protocol/openapi.json` and `web/src/protocol.d.ts` are generated from them and never edited by hand.

There is no test suite yet.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
