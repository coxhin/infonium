# Test fixtures

Everything here was made for infonium's tests. No page, document or recording of a third party is among it.

- `pdf/samples`: 16 PDFs drawn for infonium (7.1 MB). Between them they hold text layers and scans, a made-up article and magazine page, tables, code and marks, a dozen writing systems, vertical Japanese, pages stored turned, a file with a text layer on some pages only, and the cases earlier reviews found.
- `pdf/engine`: what MinerU 3.4.2 wrote for each sample when called as infonium calls it (52 KB, gzipped). It holds the results the reader reads, `*_middle.json`, and for a file read as pictures the lines it detected, `*_model.json`. The engine's pictures of the pages are left out.
- `pdf/readings`: the title and blocks that commit 6facef0 read from each sample (226 KB). A fresh run of the engine gave the same on 2026-11-21.
- `pdf/handed.json`: the engine's options, and for each sample the SHA-256 of the file the reference handed the engine, without the identifier in its trailer.

Eight samples of the set the reference was made from are left out to keep the data small: the 40-page scan, two searchable scans, a scan and a born-digital file with text in pictures, the drop-cap cases, a second scan of the article, and the scan of the writing systems.
