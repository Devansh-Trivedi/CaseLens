import asyncio
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import store, clio, ingest, briefing

app = FastAPI(title='CaseLens API', version='0.2.0')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
jobs = {}
running = set()
states = {}


@app.middleware('http')
async def local_requests(request, call_next):
    origin = request.headers.get('origin')
    if origin and urlparse(origin).netloc != request.headers.get('host'):
        return JSONResponse({'detail': 'Open CaseLens directly on this computer.'}, status_code=403)
    response = await call_next(request)
    if not request.url.path.startswith('/assets'):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get('/health')
def health():
    return {'ok': True, 'version': '0.2.0'}


@app.get('/api/status')
def status():
    ai = briefing.ai_config()
    return {'connected': bool(store.get('clio_tokens', {}).get('access_token')),
            'configured': bool(os.getenv('CLIO_CLIENT_ID') and os.getenv('CLIO_CLIENT_SECRET')),
            'ai_configured': bool(ai['key']), 'model': ai['model'], 'local_only': True}


@app.get('/auth/clio/start')
def auth_start():
    if not os.getenv('CLIO_CLIENT_ID') or not os.getenv('CLIO_CLIENT_SECRET'):
        raise HTTPException(400, 'Add CLIO_CLIENT_ID and CLIO_CLIENT_SECRET to .env, then restart.')
    now = time.time()
    for key in list(states):
        if states[key] < now:
            states.pop(key, None)
    state = secrets.token_urlsafe(32)
    states[state] = now + 600
    response = RedirectResponse(clio.BASE + '/oauth/authorize?' + urlencode({
        'response_type': 'code', 'client_id': os.getenv('CLIO_CLIENT_ID'),
        'redirect_uri': clio.REDIRECT, 'state': state}))
    response.set_cookie('clio_state', state, httponly=True, samesite='lax', max_age=600, path='/auth/clio')
    return response


@app.get('/auth/clio/callback')
async def auth_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    cookie = request.cookies.get('clio_state', '')
    if not state or not secrets.compare_digest(state, cookie) or states.pop(state, 0) < time.time():
        raise HTTPException(400, 'Connection expired. Return to CaseLens and click Connect Clio.')
    if error or not code:
        return RedirectResponse('/?connection=cancelled')
    await clio.exchange({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': clio.REDIRECT})
    response = RedirectResponse('/')
    response.delete_cookie('clio_state', path='/auth/clio')
    return response


class AISettings(BaseModel):
    api_key: str = Field(min_length=15, max_length=500)
    model: str = Field(default='gpt-4.1-mini', min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9._-]+$')


@app.post('/api/settings/ai')
def settings(body: AISettings):
    store.put('ai_key', body.api_key.strip())
    store.put('ai_model', body.model)
    return {'saved': True}


@app.get('/api/cases')
async def list_cases():
    rows = await clio.collection('matters.json', {'fields': 'id,display_number,description,status,client{id,name},matter_stage{id,name}'})
    return {'data': rows}


@app.get('/api/cases/{matter_id}')
async def get_case(matter_id: int):
    return await clio.get(f'matters/{matter_id}.json', {'fields': clio.MATTER_FIELDS})


@app.get('/api/cases/{matter_id}/digest')
def digest(matter_id: int):
    case = store.get(f'case:{matter_id}')
    if case is None:
        raise HTTPException(404, 'Select this matter and click Sync & brief to read its records.')
    return case


async def run_sync(job_id, matter_id, only_brief=False):
    def progress(message, percent):
        jobs[job_id].update(message=message, progress=percent)
    try:
        case = store.get(f'case:{matter_id}') if only_brief else await ingest.sync(matter_id, progress)
        if not case:
            raise HTTPException(400, 'Sync this case first.')
        case['briefing'] = briefing.baseline(case)
        store.put(f'case:{matter_id}', case)
        case['briefing'] = await briefing.generate(case, progress)
        store.put(f'case:{matter_id}', case)
        jobs[job_id].update(status='complete', message='Briefing ready', progress=100)
    except Exception as exc:
        message = str(exc.detail) if isinstance(exc, HTTPException) else 'Sync failed. Retry or reconnect; any saved source data remains available.'
        jobs[job_id].update(status='failed', message=message)


@app.post('/api/cases/{matter_id}/sync')
async def start_sync(matter_id: int, brief_only: bool = False):
    for key, value in jobs.items():
        if value['matter_id'] == matter_id and value['status'] == 'running':
            return {'job_id': key}
    if not brief_only:
        await clio.token()
    key = secrets.token_urlsafe(12)
    jobs[key] = {'matter_id': matter_id, 'status': 'running', 'progress': 0, 'message': 'Starting sync'}
    task = asyncio.create_task(run_sync(key, matter_id, brief_only))
    running.add(task)
    task.add_done_callback(running.discard)
    return {'job_id': key}


@app.get('/api/jobs/{job_id}')
def job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, 'This sync was interrupted by a server restart. Sync again.')
    return jobs[job_id]


class ProviderSelection(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=50)


@app.post('/api/cases/{matter_id}/provider-preview')
def provider_preview(matter_id: int, selection: ProviderSelection):
    case = store.get(f'case:{matter_id}')
    if not case:
        raise HTTPException(404, 'Sync the case first.')
    selected = set(selection.source_ids)
    rows = [s for s in case['sources'] if s['key'] in selected]
    return {'client': (case['matter'].get('client') or {}).get('name'),
            'stage': (case['matter'].get('matter_stage') or {}).get('name'),
            'approved_sources': rows, 'draft': True, 'sent': False}


DIST = store.ROOT / 'frontend' / 'dist'
app.mount('/assets', StaticFiles(directory=DIST / 'assets', check_dir=False), name='assets')


@app.get('/')
def homepage():
    if not (DIST / 'index.html').exists():
        return JSONResponse({'detail': 'Frontend build missing. Run bash start.sh.'}, status_code=503)
    return FileResponse(DIST / 'index.html')
