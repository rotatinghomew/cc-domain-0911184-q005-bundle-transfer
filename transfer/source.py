import hashlib
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class BundleSource:
    def __init__(self):
        self.version = 1
        self.token = 'signed-1'
        self.ignore_range = False
        self.revoked = False
        self.mode = 'full'
        self.base_version = None
        self.removals = []
        self.final_files = None
        self.requests = []
        self.files = {'subtitles/main.srt': '1\n00:00:01,000 --> 00:00:02,000\n山间来信\n'.encode(),
                      'cover.txt': b'cover-description'}

    def bundle(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as archive:
            for name, data in self.files.items():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                archive.writestr(info, data)
        return output.getvalue()

    def manifest(self, base_url):
        data = self.bundle()
        final_files = self.files if self.final_files is None else self.final_files
        return {'asset': 'delivery-a', 'version': self.version, 'revoked': self.revoked,
                'mode': self.mode, 'base_version': self.base_version, 'removals': self.removals,
                'url': base_url + '/download/' + self.token, 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'files': {name: {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}
                          for name, content in final_files.items()}}


@contextmanager
def serve(source=None):
    source = source or BundleSource()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            source.requests.append({'path': path, 'range': self.headers.get('Range'), 'if_range': self.headers.get('If-Range')})
            if path == '/manifest':
                requested = parse_qs(parsed.query).get('version', [str(source.version)])[-1]
                if requested != str(source.version):
                    self.send_error(404, 'version not retained')
                    return
                data = json.dumps(source.manifest(f'http://127.0.0.1:{self.server.server_port}')).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
            elif path == '/download/' + source.token:
                data = source.bundle()
                etag = '"' + hashlib.sha256(data).hexdigest() + '"'
                requested = self.headers.get('Range')
                match = self.headers.get('If-Range') in (None, etag)
                status = 200
                if requested and match and not source.ignore_range:
                    try:
                        first, last = requested.removeprefix('bytes=').split('-')
                        start, stop = int(first), int(last) if last else len(data) - 1
                        if start < 0 or start >= len(data) or stop < start:
                            raise ValueError()
                    except ValueError:
                        self.send_error(416)
                        return
                    stop = min(stop, len(data) - 1)
                    full_size = len(data)
                    data = data[start:stop + 1]
                    status = 206
                self.send_response(status)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('ETag', etag)
                self.send_header('Accept-Ranges', 'bytes')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{stop}/{full_size}')
            else:
                self.send_error(403)
                return
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', source
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
