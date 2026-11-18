"""Resumable acquisition of versioned bundles.

The important distinction implemented here is

    partial bytes on disk  !=  a verified package  !=  received material

Bytes are appended to a ``.partial`` file while a download is in progress,
but that file is only ever *promoted* to the destination after the complete
bundle has been validated against the manifest.  The presence of a file (or
of a certain number of bytes) is never treated as evidence that the transfer
finished.

Resuming is likewise based on evidence from the source: the validator sent
with a partial response is recorded together with the bytes it describes.  If
the origin later answers a conditional request with a different validator, a
full ``200`` response, or a different length, the previous bytes are discarded
rather than glued to a different representation.
"""

import hashlib
import http.client
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import DownloadError, DownloadInterrupted, UnsupportedResponse

_SAFE_NAME = re.compile(r'^[A-Za-z0-9._-]+$')
_HEX_DIGEST = re.compile(r'^[0-9a-fA-F]{64}$')
# Errors that mean "the connection stopped early"; they never mean the
# content was verified.
_TRANSPORT_ERRORS = (OSError, TimeoutError, http.client.HTTPException)


def _state_path(destination):
    return Path(str(destination) + '.state')


def _partial_path(destination):
    # Keep the historical ``<name>.partial`` spelling so existing tooling and
    # cleanup rules keep working.
    return Path(str(destination) + '.partial')


def _validator(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _etag_matches(left, right):
    """Compare strong validators; weak ETags cannot be used for If-Range."""
    if not left or not right:
        return False
    if left.startswith('W/') or right.startswith('W/'):
        return False
    return left == right


class Downloader:
    """Download and verify a bundle described by a source manifest.

    ``state_directory`` may be supplied when the resumable state should live
    somewhere other than next to the destination.  ``attempts`` controls how
    many transport attempts are made for a single call.
    """

    def __init__(self, state_directory=None, attempts=3, timeout=5):
        self.state_directory = Path(state_directory) if state_directory else None
        self.attempts = max(1, int(attempts))
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Paths and persisted evidence
    # ------------------------------------------------------------------
    def _paths(self, destination):
        destination = Path(destination)
        state_path = _state_path(destination)
        if self.state_directory is not None:
            self.state_directory.mkdir(parents=True, exist_ok=True)
            stem = destination.name
            if not _SAFE_NAME.match(stem):
                stem = hashlib.sha256(stem.encode()).hexdigest()
            state_path = self.state_directory / (stem + '.state')
        return destination, _partial_path(destination), state_path

    def _read_state(self, state_path, partial_path):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or state.get('format') != 1:
            return None
        for key in ('asset', 'version', 'sha256', 'size', 'etag'):
            if key not in state:
                return None
        if not isinstance(state['size'], int) or state['size'] < 0:
            return None
        if not isinstance(state['sha256'], str) or not _HEX_DIGEST.fullmatch(state['sha256']):
            return None
        try:
            recorded = int(state.get('offset', 0))
            present = partial_path.stat().st_size
        except (TypeError, ValueError, OSError):
            return None
        # Bytes beyond the recorded offset were never confirmed by the
        # source; never assume a longer file means more progress.
        offset = min(recorded, present)
        if offset < 0:
            return None
        state['offset'] = offset
        try:
            with partial_path.open('r+b') as handle:
                handle.truncate(offset)
        except OSError:
            return None
        return state

    def _write_state(self, state_path, state):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(state_path.name + '.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))
        os.replace(temporary, state_path)

    def _clear(self, partial_path, state_path):
        state_path.unlink(missing_ok=True)
        partial_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fetch(self, manifest, destination, progress=None):
        """Acquire ``manifest`` at ``destination``.

        Returns the destination path.  A previously completed bundle is
        reused only when it still matches the manifest exactly.  A partial
        bundle is resumed only when the source confirms, with a matching
        validator, that the retained bytes belong to the requested
        representation.
        """
        self._check_manifest(manifest)
        destination, partial_path, state_path = self._paths(destination)
        destination = Path(destination)

        if destination.exists():
            if self._file_matches(destination, manifest):
                return destination
            raise DownloadError('destination contains different content')

        state = self._read_state(state_path, partial_path)
        if state is not None and not self._state_matches(state, manifest):
            self._clear(partial_path, state_path)
            state = None

        last_error = None
        for _ in range(self.attempts):
            state = self._read_state(state_path, partial_path)
            if state is not None and not self._state_matches(state, manifest):
                self._clear(partial_path, state_path)
                state = None
            try:
                result = self._attempt(manifest, state, destination,
                                       partial_path, state_path, progress)
                if result is not None:
                    return result
            except UnsupportedResponse as error:
                # The attempt did not yield usable content.  ``_attempt``
                # only clears the retained bytes when the source proved they
                # belong to a different representation; a transient error
                # page therefore leaves a valid partial transfer intact.
                last_error = error
            except DownloadInterrupted as error:
                last_error = error
            except (HTTPError, URLError) + _TRANSPORT_ERRORS as error:
                last_error = DownloadInterrupted(str(error))
        if last_error is not None:
            raise last_error
        raise DownloadInterrupted('bundle was not acquired')

    def discard(self, destination):
        """Forget resumable evidence for ``destination``.

        This only removes unverified bytes; verified packages and received
        material are untouched.
        """
        destination, partial_path, state_path = self._paths(destination)
        self._clear(partial_path, state_path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _check_manifest(manifest):
        for key in ('asset', 'version', 'url', 'size', 'sha256', 'files'):
            if key not in manifest:
                raise DownloadError(f'manifest missing {key}')
        if not isinstance(manifest['size'], int) or manifest['size'] < 0:
            raise DownloadError('manifest size is invalid')
        if not isinstance(manifest['sha256'], str) or not _HEX_DIGEST.fullmatch(manifest['sha256']):
            raise DownloadError('manifest digest is invalid')
        if not isinstance(manifest['files'], dict):
            raise DownloadError('manifest files are invalid')

    @staticmethod
    def _state_matches(state, manifest):
        return (state.get('asset') == manifest['asset']
                and state.get('version') == manifest['version']
                and state.get('sha256') == manifest['sha256']
                and state.get('size') == manifest['size'])

    @staticmethod
    def _file_matches(path, manifest):
        try:
            if path.stat().st_size != manifest['size']:
                return False
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(65536), b''):
                    digest.update(block)
            return digest.hexdigest() == manifest['sha256']
        except OSError:
            return False

    def _new_state(self, manifest, url, etag):
        return {'format': 1, 'asset': manifest['asset'],
                'version': manifest['version'], 'sha256': manifest['sha256'],
                'size': manifest['size'], 'url': url,
                'etag': _validator(etag), 'offset': 0}

    # How many retained bytes are re-requested to prove the source still
    # serves the same representation before bytes are appended.
    OVERLAP = 4096

    def _attempt(self, manifest, state, destination, partial_path,
                 state_path, progress):
        offset = int(state.get('offset', 0)) if state else 0
        overlap = 0
        wanted_start = 0
        headers = {}
        if offset:
            # Resume with an overlapping range.  If-Range is advisory: a
            # rotated validator makes the origin send the whole body (200),
            # which is accepted.  The bytes in the overlap are the evidence
            # that actually decides whether appending is safe, so resume
            # works even when the source never supplied a validator.
            overlap = min(offset, self.OVERLAP)
            wanted_start = offset - overlap
            headers['Range'] = f'bytes={wanted_start}-'
            validator = _validator(state.get('etag'))
            if validator:
                headers['If-Range'] = validator
        if offset > manifest['size']:
            self._clear(partial_path, state_path)
            state = None
            offset = 0
            overlap = 0

        # The current manifest always carries the active signed URL.  The
        # URL recorded with the partial bytes is evidence from an earlier
        # attempt but is not preferred over a freshly issued one.
        url = manifest['url']
        request = Request(url, headers=headers) if headers else Request(url)
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            if error.code == 416 and offset:
                # The retained bytes no longer fit the representation the
                # source is offering.
                self._clear(partial_path, state_path)
                return None
            # A rejected or expired signed URL does not invalidate the bytes
            # already retained: the caller can obtain a fresh URL for the
            # same version and continue from the recorded offset.
            raise

        with response:
            status = getattr(response, 'status', 200)
            content_type = response.headers.get_content_type()
            etag = _validator(response.headers.get('ETag'))
            length_header = response.headers.get('Content-Length')

            if content_type not in ('application/zip', 'application/octet-stream'):
                # Error pages (HTML, JSON, ...) are never written as material.
                if status == 206:
                    self._clear(partial_path, state_path)
                raise UnsupportedResponse('unexpected download content type')

            if status == 206 and offset:
                content_range = response.headers.get('Content-Range')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range or '')
                if not match:
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('invalid partial response')
                start, stop, total = (int(value) for value in match.groups())
                if (start != wanted_start or total != manifest['size']
                        or stop < start or stop >= total or stop < offset - 1):
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('partial response does not continue the bundle')
                if length_header is not None and int(length_header) != stop - start + 1:
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('partial response length mismatch')
                # The body starts ``overlap`` bytes before the resume point.
                # Those bytes must equal the retained tail: that is real
                # evidence from the source that the representation still
                # continues what is already on disk.  A changed validator
                # alone means nothing, and a matching validator alone is not
                # proof either.
                try:
                    with partial_path.open('rb') as handle:
                        handle.seek(offset - overlap)
                        retained = handle.read(overlap)
                except OSError:
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('retained bytes are unreadable')
                confirmed = response.read(overlap)
                if len(confirmed) != overlap or confirmed != retained:
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('retained bytes do not match the source')
                mode = 'ab'
            elif status == 200:
                # Fresh download, or the origin ignored Range and sent the
                # whole representation.  Either way the old bytes are
                # replaced, never appended to.
                if length_header is not None and int(length_header) != manifest['size']:
                    self._clear(partial_path, state_path)
                    raise UnsupportedResponse('full response length mismatch')
                mode = 'wb'
                offset = 0
                state = None
            elif status == 206:
                self._clear(partial_path, state_path)
                raise UnsupportedResponse('unsolicited partial response')
            else:
                raise UnsupportedResponse(f'unexpected response status {status}')
            if state is None:
                state = self._new_state(manifest, url, etag)
            else:
                state['url'] = url
                if etag:
                    state['etag'] = etag
            state['offset'] = offset
            self._write_state(state_path, state)

            expected = manifest['size']
            try:
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                with partial_path.open(mode) as output:
                    size = offset
                    try:
                        while True:
                            block = response.read(65536)
                            if not block:
                                break
                            output.write(block)
                            output.flush()
                            try:
                                os.fsync(output.fileno())
                            except OSError:
                                pass
                            size += len(block)
                            if size > expected:
                                raise UnsupportedResponse('response exceeded declared size')
                            # Persist progress only after the bytes are on
                            # disk, so a crash cannot claim more than exists.
                            state['offset'] = size
                            self._write_state(state_path, state)
                            if progress is not None:
                                progress(size, expected)
                    except UnsupportedResponse:
                        self._clear(partial_path, state_path)
                        raise
                    except _TRANSPORT_ERRORS as error:
                        state['offset'] = size
                        self._write_state(state_path, state)
                        raise DownloadInterrupted(str(error))
            except UnsupportedResponse:
                raise

            if size < expected:
                state['offset'] = size
                self._write_state(state_path, state)
                raise DownloadInterrupted('connection ended before the bundle was complete')

            # The byte count now lines up, but only the digest decides
            # whether the content is the requested bundle.
            digest = hashlib.sha256()
            try:
                with partial_path.open('rb') as handle:
                    for block in iter(lambda: handle.read(65536), b''):
                        digest.update(block)
                    actual_size = handle.tell()
            except OSError as error:
                raise DownloadInterrupted(str(error))
            if actual_size != manifest['size'] or digest.hexdigest() != manifest['sha256']:
                self._clear(partial_path, state_path)
                raise UnsupportedResponse('bundle size or digest mismatch')

            os.replace(partial_path, destination)
            state_path.unlink(missing_ok=True)
            return Path(destination)
