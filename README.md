# infonium

infonium digests what you read, watch and listen to into a summary and takeaways in your language. Every statement links back to the passage of the source that supports it. One click takes you to the sentence in the article, the lines on the PDF page, or the moment in the recording. It also translates a source into your language in the source's own form, and answers questions about one source or about everything you have read, with the same links. You mark passages and write notes that link back to them, and both outlive a second reading of their source.

## What it reads

| Source | Add it by | Read through |
| --- | --- | --- |
| Web article | pasting its address, or uploading the page saved from your browser (for a site that turns infonium away) | the page's HTML, and the page as a browser builds it when its HTML holds little text |
| PDF, with a text layer or scanned | uploading the file or pasting its address | [MinerU](https://github.com/opendatalab/MinerU), which also recognises the text of scanned pages |
| Video or podcast page, such as YouTube, Bilibili, TED, Apple Podcasts and SoundCloud | pasting its address | the page's own captions, or speech recognition when it has none |
| Audio or video file | uploading the file or pasting its address | speech recognition |

A PDF opens as its pages, with the text read from them one tab away. A web article opens as its text, with the page it was read from one tab away. A recording opens as a player above its transcript.

## What it does with a source

A column beside the reader holds three tabs: the digest (摘要), a chat (对话), and a notebook (笔记).

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

### The notebook

The notebook holds your highlights (高亮) and your notes (笔记). It can be opened with a source on show or with none.

A highlight is a passage you marked, with a comment if you wrote one. Select words in a source and choose 高亮, 批注 (mark and comment) or 记到笔记 (put the passage into a note). A passage may run over several paragraphs. You can select in every view:

| View | How a passage is selected |
| --- | --- |
| The text of any source, and a recording's lines | By selecting its words. With only a translation on show, the selection marks the whole sentences or paragraphs it touches. |
| A PDF's pages | By dragging over the lines. Over a translation set on the page, by selecting its text, which marks the whole blocks it touches. |
| A web article's own page | By selecting its words in the page. |

A highlight is drawn in every view of its source. Pressing one opens it in the notebook, and pressing its words in the notebook shows the passage in the source. With no source open, the notebook lists the highlights of every source.

A note is formatted text that belongs to the library, not to one source. A link in a note may lead to a passage of any source, and pressing it opens the passage. A statement of a digest, a statement or a whole answer of the chat, a highlight and a selection can each be put into the note being written (记到笔记), with their citations as such links. A highlight goes in with its comment, written as plain text after the quotation and its link; a comment still being typed is kept first. Beside a statement the control is the ✎ that shows when the statement is pointed at. A note is kept by itself a moment after you stop typing, and a new note is kept once something is written in it. The note open in a tab is open again when the tab is loaded again.

### Reading a source again

The reader's head offers to read a source again (重新读取): a link is fetched again, and an upload is read again from the file infonium keeps. A reading that gives other text than the readings before it is kept as a new revision, and every earlier revision stays. The head lists the revisions, and an earlier one opens as its text.

Your highlights, the passages your notes link to and the citations of earlier answers are looked for in the new reading. A passage that is found is shown where it stands now; where its words differ a little, the notebook says so. A passage the new reading no longer holds is said to be lost, with the revision it was marked in, and opens in that revision.

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

Highlights and notes are kept in the library's file. Each holds passages, and a passage is an anchor for each block it runs through, all in one document. A highlight is one passage with a comment of up to 10,000 characters; marking the same passage again gives the same highlight. A note is a title of up to 200 characters and up to 500,000 characters of Markdown. A link in the Markdown whose address is `passage:` and an id names one of the passages kept beside the note, each with the source it lies in. A note belongs to the library, not to one source, and its links may lead into any source. A highlight put into a note leaves its comment there as a copy in the note's Markdown: changing the comment afterwards leaves the note as it is, and editing the note leaves the comment.

A document's identity follows from its item, the program that read it, and its blocks with their places. A reading that gives the same blocks gives the same document and adds no revision; one that gives a document the item held before returns the item to that revision, under its number. Any other reading is kept as the next revision, numbered from 1 in the order the revisions were first read. Every revision is kept with its document and its snapshot. A highlight and a note's passage stay anchored in the revision they were marked in.

Where a passage stands in the document its item holds now is worked out each time it is shown, and never stored. A passage is looked for by its letters: its characters as a citation's quote is compared, without white space, block after block, so that neither spaces nor the places where blocks are cut decide what is found. A passage of 40 letters or more, each character of a script written without spaces counting 3, is found by its letters alone where each revision holds it exactly once. Any other passage must have both of its sides borne out. A side is borne out by the 10 letters right beside it, which must stand beside the place again with one letter read otherwise at most and no digit changed, or by the edge of its block: the passage began or ended fewer than 10 letters from that edge, the letters between stand as they were, and the place lies as far from the edge of its own block. At least one side must be borne out by letters. Footnote marks are left out of these letters, and new blocks of up to 16 letters, such as the link to edit a heading that a later reading of a wiki puts after it, are passed over.

In a table, a place may run across no more edges of cells and rows than the passage was marked across, and a cell is told by the letters of its own row. A passage that began and ended at the edge of a word is found only where it does so again, and a number is never found inside a longer number. Where the new revision does not hold the passage's very letters so placed, the passage is looked for among letters a little unlike them: four in five of its letters must stand after the fewest changes, no digit may change, the letters found must not have stood elsewhere in the revision the passage was marked in, and its sides must be borne out as above. For a passage of 40 letters or more that its revision held once, one side is enough, borne out by letters or by the document's edge, where its own first and last 10 letters stand at the ends of the place; no such place is taken outside the passage's own place while that still stands between the blocks that stood before and after it. A passage over several blocks between which new blocks came to stand is found block by block, with at most 2 new blocks of up to 16 letters between two of its blocks. A passage found as its very letters, spaced as they were, is shown as it is; one found as letters a little unlike, or with two of its words now glued together, is shown with the words 文字与标记时略有不同.

Where the revision a passage was marked in held other copies of it, or the new revision holds its letters in several places, the letters around each place are compared with those that stood around the marked copy, 32 on each side and up to 256 where those are the same, and the passage is found only at a place that they tell from every other. Where nothing tells the places apart, the passage is lost. Every rule leans the same way: a passage shown at a wrong place is worse than a passage said to be lost, which still opens in the revision it was marked in.

In the text of a source and in a recording's lines, highlights are drawn through the browser's registry of highlights, over the words, and nothing is written into the page. A selection becomes a passage from its two ends, by the same walk over each block's text that resolves a citation. With only a translation on show, it takes whole every sentence or paragraph it touches; with both on show, an end that lies in a translation takes the whole of that sentence or paragraph. On a PDF's pages, highlights and selections follow the document's line boxes. Each line is cut at the gaps between its words, read off the page as it is drawn: a page that holds highlights is read when it is drawn, any other at the first press on it. A line whose gaps cannot be read is cut by the widths of its characters, and a line of a script written without spaces into equal shares. A table, a figure and a box that stands for several lines are taken whole. Where the engine broke a scan's line inside a word, the break is moved, by 15 characters at most, only where both lines it bounds then hold as many words as their print shows. In the kept page of a web article, the characters an element shows are matched with its block's text, apart from white space, case and the shapes of quotation marks and dashes, and that one correspondence places a citation's passage, draws the highlights and turns a selection into a passage. Text the page holds but does not show is left out of it.

A note is written in an editor built on [TipTap](https://tiptap.dev/), fetched when a note is first opened, which writes Markdown. A note is handed to infonium 800 ms after the last key, and at once when the reader leaves it, the item or the page. Each writing carries an edition, one after the edition infonium last showed, and infonium takes a writing only when its edition is later than the one it holds. A reader whose note was written elsewhere meanwhile is told so, and nothing more is sent until they choose: to replace that writing with their own (用这里的替换那边的), to keep their own as a new note (另存为新笔记), or to drop their own and read in the other (放弃这里的，读入那边的).

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

### Highlights

- In the text, a press opens a highlight 250 ms later, so that a double click selects a word instead. A slow double click, its presses 300 ms apart or more, opens the highlight and selects the word as well.
- In the text, a highlight made with only a translation on show, or with an end in a translation, covers each piece of the source it touches whole: a sentence of a recording, a paragraph of anything else, and a table for one of its cells.
- The notebook was tried in Chrome only. Pressing a highlight in the text opens it only in a browser that tells the words under a point, Chrome from 128 on. Selecting in the text by keyboard needs the browser's caret browsing (F7 in Chrome).
- In a narrow window, when a selection ends at the window's foot, its actions stand above its last line and cover the line before it.
- On a scan, the ends of a highlight on the pages are read off the print where the reading engine's line breaks agree with it. Where the engine broke a line inside a word, a highlight may be drawn up to a word away from its words: 7 of 42 random highlights on seven test scans.
- On the lines whose gaps could be measured, of five scans and three PDFs with a text layer, a word's edge lies a median 0.03 to 0.06 of a character from the print. It lies more than two characters off at no edge of the three PDFs with a text layer, at 1.8% of edges at most on four of the scans, and at 13.7% on the fifth, whose blocks the engine split into lines of even length.
- A line whose print is not dark on light paper, such as light text on a dark page or grey text, is cut by the widths of its characters, which on scans puts a word's edge a median 0.9 to 2.6 characters off.
- A highlight on a page not yet read is first drawn by the widths of its characters, and moves once the page is read: in the case watched, 2 seconds later and by about two characters.
- In the one PDF tried whose text layer runs downwards, the engine's line places do not match its columns, and highlights are drawn away from their words.
- On a PDF's pages, select-all (Ctrl+A) hands over every block of the document, and 高亮 then marks all of it.
- In the kept page (原件), a highlight in a block that has no element of its own there is not drawn, and the line above the page says how many such highlights there are. Where an element shows a translation, or the highlight's words are not found in it, the element is tinted whole.
- In the kept page, words the page holds but does not show, such as text for screen readers, are never part of a highlight, and words it shows struck out inside a highlight are marked with it.
- A link in the kept page opens only on a press that moved 4 pixels or less, or by Enter. A drag that starts on a link selects its words, unless it starts on the left half of the link's first character, where the browser begins no selection. No link can be dragged out of the page, and links the page made unselectable can be selected.
- A highlight in the kept page keeps its light green on a dark page and in the dark theme.
- Marking by touch was tried only in emulation, not on a phone or with a pen. On a PDF's pages, a finger held still for 450 ms marks the whole block it rests on and does not select words. In the kept page, touch was not tried.
- A comment typed and not yet kept survives turning to another item, but is lost when the page is loaded again.
- A page open in a second tab shows a highlight made, changed or removed in the first once that tab is turned to, not before.
- In 对照 in the text, a highlight is drawn on the source's words, not on the translation beside them.
- A passage that runs through a formula is quoted in the notebook and in a note with the formula's LaTeX.
- 500 highlights on one item added 60 to 85 ms to opening it in Chrome, and about 0.5 seconds with the processor slowed four times.
- The notebook was not tried with a screen reader.

### Notes

- Editing a note changes what the editor cannot show: a table is dropped, a picture keeps only its alt text, and HTML becomes marks or text. When the note is opened, it says which of these, or footnotes, it holds.
- The first writing of a note puts some Markdown in another form of the same meaning, and from then on it stays as written: a `*` bullet becomes `-`, `_x_` becomes `*x*`, an underlined heading one marked by `#`, an indented listing a fenced one, a mark inside a word is escaped (`snake\_case`), and a bare address becomes a link.
- A link to a passage inside bold or italic loses that mark when the note is edited.
- Two writers saving the same note at the same moment, one of them as its page is hidden or closed, can lose the other's last change, and neither is told.
- When a writing reached infonium but its answer was lost, the note says it was written elsewhere though nothing was lost; 用这里的替换那边的 sets it right.
- A writing of more than 64 KiB cannot be sent as the page closes. The browser asks first; a reader who leaves all the same may lose what was typed last.
- When infonium refuses a passage of a writing, such as one pasted from elsewhere that its source does not hold, every link new in that writing stands as plain words, and the note names the refused one.
- The editor is fetched when a note is first opened: 481.54 kB of script, 153.01 kB gzipped.
- A note of 50 passages in 20 items of 2,000 blocks each opened in 157.5 ms the first time and in 8.0 ms once those documents were read. infonium keeps the 64 documents read last.
- A screen keyboard was tried only as a window of 390 by 420 pixels, and the editor was not tried with a screen reader.
- A comment written from right to left stands aligned to the left in a note, as every paragraph of a note does, though on its highlight it stands to the right.

### Reading a source again

- A passage whose words still stand may be said to be lost: most often a passage of few words whose neighbours changed, a cell of a changed table, or text that a recognition repeats. Of 100 passages of each of three Wikipedia articles read in two revisions, 2, 2 and 0 were. Of 100 passages of each of two PDFs of many scripts, read from their text layer and again as pictures, 7 and 18 were. Of 2,400 passages of twelve documents read again with their blocks cut and joined, 50 were; with misreadings added, 86; with 15% of their blocks also rewritten or removed, 97. Of 600 passages of each of four pairs made from the Rust and 東京都 Wikipedia articles, with blocks removed, rewritten and copied, 23 to 26 were.
- None of the passages above was shown at a place that does not hold its words. A wrong place is still met now and then in documents made up to say the same rows, sentences and formulas again and again with small changes: in 4 of 11 runs of the test that mixes such documents, one passage was found away from its own place, such as a cell of a removed row found in another row, or a passage across two blocks found a little unlike itself on a neighbouring sentence of the same form. Where identical rows or sentences stand side by side and one is removed, a passage of the removed one is shown in one that stayed.
- In the same made-up documents, a sentence that every section says, in a block of its own, is often lost when the section beside it was removed or rewritten, though its own block stands word for word.
- Where a paragraph was rewritten or removed and a new paragraph that says the same but for one word was put elsewhere, a mark on part of the first is often shown on the second, with the words that say it differs a little: in a test made for it, 72 of 166 and 55 of 146 passages whose paragraph was rewritten, and 83 of 180 and 77 of 176 whose paragraph was removed.
- A passage of 40 letters or more that each revision holds once is found wherever it stands: where its own paragraph was removed and the same words stand in another, it is shown there.
- A passage is lost where a digit of it other than a footnote mark's changed, however little else did, and where more than one letter in five changed: of 100 passages of a PDF in Hebrew, Georgian, Armenian, Amharic and Khmer, read again as pictures, 41 were lost because the recognition wrote other text.
- The notebook works out where each highlight stands each time it lists them. After a second reading of a made-up page of 2,001 blocks and about a million characters, a list of 50 highlights took 0.25 to 0.36 seconds, one of 300 took 1.3 seconds and one of 500 took 2.1 seconds. A highlight of a single common word costs more: with ten of them among the 50, the list took 0.7 seconds. One passage took 0.9 seconds at most, on a megabyte of text that says one row of a table ten thousand times.
- A passage that its document says more than 2,000 times is lost, once anything in the document changed, when its own copy comes after the 2,000th.
- Every revision is kept with its document and its snapshot, and none is ever removed.
- Where a new paragraph of a few words came to stand inside a passage that runs over several paragraphs, the passage is found with the new paragraph inside it.
- A passage that begins with a combining mark set after white space is lost even in a reading that changed nothing: 15 of 3,988 such made-up passages.
- An earlier revision is shown as its text only, without its pages, its kept page, its player or a translation, and nothing can be marked in it.
- An upload whose reading never succeeded cannot be read again; it has to be uploaded again.
- In a library kept before revisions, an item's earlier readings are numbered in the order they were last read, not first read.

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
- An address is fetched for up to ten minutes and up to 2 GiB, and given up once its server sends nothing for 20 seconds. A video or podcast page is fetched for up to an hour, and its recording may run up to four hours and hold up to 2 GiB. What goes beyond is not read.
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

`make test` runs the tests in `tests` without the network, a browser, MinerU, FFmpeg, a speech recogniser, an embedding engine or a model: stand-ins answer for the model and the embedding engine, and a test that opens a network socket or looks up a name fails. It takes about four minutes. Three tests of where a passage stands after a second reading are marked as failing at times, each with the limit it stands for. A test that needs one of these programs carries its name as a marker and runs when the marker is named; it is skipped when the program is not found the way infonium finds it:

```bash
uv run pytest -m chrome    # Chrome or Chromium, by INFONIUM_BROWSER
uv run pytest -m ffmpeg    # ffmpeg and ffprobe
uv run pytest -m mineru    # MinerU 3.4.2, by INFONIUM_PDF_ENGINE, on a GPU
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

The UI sets formulas with [KaTeX](https://katex.org/), under the MIT license, and serves its stylesheet and fonts itself.
