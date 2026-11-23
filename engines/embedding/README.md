# The embedding engine

infonium finds a library's blocks by their meaning through an engine that turns text into vectors, reached over HTTP. This folder runs one on your own machine, on the CPU: [Text Embeddings Inference](https://github.com/huggingface/text-embeddings-inference) serving [Granite Embedding 97M Multilingual R2](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) behind OpenAI's embeddings API.

You need Docker. No GPU is used.

```bash
docker compose up -d
```

This starts the engine at <http://127.0.0.1:8751/v1>, where infonium looks for it. The first start fetches the weights, 0.4 GB, and keeps them in a Docker volume; the engine answers once it has them. The image weighs 0.9 GB, and the running engine holds about 2 GB of memory.

Without the engine, infonium finds blocks by their words alone. Once it answers, the blocks of the library get their vectors in the background, the blocks read last first.

The engine may take 4 processors. With them it gave vectors to about 30 blocks of 30 words a second, and a search took 184 ms at the median while it caught up 10,000 blocks. Given all 24 processors of the machine it was built on, it took 199 seconds over those blocks where it took 329 with 4, and a search took 111 ms. To give it another number, name it: `EMBEDDING_CPUS=8 docker compose up -d`.

To use another port, name it: `EMBEDDING_PORT=8761 docker compose up -d`. A port other than 8751 goes into `INFONIUM_EMBEDDING_BASE_URL` too.

## Without the network

The engine reaches the Hugging Face hub only to fetch the weights. Once they are fetched, start it with `EMBEDDING_OFFLINE=1 docker compose up -d`: it then reads them from its volume and asks the hub nothing. Started so with no network at all (`docker run --network none`), it loaded the weights and was ready to answer. The port it publishes stays on 127.0.0.1.

`docker compose down` stops the engine and keeps the weights. `docker compose down --volumes` removes them as well.

## Another model

Name the model and its revision: `EMBEDDING_MODEL=ibm-granite/granite-embedding-311m-multilingual-r2 EMBEDDING_REVISION=main docker compose up -d`, and set `INFONIUM_EMBEDDING_MODEL` to the same name. infonium notices the new name when it starts and makes every vector again.

Granite Embedding 311M Multilingual R2 found the right passage first a little more often in English, and took three times as long and twice the memory. infonium sends the text of a block as it is, without a prefix or an instruction, so a model that wants one for queries, as multilingual E5, Qwen3 Embedding and Harrier do, finds less than it could.

## On a GPU

Text Embeddings Inference has images for NVIDIA cards as well (`ghcr.io/huggingface/text-embeddings-inference:cuda-1.9.4`). Put that image in `compose.yaml` and give the service a GPU as `engines/speech/compose.yaml` does. The engine then holds the card's memory for as long as it runs, which is why the CPU is the default.

## Another engine

infonium sends up to 8 blocks at a time, each cut to its first 2,000 characters, to `POST /v1/embeddings` with `encoding_format=float`, and a question by itself the same way. A block of fewer than 8 words, such as a heading, is not sent. Any endpoint that answers this will do, such as Ollama's or OpenAI's: set `INFONIUM_EMBEDDING_BASE_URL`, `INFONIUM_EMBEDDING_API_KEY` and `INFONIUM_EMBEDDING_MODEL` to it.

infonium takes an endpoint for away, and tries it again every 10 seconds, when it cannot be reached, does not answer in time, or answers 429 or 5xx. Any other error, such as 401 for a wrong key or 404 for a wrong address or model, stops the index of blocks until infonium starts again, and every search fails with that error.

## Licences

Text Embeddings Inference and Granite Embedding 97M Multilingual R2 are both under the Apache License 2.0. The engine is a separate program; its licence does not extend to infonium.
