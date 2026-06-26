# Attachment fixture pack

Real, valid files in their native formats — used by the **Cătălina**
persona (defined in `qa_agents/personas.py`) and by any harness test that
needs a genuine attachment to exercise the inbound pipeline + provider
adapters end-to-end.

These files are deliberately small, hand-crafted, and synthetic. They
*are* real PDFs / DOCX / XLSX / images (the providers ingest them
correctly) — no fake bytes pretending to be the format — but the
content is fictional so there's no embedded PII and we know exactly
what's in each file.

## What's in the pack

| File | Size | Format | What it's for |
|---|---|---|---|
| `sample-invoice.pdf` | ~2 KB | PDF | Small, native-PDF text. Should be **read by the provider** on Pro/Power per #707/#800. |
| `sample-invoice-large.pdf` | ~3.8 MB | PDF (with embedded random-noise image) | Realistic "bookkeeper attachment" size. Comfortably under Pro's 10 MB cap; the harness pads this at runtime to test the cap-boundary cases (~9 MB pass, ~12 MB Pro reject, ~24 MB Power pass, ~27 MB Power reject). |
| `sample-report.docx` | ~37 KB | Microsoft Word (Open XML) | Word docs aren't natively ingested — should bounce back with the **honest "Attachment(s) not read"** disclosure on Pro/Power. |
| `sample-figures.xlsx` | ~5 KB | Microsoft Excel (Open XML) | Same as DOCX — honest disclosure expected, not silent drop. |
| `sample-expenses.csv` | ~300 B | Plain text CSV | Same — honest disclosure expected. |
| `sample-receipt.png` | ~9 KB | PNG image | Native image ingestion — should be **read by the provider**. |
| `sample-receipt.jpg` | ~9 KB | JPEG image | Native image ingestion — should be **read by the provider**. |
| `sample-empty.txt` | 0 B | Plain text | Edge case: 0-byte file. Tests whether the size-zero path is handled gracefully or 500s. |
| `sample-bundle.zip` | ~3 KB | ZIP archive containing 2 small PDFs | Edge case: compound type. Does the pipeline unpack the zip, treat it as a single unsupported attachment, or something else? |
| `sample-mislabeled.pdf` | ~37 KB | DOCX bytes with a `.pdf` extension | Edge case: content-type / extension mismatch. Tests whether the pipeline trusts the filename or sniffs the actual bytes. Real-world users do this all the time. |

## Regenerating

If you change the persona's test scenarios and need different fixtures,
edit `generate.py` and run:

```bash
uv run --with reportlab --with python-docx --with openpyxl --with Pillow \
    qa-agents/harness/qa_agents/fixtures/attachments/generate.py
```

The script is idempotent — running it overwrites the existing files
deterministically (except `sample-invoice-large.pdf` which embeds
random noise, so its bytes differ between runs but its size stays in
the same band).

## What's deliberately NOT in the pack

- **Files at the exact boundary of the per-tier caps** (e.g. an 11 MB
  PDF to test Pro's 10 MB reject). Reason: committing many multi-MB
  files inflates the repo for marginal test value. The harness
  constructs boundary cases at runtime by padding
  `sample-invoice-large.pdf` (a single base "large" file is enough).
- **HEIC / WEBP / RAW** image formats. The honest-disclosure layer
  treats them the same as DOCX (not-natively-ingested) and the
  pipeline doesn't care which not-natively-ingested type it sees;
  PNG + JPEG cover the native-ingest path, plus DOCX/XLSX/CSV/ZIP/empty
  cover the not-ingested path. Adding more formats is diminishing
  returns.
- **Password-protected PDF**, **virus-infected file (EICAR test
  pattern)**. Both worth testing eventually but neither is in the
  bookkeeper's day-to-day. File a follow-up if the persona's run
  surfaces a real gap.

## Where this fits

The fixture pack is loaded by the harness when running Cătălina (and
any other test that imports it). It is not currently loaded by any
backend test in `backend/tests/fixtures/` — those have their own tiny
PNG + tiny PDF for unit-level provider-adapter tests (see PR #800).
Two fixture packs, two different abstraction layers:

- `backend/tests/fixtures/` — unit-test inputs for provider-adapter code.
- `qa-agents/harness/qa_agents/fixtures/attachments/` (this dir) — real
  attachments sent over SMTP in end-to-end persona runs.
