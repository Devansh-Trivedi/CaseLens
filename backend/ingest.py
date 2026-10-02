import asyncio
import hashlib
import html
import io
import json
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import pymupdf
from fastapi import HTTPException
from . import clio, store


def plain(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    value = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', '', str(value))
    return html.unescape(re.sub(r'<[^>]+>', ' ', value)).strip()


def extract(data, name):
    """Return pages with honest extraction status, including macOS Vision OCR."""
    lower = name.lower()
    if data[:4] == b'%PDF' or lower.endswith('.pdf'):
        pages = []
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            for number, page in enumerate(pdf, 1):
                text, method = page.get_text(), 'text'
                if len(text.strip()) < 30 and page.get_images():
                    method = 'needs_ocr'
                    with tempfile.TemporaryDirectory(prefix='caselens-ocr-') as directory:
                        image = Path(directory) / 'page.png'
                        page.get_pixmap(dpi=150).save(str(image))
                        commands = []
                        if (store.DATA / 'ocr').exists():
                            commands.append([str(store.DATA / 'ocr'), str(image)])
                        fallback = store.DATA / 'ocr-env' / 'bin' / 'python'
                        if fallback.exists():
                            commands.append([str(fallback), str(store.ROOT / 'backend' / 'ocr_fallback.py'), str(image)])
                        for command in commands:
                            try:
                                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                                if result.returncode == 0 and result.stdout.strip():
                                    text, method = result.stdout, 'ocr'
                                    break
                            except (subprocess.TimeoutExpired, OSError):
                                continue
                pages.append({'page': number, 'text': text.strip(), 'method': method})
        return pages
    if lower.endswith('.docx'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read('word/document.xml'))
            text = '\n'.join(el.text or '' for el in root.iter() if el.tag.endswith('}t'))
        return [{'page': 1, 'text': text, 'method': 'docx'}]
    if lower.endswith(('.txt', '.csv', '.eml', '.html')):
        return [{'page': 1, 'text': plain(data.decode('utf-8', errors='replace')), 'method': 'text'}]
    return [{'page': 1, 'text': '', 'method': 'unsupported'}]


def source(key, kind, title, text, date=None, record=None, page=None, date_type='record date'):
    return {'key': key, 'kind': kind, 'title': plain(title), 'text': plain(text),
            'date': date, 'date_type': date_type, 'record_id': record, 'page': page}


async def sync(matter_id, progress):
    progress('Reading matter details', 3)
    matter = (await clio.get(f'matters/{matter_id}.json', {'fields': clio.MATTER_FIELDS}))['data']
    resources, coverage = {}, {}

    async def load(kind, definition):
        path, fields = definition
        try:
            params = {'matter_id': matter_id, 'fields': fields}
            if kind == 'notes':
                params['type'] = 'Matter'
            rows = await clio.collection(path.format(matter_id=matter_id), params)
            # Never import a record explicitly associated with a different matter.
            rows = [r for r in rows if not r.get('matter') or str(r['matter'].get('id')) == str(matter_id)]
            resources[kind] = rows
            coverage[kind] = {'status': 'complete', 'count': len(rows)}
        except Exception as exc:
            resources[kind] = []
            coverage[kind] = {'status': 'unavailable', 'count': None,
                              'message': str(exc.detail) if isinstance(exc, HTTPException) else 'Request failed; retry sync.'}

    await asyncio.gather(*(load(k, v) for k, v in clio.COLLECTIONS.items()))
    progress('Indexing notes, messages, deadlines and financial fields', 18)
    sources = [source('matter', 'matter', matter.get('display_number'), matter.get('description'), record=matter_id)]
    sources.append(source('matter:status', 'matter', 'Current case stage',
                          f"Status: {matter.get('status')}; Stage: {(matter.get('matter_stage') or {}).get('name', 'Unassigned')}", record=matter_id))
    for field in matter.get('custom_field_values', []):
        if field.get('value') is not None:
            sources.append(source(f"field:{field['id']}", 'field', field.get('field_name', 'Custom field'), field['value'], record=field['id']))
    definitions = {
        'notes': ('subject', 'detail', 'date'),
        'communications': ('subject', 'body', 'date'),
        'tasks': ('name', 'description', 'due_at'),
        'calendar': ('summary', 'description', 'start_at'),
        'activities': ('type', 'note', 'date'),
        'bills': ('number', 'subject', 'issued_at'),
        'contacts': ('name', 'relationship_name', None),
    }
    for kind, (title, body, date) in definitions.items():
        for row in resources[kind]:
            txt = plain(row.get(body))
            if kind == 'tasks':
                txt = f"Status: {row.get('status')}. Due: {row.get('due_at') or 'Not set'}. " + txt
            if kind in ('activities', 'bills'):
                txt += f"\nRecorded total: {row.get('total', 'Not provided')}"
            sources.append(source(f"{kind}:{row['id']}", kind, row.get(title) or kind.title(), txt,
                                  row.get(date) if date else None, row['id'], date_type='due date' if kind == 'tasks' else 'event date'))

    documents = resources['documents']
    pages_read = pages_missing = ocr_pages = 0
    for i, doc in enumerate(documents):
        progress(f"Reading document {i+1}/{len(documents)} · {doc.get('name', '')}", 20 + int(65 * i / max(len(documents), 1)))
        cache_key = f"document:{doc['id']}:{doc.get('etag') or doc.get('updated_at')}"
        parsed = store.get(cache_key)
        if not parsed or any(p['method'] in ('needs_ocr', 'unavailable') for p in parsed):
            try:
                data = await clio.document_bytes(doc['id'])
                parsed = await asyncio.to_thread(extract, data, doc.get('filename') or doc.get('name', ''))
                store.put(cache_key, parsed)
            except Exception as exc:
                doc['extraction_error'] = str(exc.detail) if isinstance(exc, HTTPException) else 'Text extraction failed.'
                parsed = [{'page': 1, 'text': '', 'method': 'unavailable'}]
        doc['pages'] = len(parsed)
        doc['text_status'] = 'complete' if all(p['method'] not in ('needs_ocr', 'unsupported', 'unavailable') for p in parsed) else 'partial'
        for page in parsed:
            if page['method'] in ('needs_ocr', 'unsupported', 'unavailable'):
                pages_missing += 1
            else:
                pages_read += 1
            ocr_pages += page['method'] == 'ocr'
            key = f"documents:{doc['id']}:p{page['page']}"
            src = source(key, 'documents', doc.get('name', 'Document'), page['text'], record=doc['id'], page=page['page'])
            src['extraction'] = page['method']
            sources.append(src)
    coverage['document_text'] = {'status': ('unavailable' if coverage['documents']['status'] != 'complete' else 'partial' if pages_missing else 'complete'), 'count': pages_read,
                                 'pages_unread': pages_missing, 'ocr_pages': ocr_pages}
    if coverage['documents']['status'] != 'complete':
        coverage['document_text']['message'] = 'Document listing unavailable; text coverage is unknown.'
    previous = store.get(f'case:{matter_id}')
    def fingerprints(items):
        return {s['key']: hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest() for s in items}
    current = fingerprints(sources)
    old = fingerprints(previous['sources']) if previous else {}
    # Failed groups are unknown, not deleted records.
    fully_read = {k for k, v in coverage.items() if v['status'] == 'complete'} | {'matter', 'field'}
    if coverage['document_text']['status'] != 'complete':
        fully_read.discard('documents')
    changes = {'baseline': previous is None, 'previous_sync': previous.get('synced_at') if previous else None,
               'added': [k for k in current if k not in old] if previous else [],
               'updated': [k for k in current if k in old and current[k] != old[k]],
               'removed': [k for k in old if k not in current and k.split(':')[0] in fully_read]}
    result = {'matter': matter, 'resources': resources, 'sources': sources, 'coverage': coverage,
              'changes': changes, 'synced_at': datetime.now(timezone.utc).isoformat()}
    return result
