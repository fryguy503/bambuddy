"""Common file service results and failure values used by printer addons."""

from dataclasses import dataclass
from enum import Enum


class UploadCancelled(Exception):
    """Raised inside the upload worker to abort an in-flight transfer.

    ``upload_file`` treats any exception from its progress callback as "stop
    now": it breaks out of the send loop, deletes the partial file from the
    printer, and re-raises. That is the only way to stop a transfer — an
    executor thread cannot be cancelled from the event loop, so a bare
    ``asyncio.wait_for`` leaves it streaming (see ``upload_file_async``).
    """


class DownloadCancelled(Exception):
    """Raised in an FTP callback to stop a disk-backed download cooperatively."""


class DownloadLimitExceeded(Exception):
    """Raised before an FTP callback writes beyond its caller-supplied limit."""


class DownloadInsufficientSpace(Exception):
    """Raised before an FTP callback consumes the application's disk reserve."""


class DownloadDeadlineExceeded(Exception):
    """A transfer overran the deadline derived from the size the printer reported.

    Never retried, for the reason ``UploadCancelled`` is not (#2529): the
    deadline was already stretched to fit the file at a floor rate no working
    link falls below, so another attempt would spend another full deadline
    reaching the same conclusion -- and ``on_print_start`` spends it holding a
    pooled database connection. Raised instead of returning False so
    ``with_ftp_retry`` can tell this apart from an ordinary failed attempt
    (#2957); callers that do not retry see it through their existing handlers,
    which is why the archive flow advances to its next candidate path.
    """


@dataclass(frozen=True)
class FileListResult:
    """A directory listing that distinguishes empty from unreachable."""

    files: list[dict]
    available: bool
    # The printer did not answer within the wait: slow, not necessarily gone
    # (a large /ipcam on a slow card), so not reported as unavailable (TO-C13).
    timed_out: bool = False
    # The printer answered that the directory does not exist (550). Still not
    # ``available``, but an answer about the card, not a failure to reach it.
    missing: bool = False


class DeleteResult(Enum):
    """Outcome of an FTP delete attempt.

    Distinguishes "file isn't on the printer" (550, recovery impossible by
    retrying) from "delete failed for some other reason" (network, auth,
    transient FTP error — worth retrying). The post-print SD-card cleanup in
    main.py used to flatten both into ``False`` and log a "may linger" WARNING
    on every successful print where the printer self-cleaned its SD card
    before our cleanup ran (#1721 reporter's A1).
    """

    DELETED = "deleted"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class FtpFailureKind(Enum):
    """Why an FTP operation failed, at the granularity the client can tell.

    ``connect`` and ``upload_file`` already separate every one of these -- each
    has its own log line, and 553 even gets a spelled-out list of storage
    causes -- and then both returned a bare ``False``. So the dispatch that
    reports the failure to the operator had nothing to go on, and used one
    string for all of them: "check if SD card is inserted and properly
    formatted". #2899's reporter acted on that after a TLS handshake failure
    and restarted the printer, which could not have helped: the handshake never
    got near the printer's filesystem.
    """

    COOLOFF = "cooloff"  # skipped without contacting the printer (#2780)
    HANDSHAKE = "handshake"  # port 990 answered with something that is not TLS
    AUTH = "auth"  # permanent refusal, typically a rejected access code
    TIMEOUT = "timeout"
    STORAGE = "storage"  # 553/552 -- the case the SD-card advice was written for
    NOT_FOUND = "not_found"  # 550
    NETWORK = "network"  # socket dropped, or an FTP error with no clearer reading
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FtpFailure:
    """What went wrong, kept next to the log line that already said it."""

    kind: FtpFailureKind
    detail: str
    code: str | None = None  # FTP reply code where the server gave one


@dataclass
class FtpFailureReport:
    """A slot the *caller* owns for the reason its upload failed.

    Deliberately not a per-IP dict on the client, the way ``_mode_cache`` and
    ``_handshake_blocked_until`` are. Those describe a printer, and are
    correct to share. This describes one operation, and a background timelapse
    fetch running beside a dispatch would overwrite the dispatch's reason with
    its own -- reporting the wrong cause with total confidence, which is the
    bug being fixed rather than a new way to hit it (#2899).
    """

    failure: FtpFailure | None = None


class FileNotOnPrinterError(Exception):
    """Raised when a remote FTP path returns 550 (file not found).

    550 means the file does not exist at that path — retrying the same path
    will never succeed. Callers use this sentinel with with_ftp_retry's
    non_retry_exceptions to immediately move on to the next candidate path
    instead of burning the full retry budget (up to 11 × 30s per path) on
    a lookup that cannot recover.
    """
