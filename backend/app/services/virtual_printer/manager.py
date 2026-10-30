"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.manager`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.manager")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.manager import (
        _RECENT_QUEUE_ITEM_TTL as _RECENT_QUEUE_ITEM_TTL,
        _SLICER_OPTIONS_CACHE_LIMIT as _SLICER_OPTIONS_CACHE_LIMIT,
        _SLICER_OPTIONS_WAIT_TIMEOUT as _SLICER_OPTIONS_WAIT_TIMEOUT,
        _TRISTATE_INT as _TRISTATE_INT,
        DEFAULT_VIRTUAL_PRINTER_MODEL as DEFAULT_VIRTUAL_PRINTER_MODEL,
        DISPLAY_NAME_TO_MODEL_CODE as DISPLAY_NAME_TO_MODEL_CODE,
        MODEL_SERIAL_PREFIXES as MODEL_SERIAL_PREFIXES,
        VIRTUAL_PRINTER_MODELS as VIRTUAL_PRINTER_MODELS,
        VP_MODE_ARCHIVE as VP_MODE_ARCHIVE,
        VP_MODE_PROXY as VP_MODE_PROXY,
        VP_MODE_QUEUE as VP_MODE_QUEUE,
        BindServer as BindServer,
        Callable as Callable,
        CertificateService as CertificateService,
        MQTTBridge as MQTTBridge,
        Path as Path,
        SimpleMQTTServer as SimpleMQTTServer,
        SlicerProxyManager as SlicerProxyManager,
        SSDPProxy as SSDPProxy,
        TCPProxy as TCPProxy,
        VirtualPrinterFTPServer as VirtualPrinterFTPServer,
        VirtualPrinterInstance as VirtualPrinterInstance,
        VirtualPrinterManager as VirtualPrinterManager,
        VirtualPrinterSSDPServer as VirtualPrinterSSDPServer,
        _extract_slicer_ams_mapping_json as _extract_slicer_ams_mapping_json,
        _get_serial_for_model as _get_serial_for_model,
        _tristate_from_slicer as _tristate_from_slicer,
        app_settings as app_settings,
        asyncio as asyncio,
        compute_passive_port_slice as compute_passive_port_slice,
        datetime as datetime,
        is_nozzle_rack_model as is_nozzle_rack_model,
        json as json,
        logger as logger,
        logging as logging,
        normalize_vp_mode as normalize_vp_mode,
        resolve_external_spools_in_mapping as resolve_external_spools_in_mapping,
        time as time,
        timezone as timezone,
        virtual_printer_manager as virtual_printer_manager,
    )

sys.modules[__name__] = _implementation
