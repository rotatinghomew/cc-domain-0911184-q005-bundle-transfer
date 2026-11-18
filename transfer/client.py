"""Client for the source's manifest endpoint.

The manifest is addressed by logical asset *and exact version*.  When a
version is not retained the source answers 404; this is surfaced as
:class:`VersionUnavailable` rather than silently returning the current (or
newest retained) version.  That distinction is what lets a delta refuse to
build on a version it did not ask for.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import PackageError, VersionUnavailable
from .package import validate_manifest


class ManifestClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _request(self, url):
        request = Request(url, headers={'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, 'status', 200)
                content_type = response.headers.get_content_type()
                body = response.read()
        except HTTPError as error:
            if error.code == 404:
                raise VersionUnavailable('manifest version is not available') from error
            raise
        except URLError as error:
            raise PackageError('manifest endpoint is unreachable') from error
        if status != 200:
            raise PackageError(f'unexpected manifest status {status}')
        if content_type not in ('application/json', 'text/json'):
            # Error pages are not manifests.
            raise PackageError('manifest endpoint did not return JSON')
        try:
            manifest = json.loads(body)
        except ValueError as error:
            raise PackageError('manifest is not valid JSON') from error
        if not isinstance(manifest, dict):
            raise PackageError('manifest must be an object')
        return manifest

    def manifest(self, version=None, manifest_url=None):
        url = manifest_url or self.base_url + '/manifest'
        if version is not None:
            separator = '&' if '?' in url else '?'
            url = f'{url}{separator}version={int(version)}'
        manifest = self._request(url)
        validate_manifest(manifest)
        if version is not None and manifest.get('version') != int(version):
            raise PackageError('source returned a different version than requested')
        return manifest

    def current(self, asset=None):
        """Return the currently published manifest.

        ``asset`` is only used for the caller's bookkeeping: the source in
        this project publishes one logical asset per endpoint.  A mismatch is
        rejected so a manifest for another asset is never received here.
        """
        manifest = self.manifest()
        if asset is not None and manifest.get('asset') != asset:
            raise PackageError('manifest belongs to another asset')
        return manifest


# Common alternative spelling.
SourceClient = ManifestClient
