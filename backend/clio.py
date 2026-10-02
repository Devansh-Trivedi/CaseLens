"""Clio v4 read-only connector. OAuth token exchange is the only remote POST."""
import asyncio
import os
import time
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException
from . import store

load_dotenv(store.ROOT / '.env')
BASE = 'https://app.clio.com'
API = BASE + '/api/v4/'
REDIRECT = os.getenv('CLIO_REDIRECT_URI', 'http://127.0.0.1:8000/auth/clio/callback')
refresh_lock = asyncio.Lock()
read_limit = asyncio.Semaphore(3)


def save_tokens(value):
    previous = store.get('clio_tokens', {})
    previous.update(value)
    previous['expires_at'] = time.time() + int(value.get('expires_in', 7200)) - 60
    store.put('clio_tokens', previous)


async def exchange(data):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(BASE + '/oauth/token', data={
            **data, 'client_id': os.getenv('CLIO_CLIENT_ID'),
            'client_secret': os.getenv('CLIO_CLIENT_SECRET'),
        })
    if response.is_error:
        raise HTTPException(401, 'Clio authorization expired or was rejected. Connect Clio again.')
    save_tokens(response.json())


async def token():
    async with refresh_lock:
        data = store.get('clio_tokens', {})
        if not data.get('access_token'):
            raise HTTPException(401, 'Connect Clio to load your matter.')
        if data.get('expires_at', 0) < time.time():
            if not data.get('refresh_token'):
                raise HTTPException(401, 'Connect Clio again.')
            await exchange({'grant_type': 'refresh_token', 'refresh_token': data['refresh_token']})
            data = store.get('clio_tokens')
        return data['access_token']


async def request(path, params=None):
    url = urljoin(API, path)
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'app.clio.com' or not parsed.path.startswith('/api/v4/'):
        raise HTTPException(502, 'Clio returned an invalid pagination URL.')
    async with read_limit:
        for attempt in range(4):
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.get(url, params=params, headers={'Authorization': 'Bearer ' + await token()})
            if response.status_code == 429 and attempt < 3:
                try:
                    delay = min(float(response.headers.get('Retry-After', '15')), 60)
                except ValueError:
                    delay = 15
                await asyncio.sleep(max(delay, 1))
                continue
            if response.status_code == 401:
                raise HTTPException(401, 'Clio session was rejected. Reconnect Clio.')
            if response.is_error:
                try:
                    message = response.json().get('error', {}).get('message', 'Request failed')
                except (ValueError, AttributeError):
                    message = 'Request failed'
                raise HTTPException(response.status_code, str(message)[:300])
            return response
    raise HTTPException(429, 'Clio is rate limiting requests. Retry sync shortly.')


async def get(path, params=None):
    return (await request(path, params)).json()


async def collection(path, params=None):
    rows, seen = [], set()
    url, query = path, {**(params or {}), 'limit': 200}
    for _ in range(1000):
        payload = await get(url, query)
        rows.extend(payload.get('data', []))
        nxt = payload.get('meta', {}).get('paging', {}).get('next')
        if not nxt:
            return rows
        if nxt in seen:
            raise HTTPException(502, 'Clio pagination repeated a page; sync is incomplete.')
        seen.add(nxt)
        url, query = nxt, None
    raise HTTPException(502, 'Pagination limit reached; sync is incomplete.')


async def document_bytes(doc_id):
    response = await request(f'documents/{doc_id}/download.json')
    if response.is_redirect:
        location = response.headers.get('location', '')
        if not location.startswith('https://'):
            raise HTTPException(502, 'Invalid document download URL.')
        # A separate client ensures the Clio bearer token never reaches file storage.
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            response = await client.get(location)
    if response.is_error:
        raise HTTPException(502, 'Document download failed.')
    if len(response.content) > 50 * 1024 * 1024:
        raise HTTPException(413, 'Document exceeds the 50 MB local MVP limit.')
    return response.content


MATTER_FIELDS = 'id,etag,display_number,description,status,open_date,close_date,updated_at,last_activity_date,client{id,name},practice_area{id,name},matter_stage{id,name},responsible_attorney{id,name},custom_field_values{id,field_name,field_type,value},statute_of_limitations{id,name,due_at,status}'
COLLECTIONS = {
    'notes': ('notes.json', 'id,etag,subject,detail,date,updated_at,matter{id}'),
    'communications': ('communications.json', 'id,etag,subject,body,type,date,updated_at,matter{id}'),
    'tasks': ('tasks.json', 'id,etag,name,description,due_at,status,priority,completed_at,updated_at,matter{id},assignee{id,name}'),
    'documents': ('documents.json', 'id,etag,name,filename,size,content_type,updated_at,received_at,matter{id},latest_document_version{id}'),
    'activities': ('activities.json', 'id,etag,type,note,date,total,quantity,price,updated_at,matter{id},vendor{id,name}'),
    'calendar': ('calendar_entries.json', 'id,etag,summary,description,start_at,end_at,updated_at,matter{id}'),
    'bills': ('bills.json', 'id,etag,number,subject,state,total,balance,issued_at,due_at,updated_at'),
    'contacts': ('matters/{matter_id}/contacts.json', 'id,etag,name,type,is_client,relationship_name'),
}
