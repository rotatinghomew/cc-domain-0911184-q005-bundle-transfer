"""Shared exception hierarchy.

Everything derives from :class:`ValueError` so callers that already treated
protocol problems as ``ValueError`` keep working.  The more specific classes
exist so that acquisition code can decide whether a failure is worth
retrying and so that diagnostics can expose a stable reason code.
"""


class TransferError(ValueError):
    """Base class for all bundle transfer failures."""


class DownloadError(TransferError):
    """A bundle could not be acquired completely and exactly."""


class DownloadInterrupted(DownloadError):
    """The transport stopped before the complete bundle arrived."""


class DownloadFailed(DownloadError):
    """A bundle could not be downloaded at all."""


class UnsupportedResponse(DownloadError):
    """The origin did not return a usable bundle response."""


class PackageError(TransferError):
    """A package or manifest is structurally invalid."""


class UnsafePath(PackageError):
    """A member or removal path could escape the package."""


class IntegrityError(PackageError):
    """Verified content disagrees with the manifest."""


class MissingBaseVersion(PackageError):
    """A delta cannot be completed without its exact base version."""


class CompositionError(PackageError):
    """A final file set cannot be built without fabricating members."""


class RevokedVersion(TransferError):
    """The source withdrew the version; it must not be delivered."""


class VersionUnavailable(TransferError):
    """The exact requested version is not available."""


class ImmutableVersionConflict(TransferError):
    """A version number was reused with different content."""
