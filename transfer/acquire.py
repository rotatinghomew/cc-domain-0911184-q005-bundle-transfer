"""End-to-end acquisition of a logical asset version.

``Acquisition`` ties the pieces together and keeps the three states apart:

1. :class:`transfer.fetch.Downloader` writes resumable, unverified bytes;
2. :func:`transfer.package.inspect_bundle` verifies the package (and, for a
   delta, composes the complete final set from the exact base version);
3. :class:`transfer.receiving.Receiving` publishes the version to the
   receiving list in one transaction.

A delta that names a base version which is not available is fetched when the
source still exposes that *exact* version.  The locally newest version is
never used as a substitute.  A failure on one version leaves everything
already received intact and is recorded with the reason it stopped.
"""

import hashlib
from pathlib import Path

from .client import ManifestClient
from .errors import (DownloadFailed, IntegrityError, MissingBaseVersion,
                     PackageError, RevokedVersion, TransferError,
                     VersionUnavailable)
from .fetch import Downloader
from .package import inspect_bundle, validate_manifest
from .receiving import Receiving


class Acquisition:
    """Acquire one logical asset version end to end.

    The manifest source is either an explicit ``manifest`` for the first
    call or a :class:`ManifestClient` that can fetch arbitrary exact
    versions.  Working state (unverified partial bytes, verified packages)
    lives under ``workdir`` and is never part of the receiving store.
    """

    def __init__(self, receiving, downloader=None, client=None, workdir=None,
                 max_depth=8):
        self.receiving = receiving
        self.downloader = downloader or Downloader()
        self.client = client
        if workdir is None:
            # A stable location is required for resumption to survive the
            # process exiting: the unverified bytes and their state file must
            # still be found by the next run.
            workdir = receiving.root / 'packages.pending'
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.max_depth = max_depth

    # ------------------------------------------------------------------
    # Manifest selection
    # ------------------------------------------------------------------
    def _manifest(self, manifest, version):
        if manifest is not None:
            validate_manifest(manifest)
            if version is not None and manifest['version'] != int(version):
                raise PackageError('manifest version does not match the request')
            return manifest
        if self.client is None:
            raise PackageError('a manifest or a manifest client is required')
        if version is None:
            return self.client.current()
        return self.client.manifest(version)

    def _package_path(self, manifest):
        sha = manifest.get('sha256')
        if isinstance(sha, str) and len(sha) == 64:
            name = sha
        else:
            name = f"v{manifest['version']}-{manifest.get('url', '')}".encode()
            name = hashlib.sha256(name).hexdigest()
        return self.workdir / (name + '.zip')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def collect(self, manifest=None, version=None):
        """Acquire one version and return its receipt.

        The receipt only exists once the complete final file set has been
        verified.  Re-running for an already received version does not add a
        second receipt.
        """
        return self._collect(manifest, version, depth=0)

    acquire = collect

    def _collect(self, manifest, version, depth):
        manifest = self._manifest(manifest, version)
        asset = manifest['asset']
        number = manifest['version']
        if depth > self.max_depth:
            raise PackageError('delta dependency chain is too deep')

        if manifest.get('revoked'):
            self.receiving.record_acquisition(manifest, state='refused',
                                              reason='revoked version')
            raise RevokedVersion('source withdrew this version')

        if self.receiving.received_version(asset, number):
            return self.receiving.receipt(asset, number)

        try:
            base_members = self._base_members(manifest, depth)
            path = self._download(manifest)
            try:
                members = inspect_bundle(path, manifest, base=base_members)
            except TransferError:
                # A corrupt or inconsistent package must not be reused on a
                # retry; partial transfer state is handled by the downloader
                # and is left alone.
                path.unlink(missing_ok=True)
                raise
            return self.receiving.accept(manifest, members)
        except TransferError as error:
            self.receiving.record_acquisition(
                manifest, state='incomplete', reason=str(error) or type(error).__name__,
                url=manifest.get('url'))
            raise
        except OSError as error:
            # Keep the diagnosis traceable even for local storage failures;
            # nothing was published.
            self.receiving.record_acquisition(
                manifest, state='incomplete',
                reason=f'local storage failure: {error}',
                url=manifest.get('url'))
            raise PackageError(f'local storage failure: {error}') from error

    def _base_members(self, manifest, depth):
        if manifest.get('mode', 'full') != 'delta':
            return None
        base_version = manifest['base_version']
        asset = manifest['asset']
        if self.receiving.received_version(asset, base_version):
            return self.receiving.entries(asset, base_version)
        if self.client is None:
            raise MissingBaseVersion(
                f'version {manifest["version"]} depends on version {base_version}, '
                'which has not been received and no source is configured')
        # Fetch the exact declared dependency.  A 404 or any other version is
        # not an acceptable substitute.
        try:
            base_manifest = self.client.manifest(base_version)
        except VersionUnavailable as error:
            raise MissingBaseVersion(
                f'version {manifest["version"]} depends on version {base_version}, '
                'which the source no longer offers') from error
        if base_manifest.get('asset') != asset:
            raise MissingBaseVersion('base version belongs to another asset')
        if base_manifest.get('revoked'):
            raise MissingBaseVersion(
                f'base version {base_version} has been revoked and cannot be used')
        self.receiving.record_acquisition(
            manifest, state='fetching_base',
            reason=f'fetching dependency version {base_version}',
            url=manifest.get('url'))
        self._collect(base_manifest, None, depth + 1)
        return self.receiving.entries(asset, base_version)

    def _download(self, manifest):
        path = self._package_path(manifest)
        if path.exists():
            try:
                verify = inspect_bundle(path, manifest)
                if verify is not None:
                    self.receiving.record_acquisition(
                        manifest, state='downloaded', reason='reused verified package',
                        url=manifest.get('url'), package_sha256=manifest.get('sha256'))
                    return path
            except TransferError:
                path.unlink(missing_ok=True)
        try:
            self.receiving.record_acquisition(
                manifest, state='downloading', url=manifest.get('url'))
            downloaded = self.downloader.fetch(manifest, path)
        except TransferError as error:
            raise DownloadFailed(f'download failed: {error}') from error
        self.receiving.record_acquisition(
            manifest, state='downloaded', reason='package verified',
            url=manifest.get('url'), package_sha256=manifest.get('sha256'))
        return downloaded


# Convenience functions -----------------------------------------------------

def acquire(base_url, receiving, asset=None, version=None, **kwargs):
    """Fetch (download, verify, receive) one version from ``base_url``."""
    client = kwargs.pop('client', None) or ManifestClient(base_url)
    acquisition = Acquisition(receiving, client=client, **kwargs)
    if version is not None:
        manifest = client.manifest(version)
    else:
        manifest = client.current(asset)
    return acquisition.collect(manifest)


def collect(base_url, receiving, asset=None, version=None, **kwargs):
    return acquire(base_url, receiving, asset=asset, version=version, **kwargs)
