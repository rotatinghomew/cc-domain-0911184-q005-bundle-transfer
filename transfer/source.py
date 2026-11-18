import copy
import hashlib
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class BundleSource:
    """A local, in-memory producer used by tests and the demo.

    Each version is an immutable snapshot: :meth:`retain` keeps the token,
    the archive members and the validator of the version that is being
    replaced.  Rotating a signed URL or publishing a delta therefore cannot
    change what an older manifest or download refers to.
    """

    def __init__(self):
        self.asset = 'delivery-a'
        self.version = 1
        self.token = 'signed-1'
        self.ignore_range = False
        self.ignore_if_range = False
        self.revoked = False
        self.mode = 'full'
        self.base_version = None
        self.removals = []
        self.final_files = None
        self.retained = {}
        self.requests = []
        self.files = {'subtitles/main.srt': '1\n00:00:01,000 --> 00:00:02,000\n山间来信\n'.encode(),
                      'cover.txt': b'cover-description'}

        # Optional explicit validator.  By default the ETag is derived from
        # the bytes, matching the original protocol.
        self.etag = None

        # Fault injection: send this many body bytes and then drop the
        # connection.  An integer applies to the next attempt; a list/tuple
        # is consumed one item per attempt.
        self.fail_after = None
        self.fail_once = True
        self.fail_before_headers = False

        # Answer the download endpoint with an error page (still possibly a
        # 200 status) instead of the bundle.
        self.error_body = None
        self.error_status = 200
        self.error_content_type = 'text/html; charset=utf-8'
        self.error_once = False

        self.failure_count = 0
        self.download_count = 0

    # ------------------------------------------------------------------
    # Friendly aliases (properties keep copy.copy()/snapshots working)
    # ------------------------------------------------------------------
    @property
    def interrupt_after(self):
        return self.fail_after

    @interrupt_after.setter
    def interrupt_after(self, value):
        self.fail_after = value

    @property
    def drop_after(self):
        return self.fail_after

    @drop_after.setter
    def drop_after(self, value):
        self.fail_after = value

    @property
    def disconnect_after(self):
        return self.fail_after

    @disconnect_after.setter
    def disconnect_after(self, value):
        self.fail_after = value

    @property
    def fail_at(self):
        return self.fail_after

    @fail_at.setter
    def fail_at(self, value):
        self.fail_after = value

    @property
    def validator(self):
        return self.etag

    @validator.setter
    def validator(self, value):
        self.etag = value

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------
    def retain(self, version=None):
        """Snapshot the current (or an explicitly selected) version."""
        if version is not None and version != self.version:
            raise ValueError('can only retain the currently published version')
        saved = copy.copy(self)
        saved.files = dict(self.files)
        saved.final_files = None if self.final_files is None else dict(self.final_files)
        saved.removals = list(self.removals)
        saved.retained = {}
        saved.requests = []
        self.retained[self.version] = saved
        return saved

    def publish(self, version=None, token=None, files=None, mode=None,
                base_version=None, removals=None, final_files=None,
                revoked=None):
        """Retain the current version and publish the next one.

        Only a convenience wrapper: nothing is collected or merged here.
        """
        self.retain()
        if version is not None:
            self.version = version
        if token is not None:
            self.token = token
        if files is not None:
            self.files = dict(files)
        if mode is not None:
            self.mode = mode
        if base_version is not None:
            self.base_version = base_version
        if removals is not None:
            self.removals = list(removals)
        if final_files is not None:
            self.final_files = dict(final_files)
        if revoked is not None:
            self.revoked = revoked
        return self

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------
    def bundle(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as archive:
            for name, data in self.files.items():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                archive.writestr(info, data)
        return output.getvalue()

    def _validator(self, data):
        value = self.etag
        if value is None:
            value = hashlib.sha256(data).hexdigest()
        value = str(value)
        if not (value.startswith('"') and value.endswith('"')) and not value.startswith('W/'):
            value = '"' + value + '"'
        return value

    def manifest(self, base_url, version=None):
        selected = self
        if version is not None and version != self.version:
            selected = self.retained.get(int(version))
            if selected is None:
                raise KeyError('version not retained')
        data = selected.bundle()
        final_files = selected.files if selected.final_files is None else selected.final_files
        return {'asset': selected.asset, 'version': selected.version,
                'revoked': selected.revoked,
                'mode': selected.mode, 'base_version': selected.base_version,
                'removals': list(selected.removals),
                'url': base_url + '/download/' + selected.token,
                'manifest_url': base_url + '/manifest',
                'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'etag': selected._validator(data),
                'files': {name: {'sha256': hashlib.sha256(content).hexdigest(),
                                 'size': len(content)}
                          for name, content in final_files.items()}}


def _failure_limit(source):
    """Return the failure limit for the next attempt, consuming it once."""
    value = source.fail_after
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            source.fail_after = None
            return None
        limit = value[0]
        source.fail_after = value[1:] or None
        return limit
    if source.fail_once:
        source.fail_after = None
    return value


@contextmanager
def serve(source=None):
    source = source or BundleSource()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def _selected_download(self, path):
            return next((candidate for candidate in [source, *source.retained.values()]
                         if path == '/download/' + candidate.token), None)

        def _send_error_page(self, data, etag):
            self.send_response(source.error_status)
            self.send_header('Content-Type', source.error_content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('ETag', etag)
            self.end_headers()
            self.wfile.write(data)
            if source.error_once:
                source.error_body = None

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            source.requests.append({'path': path,
                                    'range': self.headers.get('Range'),
                                    'if_range': self.headers.get('If-Range')})
            if path == '/manifest':
                requested = parse_qs(parsed.query).get('version', [str(source.version)])[-1]
                try:
                    wanted = int(requested)
                except (TypeError, ValueError):
                    self.send_error(404, 'version not retained')
                    return
                selected = source if wanted == source.version else source.retained.get(wanted)
                if selected is None:
                    self.send_error(404, 'version not retained')
                    return
                data = json.dumps(selected.manifest(
                    f'http://127.0.0.1:{self.server.server_port}')).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if not path.startswith('/download/'):
                self.send_error(403)
                return

            source.download_count += 1
            selected = self._selected_download(path)
            if selected is None:
                self.send_error(403)
                return

            data = selected.bundle()
            etag = selected._validator(data)

            if source.error_body is not None:
                self._send_error_page(source.error_body, etag)
                return
            if source.fail_before_headers:
                source.failure_count += 1
                if source.fail_once:
                    source.fail_before_headers = False
                self.close_connection = True
                try:
                    self.connection.close()
                except OSError:
                    pass
                return

            requested = self.headers.get('Range')
            if_range = self.headers.get('If-Range')
            match = if_range in (None, etag) or source.ignore_if_range
            status = 200
            full_size = len(data)
            start = 0
            stop = full_size - 1
            if requested and match and not source.ignore_range:
                try:
                    first, last = requested.removeprefix('bytes=').split('-', 1)
                    start, stop = int(first), int(last) if last else full_size - 1
                    if start < 0 or start >= full_size or stop < start:
                        raise ValueError()
                except ValueError:
                    self.send_error(416)
                    return
                stop = min(stop, full_size - 1)
                status = 206
                body = data[start:stop + 1]
            else:
                body = data

            limit = _failure_limit(source)
            if status == 206:
                self.send_response(status)
                self.send_header('Content-Range', f'bytes {start}-{stop}/{full_size}')
            else:
                self.send_response(status)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('ETag', etag)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()

            if limit is not None:
                source.failure_count += 1
                sent = body[:max(0, int(limit))]
                try:
                    self.wfile.write(sent)
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            try:
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

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
