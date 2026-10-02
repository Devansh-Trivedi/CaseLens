# CaseLens — the case, in focus

A local, read-only Clio workbench: a cited 90-second attorney briefing, dated evidence timeline, searchable source library, and an attorney-selected provider-update draft. React frontend + FastAPI backend. Clio remains the system of record.

## Run on this Mac

```bash
cd "/Users/devanshtrivedi/Documents/LDG Hackathon/CaseLens"
bash start.sh
```

Open **http://127.0.0.1:8000/**. Keep the terminal open. Use the app homepage, not `/docs`, `/digest`, or the old standalone prototype.

1. Click **Connect Clio**. Sign in to the hackathon Clio account if asked. This app uses the credentials already in `.env`; the registered redirect must be `http://127.0.0.1:8000/auth/clio/callback`.
2. Select the matter and click **Sync & brief**. The first import downloads and extracts documents, including scanned PDF pages with macOS Vision where available. Progress and incomplete collections are visible.
3. For AI synthesis, open **AI settings**, add your OpenAI API key, save, then click **Regenerate AI brief**. This sends extracted case text to OpenAI and incurs API usage. Use only hackathon data or data you are authorized to send. Without a key, the app intentionally provides labeled source excerpts, not a pretend AI summary.
4. Click **Source** beside a briefing claim to inspect the supporting saved record/page. Explore **Timeline** and **Evidence library**.
5. In **Provider update**, select exactly which case fields may be shared, inspect the draft, and optionally download it. Nothing is emailed or published.

Clio OAuth tokens and snapshots persist privately in `.local/caselens.sqlite3`. A restart no longer requires reauthorization unless Clio rejects the token. The app refreshes expiring tokens. `.env` and `.local/` are ignored by Git. Rotate or revoke development credentials after the event.

## 90-second judge demonstration

- **0–15 sec:** “An attorney inherits a matter scattered across documents, calls, notes and deadlines. CaseLens makes the next case review evidence-first.”
- **15–45 sec:** Show the synced attorney briefing: case position, treatment, outstanding issues and next actions. Click a citation and show its original source text.
- **45–65 sec:** Show the timeline and coverage report. Explain that missing permissions or unread pages remain visible rather than becoming false certainty. A second sync highlights actual changes against the first snapshot.
- **65–85 sec:** Select two provider-safe fields, preview the update, and point out that case strategy stays out unless the attorney selects it. No automatic sending.
- **85–90 sec:** “Clio holds the record. CaseLens provides the review layer, with evidence and attorney control.”

The “90 seconds” refers to reading the concise briefing, not a promise that the first import or OCR finishes in 90 seconds. The AI briefing is capped at 225 words (150 words/minute estimate); actual reading time varies.

## Architecture and safeguards

- Read-only Clio API: matter, custom fields, notes, communications, tasks, documents, calendar, activities/expenses, invoices and associated contacts. All collection pages are fetched; explicit cross-matter records are excluded.
- PDF text/OCR, DOCX and plain text extraction; source record IDs and PDF page numbers are retained. Unsupported content is flagged, not invented. Text-only extraction does not interpret photographs or diagrams.
- Optional AI map/reduce sends all extracted source text in bounded groups, validates structured output and source IDs, then produces a concise brief. Citation existence is checked; that does not prove an interpretation is correct. Attorney verification remains essential.
- Source excerpt fallback is explicitly labeled. Missing collections have unknown counts, not zero. Failures do not overwrite the previous snapshot with an empty case.
- Snapshot diffs indicate added/updated/removed evidence, not legal conclusions. First sync establishes a baseline.
- Local-only single-user prototype, bound to 127.0.0.1. No public deployment, multi-firm authorization, encryption at rest, case valuation engine, legal-deadline calculator or automated provider messaging. Add these controls before using production client data.
- API keys are stored locally, never returned to the browser. The local SQLite file is access-restricted but is not encrypted. OpenAI responses use `store: false` (not a guarantee of zero retention under all provider policies).

## Development and tests

Python 3.10+; Node.js is needed only to rebuild the frontend. Built frontend assets are included for easy startup.

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend
npm install
npm run build
```

Restart the backend after backend changes. Do not expose port 8000 publicly. `.env.example` documents settings. The demo does not write to Clio even if your test app has broader OAuth scopes.

Optional CPU OCR fallback (already set up on this Mac) uses [RapidOCR](https://github.com/RapidAI/RapidOCR) locally. On another machine with Python 3.12:

```bash
python3.12 -m venv .local/ocr-env
.local/ocr-env/bin/python -m pip install -r backend/requirements-ocr.txt
```

Sync again after installing OCR. Previously unread pages are retried automatically. OCR text is still fallible and should be checked against the original page.

## Troubleshooting

- **No matters / 401:** click Reconnect and use the same Clio account that contains the seeded matter.
- **403:** enable the required read permissions on your Clio developer app, save, then reconnect so the authorization reflects them.
- **Import issues:** expand the coverage warning for the exact collection. Fix access and sync again. A green setup banner does not prove every seeded expense imported successfully.
- **AI billing/model error:** source excerpts remain available. Check your API key, provider billing and model access, then regenerate.
- **Port already in use:** stop the old CaseLens server in its terminal with Ctrl+C, then run the command above. The launcher will not kill unrelated processes.
- **AI shows older facts:** inspect the citations, relevant dates and newer sources. This is a review assistant, not a substitute for attorney judgment.
