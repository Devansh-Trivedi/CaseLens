import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pymupdf
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import store, clio, ingest, briefing, main


class LocalStorage(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(store, 'DB', Path(self.directory.name) / 'test.sqlite3')
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def test_private_storage(self):
        store.put('test', {'value': 5})
        self.assertEqual(store.get('test'), {'value': 5})
        self.assertEqual(store.DB.stat().st_mode & 0o777, 0o600)
        store.remove('test')
        self.assertIsNone(store.get('test'))

    def test_source_excerpt_is_honest_and_excludes_completed_tasks(self):
        case = sample_case()
        result = briefing.baseline(case)
        self.assertEqual(result['mode'], 'source_excerpts')
        self.assertIn('Litigation', str(result['sections']['overview']))
        self.assertEqual([t['id'] for t in result['tasks']], [1])
        self.assertIsNone(result['model'])

    def test_rejects_unknown_citations(self):
        with self.assertRaises(ValueError):
            briefing.validate_claims({'overview': [{'text': 'Claim', 'source_ids': ['invented']}]}, {'matter'})

    def test_pdf_text_extraction(self):
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((40, 40), 'Synthetic test evidence: follow-up visit recorded.')
        pages = ingest.extract(pdf.tobytes(), 'test.pdf')
        self.assertEqual(pages[0]['page'], 1)
        self.assertEqual(pages[0]['method'], 'text')
        self.assertIn('Synthetic', pages[0]['text'])

    def test_unknown_format_is_unread(self):
        self.assertEqual(ingest.extract(b'unknown', 'scan.heic')[0]['method'], 'unsupported')

    def test_provider_requires_explicit_selection(self):
        store.put('case:1', sample_case())
        with TestClient(main.app) as client:
            empty = client.post('/api/cases/1/provider-preview', json={'source_ids': []}).json()
            self.assertEqual(empty['approved_sources'], [])
            preview = client.post('/api/cases/1/provider-preview', json={'source_ids': ['field:1']}).json()
            self.assertFalse(preview['sent'])
            self.assertTrue(preview['draft'])
            self.assertEqual([s['key'] for s in preview['approved_sources']], ['field:1'])
            self.assertNotIn('Private strategy', str(preview))

    def test_cross_origin_write_blocked(self):
        with TestClient(main.app) as client:
            response = client.post('/api/settings/ai', headers={'Origin': 'https://untrusted.example'}, json={'api_key': 'not-a-real-key-long-enough'})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(store.get('ai_key'))

    def test_callback_rejects_state_without_browser_cookie(self):
        main.states['test-state'] = 9999999999
        with TestClient(main.app) as client:
            response = client.get('/auth/clio/callback?code=test&state=test-state')
        self.assertEqual(response.status_code, 400)

    def test_status_never_exposes_secrets(self):
        store.put('clio_tokens', {'access_token': 'private-access-token'})
        store.put('ai_key', 'private-ai-key')
        with TestClient(main.app) as client:
            response = client.get('/api/status')
        self.assertTrue(response.json()['connected'])
        self.assertNotIn('private-access-token', response.text)
        self.assertNotIn('private-ai-key', response.text)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(store, 'DB', Path(self.directory.name) / 'test.sqlite3')
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_pagination_follows_next(self):
        next_url = clio.API + 'notes.json?page=2'
        with patch.object(clio, 'get', AsyncMock(side_effect=[{'data': [{'id': 1}], 'meta': {'paging': {'next': next_url}}}, {'data': [{'id': 2}]}])) as get:
            rows = await clio.collection('notes.json')
        self.assertEqual([r['id'] for r in rows], [1, 2])
        self.assertEqual(get.await_args_list[1].args, (next_url, None))

    async def test_external_pagination_rejected_before_token(self):
        with patch.object(clio, 'token', AsyncMock()) as token:
            with self.assertRaises(HTTPException):
                await clio.request('https://attacker.example/notes')
            token.assert_not_awaited()

    async def test_partial_ingestion_and_snapshot(self):
        async def collection(path, params):
            if path == 'communications.json':
                raise HTTPException(403, 'Read permission missing')
            if path == 'notes.json':
                self.assertEqual(params['type'], 'Matter')
                return [{'id': 10, 'subject': 'Synthetic note', 'detail': 'Recorded evidence', 'date': '2026-01-01', 'matter': {'id': 1}},
                        {'id': 11, 'subject': 'Wrong matter', 'matter': {'id': 2}}]
            return []
        with patch.object(clio, 'get', AsyncMock(return_value={'data': sample_case()['matter']})), patch.object(clio, 'collection', collection):
            case = await ingest.sync(1, lambda *_: None)
            self.assertEqual(case['matter']['client']['name'], 'Synthetic Client')
            self.assertIsNone(case['coverage']['communications']['count'])
            self.assertEqual(case['coverage']['communications']['status'], 'unavailable')
            self.assertEqual(case['coverage']['notes']['count'], 1)
            self.assertTrue(case['changes']['baseline'])
            case['sources'].append(ingest.source('communications:99', 'communications', 'Old message', 'Prior snapshot'))
            store.put('case:1', case)
            second = await ingest.sync(1, lambda *_: None)
            self.assertFalse(second['changes']['baseline'])
            self.assertNotIn('communications:99', second['changes']['removed'])

    async def test_no_key_does_not_call_model(self):
        with patch.object(briefing, 'ai_config', return_value={'key': None, 'model': 'test'}), patch.object(briefing, 'model_json', AsyncMock()) as model:
            result = await briefing.generate(sample_case(), lambda *_: None)
        model.assert_not_awaited()
        self.assertEqual(result['mode'], 'source_excerpts')

    async def test_failed_sync_preserves_cached_case(self):
        case = sample_case()
        store.put('case:1', case)
        main.jobs['test'] = {'status': 'running'}
        with patch.object(ingest, 'sync', AsyncMock(side_effect=HTTPException(401, 'Reconnect'))):
            await main.run_sync('test', 1)
        self.assertEqual(main.jobs['test']['status'], 'failed')
        self.assertEqual(store.get('case:1'), case)


def sample_case():
    return {'matter': {'id': 1, 'display_number': 'TEST-1', 'description': 'Synthetic matter for automated tests', 'status': 'Open',
                       'client': {'name': 'Synthetic Client'}, 'matter_stage': {'name': 'Litigation'}},
            'sources': [ingest.source('matter', 'matter', 'Test', 'Synthetic evidence'),
                        ingest.source('matter:status', 'matter', 'Status', 'Open Litigation'),
                        ingest.source('field:1', 'field', 'Treatment', 'Follow-up recorded'),
                        ingest.source('field:2', 'field', 'Private strategy', 'Do not include by default')],
            'resources': {'tasks': [{'id': 1, 'name': 'Review evidence', 'status': 'pending', 'due_at': '2026-10-01'},
                                    {'id': 2, 'name': 'Finished', 'status': 'complete'},
                                    {'id': 3, 'name': 'Also finished', 'status': 'completed'}]}}


if __name__ == '__main__':
    unittest.main()
