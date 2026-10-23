# Public PST regression fixture

`testPST.pst.gz` is a lossless gzip copy of Apache Tika's public `testPST.pst` test fixture:
https://github.com/apache/tika/blob/main/tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents/testPST.pst

Original SHA-256: `f2a6b1d2cad00f574e3d1c1211c4b1c854d6526caea77213adc3da92b7813ae3`.
Retrieved 2026-10-23. Copyright The Apache Software Foundation, distributed under the Apache License 2.0 (see `LICENSE.apache-tika`). This is public test data, not a user mailbox. Compression only reduces repository size; tests decompress it into their temporary workspace and exercise the real pypff parser offline.
