# The speech recogniser

infonium turns a recording into text through a recogniser that it reaches over HTTP. This folder runs one on your own machine: [speaches](https://github.com/speaches-ai/speaches) serving Whisper large-v3 behind OpenAI's transcription API.

You need Docker with the NVIDIA Container Toolkit, and a GPU with 5 GB of memory free.

```bash
docker compose up -d
docker compose exec recogniser curl -fsS -X POST http://localhost:8000/v1/models/Systran/faster-whisper-large-v3
```

The first command starts the recogniser at <http://127.0.0.1:8750/v1>, where infonium looks for it. The second fetches the weights, 2.9 GB, once. They are kept in a Docker volume.

To use another GPU or another port, name them: `SPEECH_GPU=1 SPEECH_PORT=8760 docker compose up -d`. A port other than 8750 goes into `INFONIUM_SPEECH_BASE_URL` too.

`docker compose down` stops the recogniser and keeps the weights. `docker compose down --volumes` removes them as well.

## A faster model

Whisper large-v3-turbo is almost twice as fast, and less accurate in Chinese. Fetch `deepdml/faster-whisper-large-v3-turbo-ct2` (1.6 GB) the same way and set `INFONIUM_SPEECH_MODEL` to that name.

## Another recogniser

infonium sends a recording in pieces of at most 16 seconds to `POST /v1/audio/transcriptions`, asks for `response_format=verbose_json` with segment times, and takes each segment as one utterance. Any endpoint that answers this will do: set `INFONIUM_SPEECH_BASE_URL`, `INFONIUM_SPEECH_API_KEY` and `INFONIUM_SPEECH_MODEL` to it.

Of each segment infonium reads `text`, `start`, `end`, `no_speech_prob` and `avg_logprob`, so the endpoint has to give all five. An endpoint that does not leave out silence itself may hear words in it.

## Licences

speaches is MIT-licensed, and so are the weights `Systran/faster-whisper-large-v3` and `deepdml/faster-whisper-large-v3-turbo-ct2`, which are conversions of OpenAI's Whisper models. The recogniser is a separate program; its licence does not extend to infonium.
