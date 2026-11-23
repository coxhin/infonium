# The embedding engine

infonium finds a library's blocks by their meaning through an engine that turns text into vectors, reached over HTTP. This folder runs one on your own machine, on the CPU: [Text Embeddings Inference](https://github.com/huggingface/text-embeddings-inference) serving [Granite Embedding 97M Multilingual R2](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2) behind OpenAI's embeddings API.

You need Docker. No GPU is used.

```bash
docker compose up -d
```

This starts the engine at <http://127.0.0.1:8751/v1>, where infonium looks for it. The first start fetches the weights, 0.4 GB, and keeps them in a Docker volume; the engine answers once it has them. The image weighs 0.9 GB, and the running engine holds about 2 GB of memory.

Without the engine, infonium finds blocks by their words alone. Once it answers, the blocks of the library get their vectors in the background: on 24 cores, a document of 300 blocks took about 9 seconds.

To use another port, name it: `EMBEDDING_PORT=8761 docker compose up -d`. A port other than 8751 goes into `INFONIUM_EMBEDDING_BASE_URL` too.

`docker compose down` stops the engine and keeps the weights. `docker compose down --volumes` removes them as well.

## Another model

Name the model and its revision: `EMBEDDING_MODEL=ibm-granite/granite-embedding-311m-multilingual-r2 EMBEDDING_REVISION=main docker compose up -d`, and set `INFONIUM_EMBEDDING_MODEL` to the same name. infonium notices the new name when it starts and makes every vector again.

Granite Embedding 311M Multilingual R2 found the right passage first a little more often in English, and took three times as long and twice the memory. infonium sends the text of a block as it is, without a prefix or an instruction, so a model that wants one for queries, as multilingual E5, Qwen3 Embedding and Harrier do, finds less than it could.

## On a GPU

Text Embeddings Inference has images for NVIDIA cards as well (`ghcr.io/huggingface/text-embeddings-inference:cuda-1.9.4`). Put that image in `compose.yaml` and give the service a GPU as `engines/speech/compose.yaml` does. The engine then holds the card's memory for as long as it runs, which is why the CPU is the default.

## Another engine

infonium sends up to 32 blocks at a time, each cut to its first 2,000 characters, to `POST /v1/embeddings` with `encoding_format=float`, and a question by itself the same way. Any endpoint that answers this will do, such as Ollama's or OpenAI's: set `INFONIUM_EMBEDDING_BASE_URL`, `INFONIUM_EMBEDDING_API_KEY` and `INFONIUM_EMBEDDING_MODEL` to it.

## Licences

Text Embeddings Inference and Granite Embedding 97M Multilingual R2 are both under the Apache License 2.0. The engine is a separate program; its licence does not extend to infonium.
