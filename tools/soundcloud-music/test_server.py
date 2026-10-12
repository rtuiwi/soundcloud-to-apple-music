"""HTTP boundary checks; never contact SoundCloud or open Music."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from core import MusicLibrary
from server import LocalServer


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = LocalServer(library=MusicLibrary(Path(cls.directory.name)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.directory.cleanup()

    def request(self, path, method='GET', body=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status, data, result_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, data, result_headers

    def auth(self):
        return {'Authorization': 'Bearer ' + self.server.token}

    def test_public_ui_has_token_and_security_headers(self):
        status, body, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertNotIn(b'__SESSION_TOKEN__', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_state_requires_token(self):
        self.assertEqual(self.request('/api/state')[0], 401)
        status, body, _ = self.request('/api/state', **self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['tracks'], [])

    def test_foreign_origin_and_host_rejected(self):
        self.assertEqual(self.request('/api/state', Origin='https://example.com', **self.auth())[0], 403)
        self.assertEqual(self.request('/', Host='attacker.test')[0], 403)
        self.assertEqual(self.request('/', **{'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_static_paths_never_expose_data_or_source(self):
        for path in ['/data/manifest.json', '/core.py', '/../core.py', '/%2e%2e/core.py']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_bad_json_and_wrong_content_type(self):
        self.assertEqual(self.request('/api/load', 'POST', '{}', **self.auth())[0], 415)
        self.assertEqual(self.request('/api/load', 'POST', '{', **{'Content-Type': 'application/json', **self.auth()})[0], 400)
        self.assertEqual(self.request('/api/load', 'POST', '[]', **{'Content-Type': 'application/json', **self.auth()})[0], 400)

    def test_oversize_rejected(self):
        self.assertEqual(self.request('/api/load', 'POST', '', **{'Content-Type': 'application/json', 'Content-Length': '131073', **self.auth()})[0], 413)

    def test_arbitrary_download_source_rejected_before_job(self):
        payload = json.dumps({'ids': ['made-up'], 'playlist': 'Test', 'import': True})
        self.assertEqual(self.request('/api/transfer', 'POST', payload, **{'Content-Type': 'application/json', **self.auth()})[0], 400)
        self.assertFalse(self.server.library.state()['job']['active'])


if __name__ == '__main__':
    unittest.main()
