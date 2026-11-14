# infonium

infonium digests what you read into a summary and takeaways in your language. Every statement links back to the exact passage of the source that supports it, so you can check a claim with one click.

Web articles work today. PDFs, podcasts and videos are next.

## Run it

You need [uv](https://docs.astral.sh/uv/), Node.js 22 or later, [pnpm](https://pnpm.io/), and an API key for an OpenAI-compatible model.

```bash
cp .env.example .env    # put your API key in .env
make install
make dev
```

Open <http://localhost:5173>, paste the URL of an article and import it. The article opens in the reader and its digest appears beside it. Click a numbered citation to jump to the quoted passage.

The API listens on port 8710 and the UI on 5173. When one is taken, pass another: `make dev API_PORT=8720 UI_PORT=5180`.

## Configuration

Settings are environment variables, read from `.env` by `make dev`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Key for the endpoint that serves the model |
| `OPENAI_BASE_URL` | OpenAI | Endpoint, when the model is served elsewhere |
| `INFONIUM_MODEL` | `gpt-6.1-terra` | Model that writes digests |
| `INFONIUM_LANGUAGE` | `简体中文` | Language digests are written in |
| `INFONIUM_DATA_DIR` | `data` | Where the library and page snapshots are stored |

## How it works

An imported page is parsed into a document: an ordered list of blocks such as headings, paragraphs and tables. The model reads the numbered blocks and must back every statement with a quote from one of them. infonium then looks each quote up in the document and keeps only the citations it finds there, as anchors: a block plus a character range. The reader resolves an anchor to that range and highlights it.

## Development

```bash
make check       # lint, types, and generated protocol files are current
make protocol    # regenerate protocol/openapi.json and web/src/protocol.d.ts
```

The HTTP protocol is defined by the models in `src/infonium`. `protocol/openapi.json` and `web/src/protocol.d.ts` are generated from them and never edited by hand.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
