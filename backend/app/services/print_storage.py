"""Compatibility alias for the shared printer addon's storage API."""

import sys
from typing import TYPE_CHECKING

from backend.app.printers import storage as _implementation

if TYPE_CHECKING:
    from backend.app.printers.storage import (
        REASON_FTP_TRANSFER_FAILED as REASON_FTP_TRANSFER_FAILED,
        REASON_FTPS_COOLOFF as REASON_FTPS_COOLOFF,
        REASON_INTERNAL_HISTORY as REASON_INTERNAL_HISTORY,
        REASON_INTERNAL_STORAGE as REASON_INTERNAL_STORAGE,
        REASON_NO_EXTERNAL_STORAGE as REASON_NO_EXTERNAL_STORAGE,
        StorageVerdict as StorageVerdict,
        external_storage_present as external_storage_present,
        ftp_probe_paths as ftp_probe_paths,
        last_print_storage_verdict as last_print_storage_verdict,
        print_file_reachable_over_ftp as print_file_reachable_over_ftp,
        probe_filename_from_url as probe_filename_from_url,
        url_is_external_storage as url_is_external_storage,
    )

sys.modules[__name__] = _implementation
