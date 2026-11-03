# Generated image labels

`generate_image` processes PNG, JPEG and static WebP responses before writing
files. Text-to-image and image-to-image use the same implementation.

- The visible `AI 生成` label appears in a white footer below the original image.
  Width is unchanged; height increases. PNG source pixels and transparency are
  preserved after EXIF orientation is normalized. JPEG is re-encoded at quality 95; WebP uses lossless encoding.
- Published images retain the AI label even when `watermark=false` is passed.
  Intermediate assets (`publish_artifact=false`) can disable the footer for
  embedding into a larger deliverable. They still receive embedded metadata.
- Custom watermark text is supplementary; it cannot replace the AI label.
  If the custom text does not fit, the footer keeps only the AI label.
- XMP contains the TC260 AIGC JSON property in JPEG APP1, PNG iTXt and WebP XMP.
  PNG also keeps the legacy `AIGC` text entry for existing readers. The service
  code matches raccoon-lab's export implementation. New content/propagation IDs
  are UUIDs; upstream producer IDs from existing AIGC XMP or PNG text are retained.
  Unrelated XMP properties, EXIF, ICC profiles and PNG text are preserved.
- Result `width`/`height`, byte count and the artifact digest describe the saved
  file. `size` remains the provider request size. `aigc_metadata_applied` and
  `watermark.metadata_applied` expose whether embedded labeling succeeded.

The XMP layout follows TC260-PG-20259A section 6 (source: 全国网络安全标准化技术委员会秘书处).
The implementation uses the namespace spelling with a trailing slash shown in
its code example and also reads the spelling without a trailing slash.

## Existing fallback boundaries

SVG, GIF and animated images are not processed. As before, unavailable Pillow,
invalid image bytes return the original bytes with failure status rather than
failing generation. Once a valid static image is decoded, rendering or metadata
encoding failures (including invalid XMP) fail the tool before publishing or
overwriting any file. Callers must inspect both label statuses;
a successful generation response alone does not guarantee labels were applied.

Tests cover saved-file metadata, footer pixel isolation, alpha, upstream XMP,
legacy PNG provenance, narrow images, disabled intermediate footers, publication,
actual dimensions and artifact digests. A rebuilt runtime and a fresh host task
are required to verify packaged desktop behavior.
