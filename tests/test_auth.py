import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from app import create_app, STORE
from plex_auth import COOKIE, SessionStore, PlexSignIn, SignInExpired

SERVER = {'provides':'server', 'clientIdentifier':'test-machine', 'name':'Valhalla', 'accessToken':'server-fixture-value',
          'connections':[{'uri':'http://plex:32400','local':True,'relay':False}, {'uri':'https://secure.plex.direct:32400','local':False,'relay':False}]}

class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.directory.name)
        self.sid = self.store.ensure(None)
    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()
    async def fake_cloud(self, method, path, client_id, token=None, params=None):
        if path == '/pins': return {'id':123, 'code':'fixture-code', 'expiresIn':600}
        if path == '/pins/123': return {'authToken':'account-fixture-value'}
        if path == '/user': return {'username':'Test viewer'}
        if path == '/resources': return [SERVER]
        raise AssertionError(path)
    async def test_remembered_login_is_encrypted_and_survives_restart(self):
        with patch('plex_auth.cloud', self.fake_cloud):
            auth = PlexSignIn(self.store, self.sid)
            started = await auth.start(True)
            self.assertTrue(started['url'].startswith('https://app.plex.tv/auth#?'))
            signed = await auth.check()
            self.assertEqual(signed['name'], 'Test viewer')
            self.assertNotIn('account-fixture-value', json.dumps(signed))
            self.assertNotIn('server-fixture-value', json.dumps(signed))
            probe = AsyncMock(return_value=ET.fromstring('<MediaContainer machineIdentifier="test-machine"/>'))
            connected = await auth.select('test-machine', 'http://plex:32400', probe)
            self.assertEqual(connected['type'], 'connected')
            payload = self.store.path.read_bytes()
            self.assertNotIn(b'account-fixture-value', payload)
            self.assertNotIn(self.sid.encode(), payload)
            self.store.close()
            self.store = SessionStore(self.directory.name)
            restored = PlexSignIn(self.store, self.sid)
            self.assertEqual((await restored.resume())['selected'], 'test-machine')
            await restored.select('test-machine', restored.uri, probe)
            self.assertEqual(restored.connection['token'], 'server-fixture-value')
    async def test_not_remembered_cancelled_and_expired(self):
        with patch('plex_auth.cloud', self.fake_cloud):
            auth = PlexSignIn(self.store, self.sid)
            await auth.start(False)
            await auth.check()
            self.assertNotIn('account_token', self.store.get(self.sid))
            self.assertIsNone(PlexSignIn(self.store, self.sid).token)
            auth.pending = {'deadline':time.monotonic()-1}
            with self.assertRaises(SignInExpired): await auth.check()
            self.assertIsNone(auth.pending)
    async def test_polling_and_account_isolation(self):
        fake = AsyncMock(return_value={'authToken':None})
        auth = PlexSignIn(self.store, self.sid)
        with patch('plex_auth.cloud', self.fake_cloud): await auth.start(True)
        with patch('plex_auth.cloud', fake):
            self.assertEqual((await auth.check())['type'], 'auth_pending')
            await auth.check()
            self.assertEqual(fake.await_count, 1)
        other = PlexSignIn(self.store, self.store.ensure(None))
        with self.assertRaises(SignInExpired): await other.check()
        with self.assertRaises(ValueError): await other.select('test-machine','http://evil.invalid', AsyncMock())
    async def test_secure_server_choice_and_expired_cookie(self):
        auth = PlexSignIn(self.store, self.sid, secure=True)
        auth.servers = [SERVER]
        self.assertEqual(len(auth.summary()['servers'][0]['connections']),1)
        probe = AsyncMock()
        with self.assertRaises(ValueError): await auth.select('test-machine','http://plex:32400',probe)
        probe.assert_not_awaited()
        self.store.db.execute('UPDATE sessions SET expires=?', (time.time()-1,))
        self.store.db.commit()
        self.assertIsNone(self.store.get(self.sid))
        self.assertNotEqual(self.store.ensure(self.sid), self.sid)

class BrowserSessions(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_and_signout_invalidates_other_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(TestServer(create_app(directory)), cookie_jar=CookieJar(unsafe=True))
            await client.start_server()
            try:
                page = await client.get('/')
                cookie = page.cookies[COOKIE]
                self.assertTrue(cookie['httponly'])
                self.assertEqual(cookie['samesite'], 'Strict')
                self.assertEqual(int(cookie['max-age']),30*24*60*60)
                sockets = [await client.ws_connect('/ws') for _ in range(2)]
                for ws in sockets:
                    await ws.receive_json()
                    await ws.receive_json()
                await sockets[0].send_json({'type':'create','name':'Spoofed name'})
                denied = await sockets[0].receive_json(timeout=3)
                self.assertEqual(denied['type'], 'error')
                self.assertIn('Sign in with Plex', denied['message'])
                await sockets[0].send_json({'type':'auth_signout'})
                for ws in sockets:
                    self.assertEqual((await ws.receive_json(timeout=3))['type'],'auth_signed_out')
                    await ws.close()
                await asyncio.sleep(0)
                self.assertIsNone(client.server.app[STORE].get(cookie.value))
            finally:
                await client.close()
