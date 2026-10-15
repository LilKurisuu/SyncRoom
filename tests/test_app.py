import unittest
import tempfile
from aiohttp import CookieJar
from unittest.mock import patch
from xml.etree import ElementTree as ET
from aiohttp.test_utils import TestClient, TestServer
from app import create_app

class Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = TestClient(TestServer(create_app(self.directory.name)), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        await self.client.get('/')
        self.sockets = []
    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.client.close()
        self.directory.cleanup()
    async def connect(self):
        ws = await self.client.ws_connect('/ws')
        self.sockets.append(ws)
        identity = (await ws.receive_json())['id']
        self.assertEqual((await ws.receive_json())['type'], 'auth_ready')
        async def fake_auth(method, path, client_id, token=None, params=None):
            if path == '/pins': return {'id':123, 'code':'fixture-code', 'expiresIn':600}
            if path == '/pins/123': return {'authToken':'account-fixture-value'}
            if path == '/user': return {'username':'Plex viewer'}
            if path == '/resources': return [{'provides':'server','clientIdentifier':'test-server','name':'Test server','accessToken':'fixture-value','connections':[{'uri':'http://plex:32400','local':True}]}]
            raise AssertionError(path)
        with patch('plex_auth.cloud', fake_auth):
            self.assertEqual((await self.command(ws, {'type':'auth_start','remember':False}))['type'], 'auth_started')
            self.assertEqual((await self.command(ws, {'type':'auth_check'}))['type'], 'auth_signed_in')
        return ws, identity
    async def command(self, ws, data):
        await ws.send_json(data)
        return await ws.receive_json(timeout=3)
    async def test_room_permissions_sync_handoff_cleanup(self):
        host, host_id = await self.connect()
        guest, guest_id = await self.connect()
        created = await self.command(host, {'type':'create','name':'Host'})
        self.assertEqual(created['host'], host_id)
        self.assertEqual(created['members'][0]['name'], 'Plex viewer')
        joined = await self.command(guest, {'type':'join','code':created['code'],'name':'Guest'})
        await host.receive_json()
        self.assertEqual(len(joined['members']), 2)
        self.assertEqual((await self.command(guest, {'type':'playback','position':40,'playing':True}))['type'], 'error')
        playing = await self.command(host, {'type':'playback','position':12,'playing':True})
        received = await guest.receive_json()
        self.assertEqual(received['position'], 12)
        self.assertTrue(playing['playing'])
        self.assertEqual((await self.command(host, {'type':'playback','position':float('nan')}))['type'], 'error')
        await host.close()
        handed = await guest.receive_json(timeout=3)
        self.assertEqual(handed['host'], guest_id)
        self.assertEqual((await self.command(guest, {'type':'leave'}))['type'], 'left')
        newcomer, _ = await self.connect()
        self.assertEqual((await self.command(newcomer, {'type':'join','code':created['code']}))['type'], 'error')
    async def test_plex_selection_and_private_stream(self):
        async def fake(session, path):
            if path == '/':
                return ET.fromstring('<MediaContainer machineIdentifier="test-server"/>')
            if path == '/library/sections':
                return ET.fromstring('<MediaContainer><Directory key="1" title="Movies" type="movie"/></MediaContainer>')
            return ET.fromstring('<MediaContainer><Video ratingKey="42" title="Test movie"><Media><Part key="/library/parts/42/file.mp4"/></Media></Video></MediaContainer>')
        with patch('app.plex', fake):
            host, _ = await self.connect()
            guest, _ = await self.connect()
            for ws in (host, guest):
                self.assertEqual((await self.command(ws, {'type':'auth_select','id':'test-server','uri':'http://plex:32400'}))['type'], 'connected')
            created = await self.command(host, {'type':'create'})
            await self.command(guest, {'type':'join','code':created['code']})
            await host.receive_json()
            library = await self.command(host, {'type':'browse'})
            self.assertEqual(library['items'][0]['title'], 'Movies')
            selected = await self.command(host, {'type':'select','id':'42'})
            self.assertEqual(selected['media']['title'], 'Test movie')
            self.assertNotIn('fixture-value', str(await guest.receive_json()))
            stream = await self.command(guest, {'type':'stream'})
            self.assertIn('/library/parts/42/file.mp4', stream['url'])
    async def test_pages_and_origin(self):
        for path in ('/', '/health', '/static/app.js'):
            self.assertEqual((await self.client.get(path)).status, 200)
        self.assertEqual((await self.client.get('/ws', headers={'Origin':'https://evil.example'})).status, 403)
        ws, _ = await self.connect()
        self.assertEqual((await self.command(ws, {'type':'connect','url':'file:///etc/passwd'}))['type'], 'error')

if __name__ == '__main__':
    unittest.main()
