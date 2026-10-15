"""Plex's hosted PIN sign-in and encrypted, browser-specific sessions."""
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from aiohttp import ClientSession, ClientTimeout, ClientError
from cryptography.fernet import Fernet

COOKIE = 'syncroom_session'
SESSION_SECONDS = 30 * 24 * 60 * 60
API = 'https://clients.plex.tv/api/v2'

class SignInExpired(ValueError):
    pass

class SessionStore:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = directory / 'session.key'
        try:
            with key_path.open('xb') as file:
                os.chmod(key_path, 0o600)
                file.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self.cipher = Fernet(key_path.read_bytes())
        self.path = directory / 'sessions.sqlite3'
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL, data BLOB)')
        self.db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        self.db.commit()
    @staticmethod
    def digest(sid):
        return hashlib.sha256(sid.encode()).hexdigest()
    def get(self, sid):
        if not sid or len(sid) > 128:
            return None
        row = self.db.execute('SELECT expires, data FROM sessions WHERE id=?', (self.digest(sid),)).fetchone()
        if not row:
            return None
        if row[0] < time.time():
            self.delete(sid)
            return None
        return json.loads(self.cipher.decrypt(row[1]))
    def save(self, sid, data):
        ciphertext = self.cipher.encrypt(json.dumps(data).encode())
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)', (self.digest(sid), time.time() + SESSION_SECONDS, ciphertext))
        self.db.commit()
    def ensure(self, sid):
        data = self.get(sid)
        if data is None:
            sid = secrets.token_urlsafe(32)
            data = {'client_id': str(uuid.uuid4())}
        self.save(sid, data)
        return sid
    def delete(self, sid):
        self.db.execute('DELETE FROM sessions WHERE id=?', (self.digest(sid),))
        self.db.commit()
    def close(self):
        self.db.close()

async def cloud(method, path, client_id, token=None, params=None):
    headers = {'Accept':'application/json', 'X-Plex-Product':'SyncRoom', 'X-Plex-Version':'0.2',
               'X-Plex-Client-Identifier':client_id, 'X-Plex-Platform':'Web', 'X-Plex-Device':'Browser'}
    if token:
        headers['X-Plex-Token'] = token
    async with ClientSession(timeout=ClientTimeout(total=15)) as client:
        async with client.request(method, API + path, headers=headers, params=params, allow_redirects=False) as response:
            if response.status in (401, 403):
                raise SignInExpired('Plex access expired or was revoked. Sign in again.')
            if response.status == 404 and path.startswith('/pins/'):
                raise SignInExpired('Sign-in expired. Please try again.')
            if response.status not in (200, 201):
                raise ValueError('Plex sign-in is unavailable. Please try again later.')
            try:
                body = await response.content.readexactly(1_000_001)
            except asyncio.IncompleteReadError as error:
                body = error.partial
            if len(body) > 1_000_000:
                raise ValueError('Plex returned too much account data.')
            return json.loads(body)

def valid_url(uri):
    parsed = urlsplit(uri)
    return parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not (parsed.username or parsed.password or parsed.query or parsed.fragment) and parsed.path in ('', '/')

class PlexSignIn:
    def __init__(self, store, sid, secure=False):
        self.store, self.sid, self.secure = store, sid, secure
        data = store.get(sid)
        self.client_id = data['client_id']
        self.token = data.get('account_token')
        self.name = data.get('name', '')
        self.account_id = data.get('account_id', self.client_id)
        self.connection = data.get('manual')
        self.selected = data.get('selected')
        self.uri = data.get('uri')
        self.remember = bool(self.token or self.connection)
        self.servers = []
        self.pending = None
        self.last_start = 0
    def save(self):
        data = {'client_id':self.client_id}
        if self.remember:
            if self.token:
                data.update(account_token=self.token, name=self.name, account_id=self.account_id, selected=self.selected, uri=self.uri)
            elif self.connection:
                data['manual'] = self.connection
        self.store.save(self.sid, data)
    def clear(self):
        self.token = self.connection = self.pending = self.selected = self.uri = None
        self.name = ''
        self.servers = []
        self.save()
    async def start(self, remember):
        now = time.monotonic()
        if now - self.last_start < 5:
            raise ValueError('Please wait a few seconds before starting sign-in again.')
        self.last_start = now
        pin = await cloud('POST', '/pins', self.client_id, params={'strong':'true'})
        self.pending = {'id':int(pin['id']), 'code':pin['code'], 'deadline':now + min(int(pin.get('expiresIn', 600)), 600), 'next_check':0, 'remember':bool(remember)}
        url = 'https://app.plex.tv/auth#?' + urlencode({'clientID':self.client_id, 'code':pin['code'], 'context[device][product]':'SyncRoom'})
        return {'type':'auth_started', 'url':url, 'expiresIn':min(int(pin.get('expiresIn',600)),600)}
    async def check(self):
        pin = self.pending
        if not pin or time.monotonic() >= pin['deadline']:
            self.pending = None
            raise SignInExpired('Sign-in expired. Please try again.')
        if time.monotonic() < pin['next_check']:
            return {'type':'auth_pending'}
        pin['next_check'] = time.monotonic() + 2
        result = await cloud('GET', f"/pins/{pin['id']}", self.client_id, params={'code':pin['code']})
        token = result.get('authToken')
        if not token:
            return {'type':'auth_pending'}
        # Commit credentials only after the account and resource lookup succeed.
        user = await cloud('GET', '/user', self.client_id, token)
        servers = await self.resources(token)
        self.token, self.servers = token, servers
        self.remember = pin['remember']
        self.name = user.get('friendlyName') or user.get('username') or 'Plex viewer'
        self.account_id = str(user.get('id') or self.client_id)
        self.connection = self.selected = self.uri = self.pending = None
        self.save()
        return self.summary()
    async def resources(self, token):
        result = await cloud('GET', '/resources', self.client_id, token, params={'includeHttps':'1', 'includeRelay':'1', 'includeIPv6':'1'})
        return [resource for resource in result if 'server' in resource.get('provides', '').split(',') and resource.get('accessToken')]
    async def resume(self):
        if self.token:
            self.servers = await self.resources(self.token)
            if self.account_id == self.client_id:
                user = await cloud('GET', '/user', self.client_id, self.token)
                self.account_id = str(user.get('id') or self.client_id)
                self.save()
            return self.summary()
        return None
    def summary(self):
        # Server credentials never enter a shared room or the account UI.
        return {'type':'auth_signed_in', 'name':self.name, 'remembered':self.remember,
                'selected':self.selected, 'uri':self.uri,
                'servers':[{'id':s['clientIdentifier'], 'name':s.get('name','Plex server'),
                            'connections':[{'uri':c['uri'], 'local':bool(c.get('local')), 'relay':bool(c.get('relay'))}
                                           for c in s.get('connections',[]) if valid_url(c.get('uri','')) and (not self.secure or c['uri'].startswith('https://'))]}
                           for s in self.servers]}
    async def select(self, server_id, uri, probe):
        server = next((s for s in self.servers if s['clientIdentifier'] == server_id), None)
        if not server:
            raise ValueError('Select a Plex server available to your account.')
        candidates = [c for c in server.get('connections',[]) if valid_url(c.get('uri','')) and (not self.secure or c['uri'].startswith('https://'))]
        if uri:
            candidates = [c for c in candidates if c['uri'] == uri]
        candidates.sort(key=lambda c:(bool(c.get('relay')), not bool(c.get('local')), not c['uri'].startswith('https://')))
        for endpoint in candidates[:6]:
            candidate = {'url':endpoint['uri'].rstrip('/'), 'token':server['accessToken']}
            try:
                root = await asyncio.wait_for(probe(candidate, '/'), timeout=4)
                if root.get('machineIdentifier') != server['clientIdentifier']:
                    continue
            except (ValueError, OSError, asyncio.TimeoutError, ClientError):
                continue
            candidate['machine'] = server['clientIdentifier']
            self.connection = candidate
            self.selected, self.uri = server_id, endpoint['uri']
            self.save()
            return {'type':'connected', 'name':server.get('name','Plex server'), 'machine':candidate['machine']}
        raise ValueError('Cannot reach this server. Try another connection from Your Plex server.')
