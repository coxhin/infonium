# infonium

infonium digests what you read, watch and listen to into a summary and takeaways in your language. Every statement links back to the passage of the source that supports it. One click takes you to the sentence in the article, the lines on the PDF page, or the moment in the recording. It also translates a source into your language in the source's own form, and answers questions about one source or about everything you have read, with the same links.

## What it reads

| Source | Add it by | Read through |
| --- | --- | --- |
| Web article | pasting its address, or uploading the page saved from your browser (for a site that turns infonium away) | the page's HTML, and the page as a browser builds it when its HTML holds little text |
| PDF, with a text layer or scanned | uploading the file or pasting its address | [MinerU](https://github.com/opendatalab/MinerU), which also recognises the text of scanned pages |
| Video or podcast page, such as YouTube, Bilibili, TED, Apple Podcasts and SoundCloud | pasting its address | the page's own captions, or speech recognition when it has none |
| Audio or video file | uploading the file or pasting its address | speech recognition |

A PDF opens as its pages, with the text read from them one tab away. A web article opens as its text, with the page it was read from one tab away. A recording opens as a player above its transcript.

## What it does with a source

A column beside the reader holds three tabs: the digest (摘要), a chat (对话), and a notebook (笔记), which stays empty until notes are built.

### Translation

A switch above the reader shows the source (原文), its translation (译文), or both (对照). A translation is made when a source is first shown with the switch on 译文 or 对照, into the language of digests, and arrives block by block. It keeps the form of its source:

| Source | Its translation |
| --- | --- |
| PDF | On the page: each block is drawn where its lines stand, in the page's layout and at the size of its text. With both on show and room for two, the source's page stands beside the translated page. |
| Recording | As lines that follow the playback: each sentence of the transcript with its translation. A click on a line plays from that moment. |
| Web article | In the page's own layout: each block's translation stands in the place of its words, or after them with both on show. |

Every source can also be read as text, each block in translation or followed by its translation. A formula is set as a formula and is never translated. One block can be translated again, by the model that writes digests.

### The chat

The chat answers a question about the open source, or about the whole library (整个库). An answer appears as it is written. Each of its statements carries citations that open the quoted passage, in whichever source it lies.

## Run it

You need [uv](https://docs.astral.sh/uv/), Node.js 22 or later, [pnpm](https://pnpm.io/), and an API key for an OpenAI-compatible model.

```bash
cp .env.example .env    # put your API key in .env
make install
make dev
```

Open <http://localhost:5173>. Paste an address or upload a file. The source opens in the reader and its digest appears beside it. Click a numbered citation to jump to the quoted passage. Switch to 译文 to read the source in your language, and ask about it under 对话.

The API listens on port 8710 and the UI on 5173. When one is taken, pass another: `make dev API_PORT=8720 UI_PORT=5180`.

Some sources and features need one more program:

| For | Install |
| --- | --- |
| Web pages that scripts build | [Google Chrome](https://www.google.com/chrome/), as the `google-chrome` command, or Chromium, on Linux 5.9 or later; `playwright install` is not needed. The browser must start with its sandbox, which infonium checks before each page and never turns off: on Ubuntu 23.10 and later only a packaged Chrome with an AppArmor profile does, and in a container the seccomp profile must allow user namespaces (not tried). A page whose HTML holds little text is opened in it and read again. Without it, such a page is read from its HTML alone, and one with no text there is not read. Set `INFONIUM_BROWSER` when the command is another or not on the `PATH`. |
| PDFs | [MinerU](https://github.com/opendatalab/MinerU) 3.4.2 with its `core` extra (`mineru[core]==3.4.2`), as the `mineru` command. infonium reads what this version writes, and with another version every PDF fails. Set `INFONIUM_PDF_ENGINE` when the command is not on the `PATH`. |
| Recordings | [FFmpeg](https://ffmpeg.org/), as the `ffmpeg` and `ffprobe` commands on the `PATH`. |
| Recordings without captions | A speech recogniser. [`engines/speech`](engines/speech) starts one on your machine with two commands. |
| Passages found by their meaning, for questions about the whole library | An embedding engine. [`engines/embedding`](engines/embedding) starts one on your machine, on the CPU, with one command. Without it, the library is searched by words alone. |

The Python that infonium runs on must be able to load SQLite extensions: infonium loads [sqlite-vec](https://github.com/asg017/sqlite-vec) into every connection to the library's file. uv's Python 3.13 can. On a Python whose `sqlite3` cannot, infonium does not start; this comes from reading the code and has not been tried.

## Configuration

Settings are environment variables, read from `.env` by `make dev`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | none | Key for the endpoint that serves the model |
| `OPENAI_BASE_URL` | OpenAI | Endpoint, when the model is served elsewhere |
| `INFONIUM_MODEL` | `gpt-6.1-terra` | Model that writes digests and answers questions, and translates one block again |
| `INFONIUM_TRANSLATION_MODEL` | `gpt-6-luna` | Model that translates a document |
| `INFONIUM_WIRE_API` | `chat_completions` | API the endpoint speaks: `chat_completions` or `responses` |
| `INFONIUM_LANGUAGE` | `简体中文` | Language of digests and translations |
| `INFONIUM_DATA_DIR` | `data` | Where the library and the snapshots of its sources are stored |
| `INFONIUM_PDF_ENGINE` | `mineru` | MinerU command that parses PDFs |
| `INFONIUM_BROWSER` | `google-chrome` | Chrome command, or its path, that renders web pages |
| `INFONIUM_SPEECH_BASE_URL` | `http://127.0.0.1:8750/v1` | Endpoint of the speech recogniser; the default is the one that `engines/speech` starts |
| `INFONIUM_SPEECH_API_KEY` | `unused` | Key for that endpoint; the local recogniser asks for none |
| `INFONIUM_SPEECH_MODEL` | `Systran/faster-whisper-large-v3` | Model the recogniser transcribes with |
| `INFONIUM_EMBEDDING_BASE_URL` | `http://127.0.0.1:8751/v1` | Endpoint of the embedding engine; the default is the one that `engines/embedding` starts |
| `INFONIUM_EMBEDDING_API_KEY` | `unused` | Key for that endpoint; the local engine asks for none |
| `INFONIUM_EMBEDDING_MODEL` | `ibm-granite/granite-embedding-97m-multilingual-r2` | Model the engine embeds with; when it changes, every vector is made again |

## How it works

A source is read into a document: an ordered list of blocks such as headings, paragraphs and tables. A block of a PDF or a recording also knows its places in the source. In a PDF, a place is a box on a page that holds part of the block, usually one line. In a recording, a place is the stretch of time in which part of the block is spoken.

The model reads the numbered blocks and must back every statement with a quote from one of them. infonium then looks each quote up in the document and keeps only the citations it finds there, as anchors: a block plus a character range.

The reader resolves an anchor to that range and highlights it. In a PDF it also outlines the places of the range on the page. In a recording it plays from the place where the range is spoken.

infonium keeps a snapshot of every source it reads: the HTML of a page, the PDF file, or the sound of a recording. The reader shows the snapshot, so a citation still leads to its passage after the source has changed or gone.

A document is translated into the language of digests by a model of its own. infonium first shows that model passages from across the document. The model says which language the document is in and, if it needs translating, lists the names and terms that recur, each with the one translation to use. In a technical document, a term of art that the source writes in English stays in English, as people of the field say it: the name of a model, a method, a component, a tool, a format or a measure, in the singular and with no translation in brackets after it. The general vocabulary of science and computing, which has a settled word in the reader's language, is translated, and so is everything in a source that is not technical. The list gives a term that stays in English as itself, so that requests write it alike. infonium then translates the document a few blocks a request, eight requests at a time, and keeps each block as it comes; each request carries the listed terms that its passages use. A link, a style or a formula of a passage goes to the model as a pair of tags around its words and comes back as a mark on the words that translate them. A formula is not translated: infonium puts its own text back between its tags, whatever the reply holds there, since a model that copies LaTeX alters it. A reply is kept only if it answers each block with exactly that block's passages, holds no remnant of a tag, and keeps every formula's tags with no word of the sentence inside them. The sentences of a recording are also checked against their neighbours: an answer out of proportion with its source, or the same answer to different sentences, is not kept. A reply that fails any of these is asked for again, up to three times. A table is translated cell by cell into the same grid, and a recording sentence by sentence along its stretches of time. Listings and formulas stay as written.

The reader draws a PDF's translation itself, over the page. A block's lines that follow one another in one column are one area. The area is filled with the colour of the paper around the lines, and the translation is set in it at the size of the source's text, told from the height of its lines, in the colour of their ink. A formula is set from its LaTeX by [KaTeX](https://katex.org/), here as in the text.

For a web article the reader can also show the kept page itself. infonium serves it with headers that let none of its scripts, plugins or frames run and send none of its forms. It removes from the page what would run, makes its links and forms open nothing by themselves, and marks the element that holds each block's text, and each cell of a table, so that the reader can put a translation in its place. A link that is clicked is opened by the reader, in a new tab.

A question about a source goes to the model that writes digests with the source's whole document and the earlier questions with their answers. A question about the library goes with an outline of the library and two tools that change nothing: one searches the library's blocks, the other reads a run of a source's blocks. Everything of a source reaches the model as JSON, so that a source's text cannot pass for anything but itself. The model answers with statements whose citations name a block and a quote. infonium keeps the citations it finds, as it does for a digest, and counts on each statement those it could not find.

The blocks of every document the library holds are indexed in the library's file, by their words and, through an embedding engine, by their meaning. A block is given to the engine under the title of its item. A search gives first the blocks that hold every word of it, then the blocks nearest to it in meaning and the blocks without a vector yet that hold some of its words, and says of each how it was found.

## Limits

### Web pages

- A site that turns programs away is not read. When it answers with anything but success, the reading fails with that status. When it answers with a check for a person in place of the page, the reading fails with the site's name, as long as the check is one that infonium knows; a check it does not know may be read as the page.
- A page of fewer than 200 words is taken for a check when its title is one that guards give their checks, or when the address it ends at names a captcha and carries another address to go on to, so a short page that happens to look like that is refused.
- A page whose HTML holds 200 words or more, menus and links included, is read from its HTML and never opened in a browser.
- A page of fewer than 200 words waits while it is opened in a browser, 90 seconds at most: the three such pages of a survey of 30 sites took 5 to 26 seconds.
- Of the HTML and the page the browser builds, the one with more words is kept, even when those words are menus and links.
- When the browser is missing or cannot render a page, the page is read from its HTML alone, and one with no text there fails with the browser's reason.
- A page whose HTML only asks for JavaScript may then be read as that one line.
- A formula in a web page is read as the characters the page shows for it, without their places: 1/k set as a fraction came out as 1k, in the document and in everything written from it.
- A page in the browser may take 3 GiB of memory, counted over the browser and the programs that drive it, and 1 GiB of disk. It may pass the memory bound by about 1.5 GiB, and the disk bound by what it writes in about 2 seconds, before it is ended.
- Nothing but its 90 seconds bounds the processor time a page takes in the browser, on every core the browser uses, and watching it costs infonium about an eighth of a core.
- A page in the browser reaches only public addresses, not this machine, a network one of its interfaces stands in, or the ranges set aside for special uses, but it can have this machine's resolver look up any name.
- Of public addresses, infonium cannot tell those that lead back into your own networks, so a page still reaches your router's public address and the ports it forwards, a company or campus network with public addresses, the networks a VPN routes to, a network's own NAT64 prefix, and services that trust this machine's address.
- A rendering does not go through a proxy set with `HTTP_PROXY` or `HTTPS_PROXY`, so where every connection must take such a proxy, a page that scripts build cannot be rendered and is read from its HTML alone; this comes from reading the code and has not been tried.
- If infonium is killed while a page is in the browser, the browser is ended and its folders are removed within half a second. If the program that drives the browser is killed with it, the browser's work folder stays until infonium starts again, and a small folder stays in the system's temporary folder.
- Each time an address of this machine changes, as when a container starts or stops beside it, the browser breaks off what a page is loading, and the page is opened anew once a second passes without a change, in at most five attempts, each begun within its first 25.5 seconds.
- When the network changed through every attempt, the fullest attempt is kept, which may hold only part of the page, and when neither it nor the page's HTML holds text, the reading fails saying that the network changed.
- The kept page (原件) is shown without its scripts, so it lacks what they build or show: menus, sections that a script unfolds, pictures that a script loads, and every style of a page that loads its stylesheet by script.
- The kept page asks its site for its stylesheets, pictures, fonts and media each time it is shown, so a page whose site has changed or gone is shown without them. The site learns infonium's address from some of these requests: over 51 pages, 83 requests named it, as the origin of a font request or as the referrer of what the page's own CSS loads.
- Some of those requests fail because the page is shown from infonium's address and sends no referrer: fonts that a site serves only to its own pages, and everything from a site that asks for its own referrer (on sspai.com, every stylesheet and article picture).
- Of the blocks of a web page that are not figures, 87% have an element of their own in the kept page (4,210 of 4,858 over 51 pages), and 22 of 35 tables. The others are shown there as the page has them, untranslated, a citation of one sends the reader to 文本, and the line above the page says how many they are. A page that parts its paragraphs by line breaks inside one element has many: 108 of the 274 blocks of an essay on paulgraham.com.
- A citation is marked in the kept page by its words where the page shows them as the block's text, white space and the quotation marks of an inline quotation aside. Elsewhere the whole paragraph is marked, and in a table its first cell.
- A link in the kept page opens in a new tab, and one to a place in the page scrolls there. A link to infonium itself, to anything but a web address, or, in a saved page with no web address of its own, to a relative address, does nothing.
- When the kept page cannot be fetched, or something other than the item's page comes back, 原件 says so and shows nothing of it; 文本 still has the whole text.
- In one column, where fewer than 160 pixels of the window are left for the kept page, it keeps 160 pixels and its foot lies under the column beside the reader.

### PDFs

- A PDF without a usable text layer, a scan above all, is read as pictures of its pages. The words of a paragraph or a heading are then recognised together and spread over its lines by their lengths, so a citation may outline a neighbouring line as well, or miss a line that holds only a word of the quote. A caption, a footnote or a reference is outlined whole. The engine chooses one way of reading for a whole file: scanned pages in a file that has a text layer elsewhere are read line by line, which glues and splits more words.
- A code listing is read as a whole in every PDF, and each of its rows is outlined as an even slice of its box.
- A PDF with a text layer whose lines are written downwards is read badly: in the one page tried, halves of lines came out interleaved, characters were lost, and nearly all the text was marked as superscript or subscript. The same page as a scan was read well.
- A font may say that its letters reach less far above and below the line than they do: pdfTeX embeds XCharter saying its letters reach no higher than the x-height and not below the baseline. Read so, MinerU takes plain letters for raised or lowered ones and loses letters that reach below the line. In a paper of 20 pages, 5,087 letters and stops were marked as subscripts or superscripts, and "superintelligence" came out as "su erintelli ence". MinerU is therefore given a copy of such a PDF in which each such font says how far its Latin letters reach, as measured from them. The paper then came out with only its 7 footnote marks raised, and with the letters it had lost read. A font is measured by its Latin letters alone: in the samples drawn for infonium, letters in Bengali and Khmer are still taken for raised or lowered ones.
- In a line set in a typewriter face, letters that reach below the line may be read as subscripts: an address at google.com came out with both of its g's set low.
- A scan of typewritten pages with wide line spacing was read a line a block: 101 of its 108 paragraphs were single lines. Such a document is translated a line at a time, which reads poorly.
- On a page that a PDF stores turned, a word may come out split in two when the file's own figures for the turn are not exact, as pypdf's `Transformation().rotate` writes them.
- How well a page read as pictures comes out depends on its writing, and that has been tried on no more than a paragraph of each. Latin, Cyrillic and Greek writing, Chinese and Japanese came out right or nearly, and Korean, Arabic, Thai, Hindi, Tamil, Bengali and Tibetan with a few wrong characters. In writing the engine does not know, Hebrew, Georgian, Armenian, Amharic and Khmer among what was tried, it writes words that are not on the page, and may take so long that the reading is given up.
- MinerU's models want a GPU. On the machine infonium was built on, a PDF of up to ten pages took about 4 to 5.5 GB of its memory, and a scan of 40 pages about 10 GB.
- MinerU reaches a server of its own on 127.0.0.1 through a proxy set with `HTTP_PROXY`, so where one is set, `NO_PROXY` must name 127.0.0.1. Behind a proxy that refused connections, a PDF failed once MinerU had waited for its server as long as `MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS` allows (300 seconds unless set; 30 in the run), and with 127.0.0.1 named in `NO_PROXY` it was read in 16 seconds. That `ALL_PROXY` and a lowercase `http_proxy` do the same comes from reading the code.

### Recordings

- Of a video only the sound is kept. The reader plays it and does not show the picture.
- A WAV file of Vorbis sound, or of AAC sound without ADTS frame headers, is refused, since FFmpeg reads only part of such sound. FFmpeg writes both kinds when told to keep the sound of an Ogg or M4A file in a WAV. An M4A, an Ogg or an uncompressed WAV of the same recording is read.
- Speech is recognised in pieces of up to 16 seconds. A citation in a recognised recording plays from the start of the piece that holds the quote. A word at the edge of a piece is lost now and then, and each piece is punctuated by itself.
- The default speech model is less accurate in Chinese than in English.

### Translation

- Whether a document is in the language of digests already is judged by the translating model from up to 40,000 characters spread over it, by the language most of that sample is in. A document mostly in the reader's language is not translated, and its passages in other languages stay as written, unless the reader asks for it all the same (仍然翻译). A document mostly in another language is translated whole, and its passages already in the reader's language go to the model as well.
- A long document takes minutes. The 20,840 words of a Wikipedia article took 288 seconds, and a paper of 15 pages 37,000 input and 20,000 output tokens. The first blocks appear after one to two and a half minutes, most of it the survey, which lists each term with its forms and takes the longer the more it lists: on a busy day the survey of that paper took 53 to 110 seconds in nine runs, whether it read 10,000 or 39,000 characters of the paper, and the paper's first block came after 146 seconds.
- The translating model decides once for a document how its recurring names and terms are translated, and a wrong choice repeats wherever the term stands: in one Japanese article a shark was misnamed in every block that named it.
- In a technical source, which terms stay in English is the translating model's judgment, and on the borderline it differs from one translation to the next, though each keeps one way throughout: of the paper tried, one translation kept query, key and value in English and a later one translated them. A sentence can still keep a general word in English, such as state-of-the-art, or a plural, such as positional encodings. A term the source marks, as an emphasised word, can lose its space to the Chinese beside it: 3 of 145 places in the Rust chapter tried.
- A heading made only of terms of art, such as Scaled Dot-Product Attention, stays as written, and on a PDF's page it is set over the source's lines like any other block. In the paper tried one heading, Why Self-Attention, stayed in English whole.
- A translation made before this style keeps its terms translated until 全部重新翻译 is pressed; a block translated again by itself follows the terms of the translation it belongs to.
- A digest and a translation are written by different models and may give one name two translations: 吉普夫定律 in a digest, 齐普夫定律 in the translation of the same article.
- The translating model can misplace words: in a paper it swapped two formulas in one sentence. One block can be translated again by the model that writes digests.
- A link or a style is kept only where the model puts its tags back around the words that translate it. In the articles tried, 97 to 100 percent of links were kept, after asking again for any passage that lost half its marks or more.
- A block whose replies hold what is left of a tag, lose a formula's tags, put words of the sentence inside them or, in a recording, answer a sentence with the words of another is asked for again. After three attempts it stays in its source's language, and the translation shows as stopped, with how many of its blocks are translated and why the last failed. Asking for the translation again goes on with those blocks, and each of them can be translated by itself, by the model that writes digests.
- Cells of a table that hold only numbers are not translated and keep the source's way of writing numbers, such as a decimal comma.
- A line of a recording's translation ends at the end of a sentence, or at the first stretch that ends after 10 seconds. In the talks tried the longest line ran 16.5 seconds. A recording recognised without punctuation has no sentences, so every line of it is of the second kind.
- On a PDF's pages, a translation that does not fit its block's lines at their size is set smaller, down to 0.6 of it, and beyond that cut, with a mark that shows the rest when pressed. Over 11 documents and 396 areas, 108 were set smaller and 6 cut, all 6 in a typewritten scan.
- On a PDF's pages, tables, listings and blocks that are one formula stay as the page has them; a table's translation is read under 文本. Small leftovers of the source's letters may show between the translated areas of a dense or scanned page.
- A formula that KaTeX cannot set is shown as its LaTeX, and so is one that writes a command to load an address, to set a class, an id, a style or an attribute, to define a command or to write to the console. MinerU writes a subscript or a superscript inside `\text{…}` at times, which neither LaTeX nor KaTeX sets: a formula KaTeX refuses as written is tried once more with each such group (`\text`, `\textbf`, `\textit` and their kin) closed before its first script, and the rest of the group after it. Of two papers, all 100 formulas of one and all 59 of the other were set, one of these after that second try. What a formula draws outside its own box, as with a negative space, is cut, and so is a formula's height beyond 40 times the size of its text. A selection across a formula copies the characters KaTeX draws, each script on a line of its own, not its LaTeX: a wide hat is lost, and a subscript may bring a zero-width space with it.
- With a translation on show, a citation marks the translated block, or the lines of a recording, that hold the quote, not the quoted words themselves.
- The switch keeps its place from one source to the next, so a source opened while it stands on 译文 or 对照 is translated without another word from the reader.
- If infonium is killed during a translation, the requests in flight are made again when it starts; blocks already kept are not. A document whose item is read again meanwhile is not translated further.

### The chat

- infonium checks that each quote of an answer stands in the block it names, not that the statement follows from it: one answer about a story added a sum that its cited passages do not hold.
- The model now and then translates a quote instead of copying it, once in five tries of one question. Such a citation is not found, and its statement is marked 出处找不到 although the source holds what it says.
- Every question about a source sends the source's whole document to the model again: about 9,000 input tokens a question for a Wikipedia article of 165 blocks. A document too long for the model cannot be asked about, just as it cannot be digested.
- An answer about a source begins to appear after 2.5 to 5 seconds, and after up to 18 seconds when the model reasons first.
- A question about the library takes 6 to 42 seconds and 4,000 to 53,000 input tokens with the embedding engine running, measured on two libraries of nine and ten sources, and up to 80,000 tokens without it, when the model searches longer. When the endpoint stalls in the middle of an answer, as it did once in ten, it takes longer: 99 seconds that time.
- For a question about the library the model may call its tools in at most 7 rounds of at most 4 calls each. Its last request offers no tools, so it answers with what it has found.
- A question in one language does not always reach a source in another: the model does not always search in every language of the library, and a search by meaning in English did not bring a Chinese passage on the same subject.
- The model is told the titles of at most 80 sources, each cut at 120 characters; it finds the others by searching.
- Earlier questions are given to the model again only as far as 12,000 characters of them go: the latest always, its answer cut where it is longer, and the model is told how many older ones are left out.
- A conversation answers one question at a time: a second question, or clearing the conversation, is refused while it answers. A reader who leaves before an answer is complete keeps nothing of it.
- The question being typed and an answer being written are held by the open page: reloading it keeps only the answers that infonium has kept.
- While the embedding engine is away, nothing on the page says that the library is being searched by words alone.
- A digest written before infonium counted the citations it could not find shows a statement that lost all of them as 无出处, like one that was given none.

### Finding passages in the library

- A block is found by its words at the first search after its reading settles, and by its meaning once it has a vector. The blocks read last get theirs first: a page read behind 9,500 waiting blocks had them within 3 seconds.
- A heading, and any other block of fewer than 3 words (a Chinese or Japanese character counting as one), is never found by meaning, only when it holds every word of the search: 29% of the blocks of a library of 8 Wikipedia pages.
- A block that shares with a search only a word that more than a tenth of the library's blocks hold, and more than ten of them, is not found by that word.
- A short block that says what longer blocks beside it say may rank below them: a five-word Finnish answer came 6th for a question in English and 11th for one in Chinese.
- A search by meaning prefers the language it is written in: for a question in English, a Chinese passage that answered it was not among the first fifty blocks.
- While the embedding engine is away (no answer, a timeout, 429 or 5xx), searches go by words alone, and blocks get their vectors within 10 seconds of its answering again. Any other error of the engine stops the search of the library until infonium starts again.
- When another writer holds the library's file past 30 seconds, the index waits a round and tries again. Any other fault of the index stops the search of the library until infonium starts again.
- Words are found in the form written, but a Korean word also finds the forms it starts (서울 finds 서울은). Chinese, Japanese, Thai, Lao, Burmese and Khmer are found by any two or more letters side by side.
- `engines/embedding` holds the engine to 4 processors. Over 9,500 waiting blocks it took 97 seconds, and searches meanwhile took 56 ms at the median.
- The first search after starting on a large library not yet indexed waits for its words: 12 seconds for 320,000 blocks. Over 100,000 blocks, a search took 29 ms at the median by words alone and 123 ms with vectors.

### In general

- The reason shown when a call of the model or of an engine fails gives the address the call went to and what was answered.
- An address is fetched for up to ten minutes and up to 2 GiB. A video or podcast page is fetched for up to an hour, and its recording may run up to four hours and hold up to 2 GiB. What goes beyond is not read.
- The browser is given none of infonium's settings or keys, only `PATH`, the locale, the time zone and a home of its own.
- The other programs infonium starts are given none of them either, nor any variable named like a key, a token, a password or a secret: FFmpeg only `PATH`; the fetcher of video and podcast pages `PATH`, the locale, the proxies and yt-dlp's `YTDLP_` settings; MinerU `PATH`, the locale, the proxies, `HOME`, `XDG_CACHE_HOME`, `LD_LIBRARY_PATH`, its own `MINERU_` settings, the GPU's `CUDA_` settings and the `HF_`, `HUGGINGFACE_` and `MODELSCOPE_` settings of the hubs it fetches its models from. Only `file` is given all of infonium's environment: when infonium loads yt-dlp, once each time infonium starts and each time `--reload` restarts it, Python runs `file` on its own executable, and `file` reads that executable and its magic database and nothing else.
- infonium serves one person on one machine. It has no accounts and no authentication, so keep its ports off the network.

## Development

```bash
make check       # lint, types, and generated protocol files are current
make test        # the tests that need nothing outside the repository
make protocol    # regenerate protocol/openapi.json and web/src/protocol.d.ts
```

The HTTP protocol is defined by the models in `src/infonium`. `protocol/openapi.json` and `web/src/protocol.d.ts` are generated from them and never edited by hand.

`make test` runs the tests in `tests` without the network, a browser, MinerU, FFmpeg, a speech recogniser, an embedding engine or a model: stand-ins answer for the model and the embedding engine, and a test that opens a network socket or looks up a name fails. It takes two to three minutes. A test that needs one of these programs carries its name as a marker and runs when the marker is named; it is skipped when the program is not found the way infonium finds it:

```bash
uv run pytest -m chrome    # Chrome or Chromium, by INFONIUM_BROWSER
uv run pytest -m ffmpeg    # ffmpeg and ffprobe
uv run pytest -m mineru    # MinerU 3.4.2, by INFONIUM_PDF_ENGINE, on a GPU
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

The UI sets formulas with [KaTeX](https://katex.org/), under the MIT license, and serves its stylesheet and fonts itself.
