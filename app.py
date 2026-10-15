"""SyncRoom: ephemeral rooms and per-participant Plex connections."""
import asyncio
import math
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
    def __init__(self, store):
        self.rooms = {}
        self.store = store
        self.sockets = {}

    async def broadcast(self, room):
        state = {k: v for k, v in room.items() if k != 'members'}
        state['type'] = 'state'
        state['serverTime'] = time.time()
        state['members'] = [{'id': k, 'name': v['name']} for k, v in room['members'].items()]
        for member in list(room['members'].values()):
            if not member['ws'].closed:
                await member['ws'].send_json(state)

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
                try:
                    data = message.json()
                    action = data.get('type')
                    if self.store.get(sid) is None:
                        await ws.send_json({'type':'auth_signed_out', 'message':'Session expired. Reload and sign in again.'})
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
                            await ws.send_json({'type':'connected'})
                        else:
                            await ws.send_json({'type':'auth_guest'})
                    elif action == 'auth_select':
                        await ws.send_json(await auth.select(str(data.get('id','')), str(data.get('uri','')), plex))
                    elif action == 'auth_cancel':
                        auth.pending = None
                        await ws.send_json({'type':'auth_cancelled'})
                    elif action == 'auth_signout':
                        self.store.delete(sid)
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
                            raise ValueError('Leave your current room first.')
                        if action == 'create':
                            if len(self.rooms) >= 100:
                                raise ValueError('Room limit reached.')
                            code = secrets.token_hex(4).upper()
                            room = {'code': code, 'host': identity, 'media': None, 'playing': False, 'position': 0, 'updated': time.time(), 'members': {}}
                            self.rooms[code] = room
                        else:
                            room = self.rooms.get(str(data.get('code', '')).strip().upper())
                            if not room:
                                raise ValueError('Room not found.')
                            if len(room['members']) >= 12:
                                room = None
                                raise ValueError('Room is full.')
                        room['members'][identity] = {'ws': ws, 'name': auth.name[:64]}
                        await self.broadcast(room)
                    elif action in ('select', 'playback'):
                        if not room or room['host'] != identity:
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
                        await ws.send_json({'type': 'stream', 'id': media['id'], 'url': auth.connection['url'] + part.get('key') + '?X-Plex-Token=' + quote(auth.connection['token'], safe='')})
                    elif action == 'leave':
                        if room:
                            await self.leave(room, identity)
                            room = None
                        await ws.send_json({'type': 'left'})
                    else:
                        raise ValueError('Unknown command.')
                except SignInExpired as error:
                    auth.clear()
                    if room:
                        await self.leave(room, identity)
                        room = None
                        await ws.send_json({'type':'left'})
                    await ws.send_json({'type':'auth_error', 'message':str(error)})
                except ValueError as error:
                    await ws.send_json({'type': 'error', 'message': str(error)[:200]})
                except (TypeError, KeyError, AttributeError, ET.ParseError):
                    await ws.send_json({'type': 'error', 'message': 'Invalid request or Plex response.'})
                except (asyncio.TimeoutError, OSError, ClientError):
                    await ws.send_json({'type': 'error', 'message': 'Plex is unreachable. Check your network connection.'})
        finally:
            self.sockets.get(sid, set()).discard(ws)
            if not self.sockets.get(sid):
                self.sockets.pop(sid, None)
            if room:
                await self.leave(room, identity)
        return ws

    async def leave(self, room, identity):
        room['members'].pop(identity, None)
        if not room['members']:
            self.rooms.pop(room['code'], None)
        else:
            if room['host'] == identity:
                room['host'] = next(iter(room['members']))
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

def create_app(data_dir=None):
    app = web.Application(client_max_size=8192)
    store = SessionStore(data_dir or os.getenv('SYNCROOM_DATA_DIR', 'data'))
    rooms = Rooms(store)
    app[STORE], app[ROOMS] = store, rooms
    async def cleanup(app):
        for peers in list(rooms.sockets.values()):
            for ws in list(peers):
                await ws.close(code=1001, message=b'Server restarting')
        store.close()
    app.on_shutdown.append(cleanup)
    app.router.add_get('/ws', rooms.websocket)
    app.router.add_get('/health', health)
    app.router.add_get('/', index)
    app.router.add_static('/static', STATIC)
    return app

if __name__ == '__main__':
    web.run_app(create_app(), host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '8765')), access_log=None)
