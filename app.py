"""SyncRoom: ephemeral rooms and per-participant Plex connections."""
import asyncio
import math
import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from aiohttp import web, ClientSession, ClientTimeout, ClientError, WSMsgType

from plex_auth import COOKIE, SESSION_SECONDS, SessionStore, PlexSignIn, SignInExpired, valid_url

STORE = web.AppKey('store', SessionStore)
ROOMS = web.AppKey('rooms', object)

STATIC = Path(__file__).parent / 'static'

async def plex(session, path):
    async with ClientSession(timeout=ClientTimeout(total=15)) as client:
        async with client.get(session['url'] + path, headers={'X-Plex-Token': session['token']}, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Plex rejected the request. Check your server URL and token.')
            try:
                body = await response.content.readexactly(8_000_001)
            except asyncio.IncompleteReadError as error:
                body = error.partial
            if len(body) > 8_000_000:
                raise ValueError('Plex response too large.')
            return ET.fromstring(body)

class Rooms:
    def __init__(self, store, reconnect_grace=20):
        self.rooms = {}
        self.store = store
        self.sockets = {}
        self.reconnect_grace = reconnect_grace
        self.cleanup_tasks = set()
        self.intentional_disconnects = set()

    @staticmethod
    def account_key(account):
        return hashlib.sha256(account.encode()).hexdigest()[:24]

    def is_host(self, room, identity):
        return bool(room and identity in room['members'] and room['host'] in room['members'] and
                    room['members'][identity]['account'] == room['members'][room['host']]['account'])

    async def broadcast(self, room):
        groups = {}
        host_account = room['members'][room['host']]['account']
        for member in room['members'].values():
            key = self.account_key(member['account'])
            group = groups.setdefault(key, {'id':key, 'name':member['name'], 'tabs':0, 'online':False})
            if member['ws'] is not None and not member['ws'].closed:
                group['tabs'] += 1
                group['online'] = True
        state = {k:v for k,v in room.items() if k not in ('members','host')}
        state.update(type='state', host=self.account_key(host_account), serverTime=time.time(),
                     members=sorted(groups.values(), key=lambda m:(m['id'] != self.account_key(host_account),m['name'].casefold())))
        for member in list(room['members'].values()):
            if member['ws'] is not None and not member['ws'].closed:
                try:
                    await member['ws'].send_json(dict(state, self=self.account_key(member['account'])))
                except ConnectionResetError:
                    pass

    async def websocket(self, request):
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden()
        sid = self.store.get(request.cookies.get(COOKIE))
        if sid is None:
            raise web.HTTPUnauthorized(text='Open SyncRoom in your browser first.')
        sid = request.cookies[COOKIE]
        auth = PlexSignIn(self.store, sid, request.secure or os.getenv('SYNCROOM_SECURE_COOKIE') == '1')
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
        await ws.prepare(request)
        self.sockets.setdefault(sid, set()).add(ws)
        identity = secrets.token_urlsafe(18)
        room = None
        await ws.send_json({'type': 'hello', 'id': identity})
        await ws.send_json({'type': 'auth_ready'})
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                action = None
                try:
                    data = message.json()
                    action = data.get('type')
                    if self.store.get(sid) is None:
                        await ws.send_json({'type':'auth_signed_out', 'message':'Session expired. Reload and sign in again.'})
                        self.intentional_disconnects.add(ws)
                        await ws.close()
                        break
                    if action == 'auth_start':
                        await ws.send_json(await auth.start(data.get('remember', True)))
                    elif action == 'auth_check':
                        await ws.send_json(await auth.check())
                    elif action == 'auth_resume':
                        result = await auth.resume()
                        if result:
                            await ws.send_json(result)
                        elif auth.connection:
                            await ws.send_json({'type':'connected', 'machine':auth.connection['machine']})
                        else:
                            await ws.send_json({'type':'auth_guest'})
                    elif action == 'auth_select':
                        await ws.send_json(await auth.select(str(data.get('id','')), str(data.get('uri','')), plex))
                    elif action == 'auth_cancel':
                        auth.pending = None
                        await ws.send_json({'type':'auth_cancelled'})
                    elif action == 'auth_signout':
                        self.store.delete(sid)
                        self.intentional_disconnects.update(self.sockets.get(sid, ()))
                        for peer in list(self.sockets.get(sid, ())):
                            await peer.send_json({'type':'auth_signed_out'})
                            await peer.close()
                        break
                    elif action == 'connect':
                        parsed = urlsplit(str(data.get('url', '')))
                        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                            raise ValueError('Use a Plex server URL such as http://192.168.1.10:32400.')
                        candidate = {'url': data['url'].rstrip('/'), 'token': str(data.get('token', ''))}
                        root = await plex(candidate, '/')
                        candidate['machine'] = root.get('machineIdentifier')
                        if not candidate['machine']:
                            raise ValueError('This URL does not identify a Plex server.')
                        auth.clear()
                        auth.connection = candidate
                        auth.remember = bool(data.get('remember', True))
                        auth.save()
                        await ws.send_json({'type': 'connected'})
                    elif action == 'browse':
                        if not auth.connection:
                            raise ValueError('Connect Plex first.')
                        section = str(data.get('section', ''))
                        if section and not section.isdigit():
                            raise ValueError('Invalid section.')
                        root = await plex(auth.connection, '/library/sections' + (f'/{section}/all' if section else ''))
                        items = [{'id': x.get('ratingKey') or x.get('key'), 'title': x.get('title', 'Untitled'), 'kind': x.get('type')} for x in root if x.tag in ('Directory', 'Video')]
                        await ws.send_json({'type': 'library', 'items': items, 'section': section})
                    elif action in ('create', 'join'):
                        if not auth.token or not auth.name:
                            raise ValueError('Sign in with Plex before creating or joining a room.')
                        if room:
                            if action == 'create' or str(data.get('code','')).strip().upper() == room['code']:
                                await self.broadcast(room)
                                continue
                            raise ValueError('Leave your current room before joining another.')
                        if action == 'create':
                            if len(self.rooms) >= 100:
                                raise ValueError('Room limit reached.')
                            code = secrets.token_hex(4).upper()
                            while code in self.rooms:
                                code = secrets.token_hex(4).upper()
                            room = {'code': code, 'host': identity, 'media': None, 'playing': False, 'position': 0, 'updated': time.time(), 'members': {}}
                            self.rooms[code] = room
                        else:
                            room = self.rooms.get(str(data.get('code', '')).strip().upper())
                            if not room:
                                raise ValueError('Room not found.')
                            accounts = {m['account'] for m in room['members'].values()}
                            if len(room['members']) >= 36 or (auth.account_id not in accounts and len(accounts) >= 12):
                                room = None
                                raise ValueError('Room is full.')
                        for previous, member in list(room['members'].items()):
                            if member['account'] == auth.account_id and member['ws'] is None:
                                room['members'].pop(previous)
                                if room['host'] == previous:
                                    room['host'] = identity
                        room['members'][identity] = {'ws':ws, 'name':auth.name[:64], 'account':auth.account_id}
                        await self.broadcast(room)
                    elif action in ('select', 'playback'):
                        if not self.is_host(room, identity):
                            raise ValueError('Only the host can control playback.')
                        if action == 'select':
                            if not auth.connection:
                                raise ValueError('Connect Plex first.')
                            key = str(data.get('id', ''))
                            if not key.isdigit():
                                raise ValueError('Invalid media ID.')
                            root = await plex(auth.connection, '/library/metadata/' + key)
                            video = root.find('Video')
                            if video is None:
                                raise ValueError('Select a movie or episode, not a show folder.')
                            room['media'] = {'id': key, 'title': video.get('title'), 'machine': auth.connection['machine']}
                            room['position'], room['playing'] = 0, False
                        else:
                            position = float(data.get('position', 0))
                            if not math.isfinite(position) or position < 0 or position > 604800:
                                raise ValueError('Invalid playback position.')
                            room['position'] = position
                            room['playing'] = bool(data.get('playing'))
                        room['updated'] = time.time()
                        await self.broadcast(room)
                    elif action == 'stream':
                        if not auth.connection or not room or not room['media']:
                            raise ValueError('Join a room and connect Plex first.')
                        media = room['media']
                        if media['machine'] != auth.connection['machine']:
                            raise ValueError('Connect to the same Plex server as the host.')
                        root = await plex(auth.connection, '/library/metadata/' + media['id'])
                        part = root.find('./Video/Media/Part')
                        if part is None or not (part.get('key', '').startswith('/library/parts/')):
                            raise ValueError('No playable media part found.')
                        # Credentials go only to their owning browser, never into shared room state.
                        from urllib.parse import quote
                        await ws.send_json({'type': 'stream', 'id': media['id'], 'machine':media['machine'], 'url': auth.connection['url'] + part.get('key') + '?X-Plex-Token=' + quote(auth.connection['token'], safe='')})
                    elif action == 'leave':
                        if room:
                            await self.leave(room, identity, intentional=True)
                            room = None
                        await ws.send_json({'type': 'left'})
                    else:
                        raise ValueError('Unknown command.')
                except SignInExpired as error:
                    auth.clear()
                    if room:
                        await self.leave(room, identity, intentional=True)
                        room = None
                        await ws.send_json({'type':'left'})
                    await ws.send_json({'type':'auth_error', 'message':str(error)})
                except ValueError as error:
                    await ws.send_json({'type': 'error', 'action':action, 'message': str(error)[:200]})
                except (TypeError, KeyError, AttributeError, ET.ParseError):
                    await ws.send_json({'type': 'error', 'action':action, 'message': 'Invalid request or Plex response.'})
                except (asyncio.TimeoutError, OSError, ClientError):
                    await ws.send_json({'type': 'error', 'action':action, 'message': 'Plex is unreachable. Check your network connection.'})
        finally:
            self.sockets.get(sid, set()).discard(ws)
            if not self.sockets.get(sid):
                self.sockets.pop(sid, None)
            intentional = ws in self.intentional_disconnects
            self.intentional_disconnects.discard(ws)
            if room:
                await self.leave(room, identity, intentional=intentional)
        return ws

    async def leave(self, room, identity, intentional=False):
        member = room['members'].get(identity)
        if not member:
            return
        same_account = [key for key,m in room['members'].items() if key != identity and
                        m['account'] == member['account'] and m['ws'] is not None and not m['ws'].closed]
        if not intentional and not same_account:
            member['ws'] = None
            async def expire():
                await asyncio.sleep(self.reconnect_grace)
                if room['members'].get(identity) is member and member['ws'] is None:
                    await self.remove_member(room, identity)
            task = asyncio.create_task(expire())
            self.cleanup_tasks.add(task)
            task.add_done_callback(self.cleanup_tasks.discard)
            await self.broadcast(room)
        else:
            await self.remove_member(room, identity, same_account)

    async def remove_member(self, room, identity, preferred=()):
        room['members'].pop(identity, None)
        if not room['members']:
            if self.rooms.get(room['code']) is room:
                self.rooms.pop(room['code'], None)
        else:
            if room['host'] == identity:
                active = [key for key,m in room['members'].items() if m['ws'] is not None and not m['ws'].closed]
                room['host'] = next(iter(preferred or active or room['members']))
            await self.broadcast(room)


async def health(request):
    return web.json_response({'status': 'ok'})

async def index(request):
    sid = request.app[STORE].ensure(request.cookies.get(COOKIE))
    response = web.FileResponse(STATIC / 'index.html')
    response.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True, samesite='Strict',
                        secure=request.secure or os.getenv('SYNCROOM_SECURE_COOKIE') == '1', path='/')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

def create_app(data_dir=None, reconnect_grace=20):
    app = web.Application(client_max_size=8192)
    store = SessionStore(data_dir or os.getenv('SYNCROOM_DATA_DIR', 'data'))
    rooms = Rooms(store, reconnect_grace)
    app[STORE], app[ROOMS] = store, rooms
    async def cleanup(app):
        for peers in list(rooms.sockets.values()):
            for ws in list(peers):
                await ws.close(code=1001, message=b'Server restarting')
        for task in list(rooms.cleanup_tasks):
            task.cancel()
        await asyncio.gather(*rooms.cleanup_tasks, return_exceptions=True)
        store.close()
    app.on_shutdown.append(cleanup)
    app.router.add_get('/ws', rooms.websocket)
    app.router.add_get('/health', health)
    app.router.add_get('/', index)
    app.router.add_static('/static', STATIC)
    return app

if __name__ == '__main__':
    web.run_app(create_app(), host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '8765')), access_log=None)
