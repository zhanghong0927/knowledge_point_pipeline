from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'data': [{'id': 'test-model'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class EmptyStreamingCliTests(unittest.TestCase):
    def test_empty_both_classifiers_create_native_product_directories(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), ModelsHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'books.json').write_text('[]', encoding='utf-8')
                    (root / 'model.json').write_text(json.dumps({'api_url': f'http://127.0.0.1:{server.server_port}',
                        'model': 'test-model', 'context_limit': 32768, 'max_tokens': 8192}), encoding='utf-8')
                    scripts = {'dictionary': ROOT / 'modules/dictionary/classification/scripts/classify_books.py',
                               'important': ROOT / 'modules/important_books/scripts/layout_model_validation.py'}
                    for name, script in scripts.items():
                        with self.subTest(track=name):
                            output = root / name
                            result = subprocess.run([sys.executable, '-B', str(script), 'prepare-run',
                                '--books', str(root / 'books.json'), '--config', str(root / 'model.json'),
                                '--out', str(output / 'prepared'), '--classification', str(output / 'classified')],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertTrue((output / 'classified/results').is_dir())
                            self.assertTrue((output / 'prepared/PREPARED.json').is_file())
            finally:
                server.shutdown()
                worker.join()


if __name__ == '__main__':
    unittest.main()
