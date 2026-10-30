"""Compatibility alias for :mod:`backend.app.printers.bambu.camera`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "camera")

if TYPE_CHECKING:
    from backend.app.printers.bambu.camera import (
        _active_capture_pids as _active_capture_pids,
        apply_camera_rotation as apply_camera_rotation,
        apply_camera_rotation_to_file as apply_camera_rotation_to_file,
        capture_camera_frame_bytes as capture_camera_frame_bytes,
        capture_in_flight as capture_in_flight,
        close_tls_proxy as close_tls_proxy,
        create_tls_proxy as create_tls_proxy,
        extract_video_last_frame as extract_video_last_frame,
        generate_chamber_image_stream as generate_chamber_image_stream,
        get_camera_port as get_camera_port,
        get_ffmpeg_path as get_ffmpeg_path,
        is_chamber_image_model as is_chamber_image_model,
        read_next_chamber_frame as read_next_chamber_frame,
        rtsp_socket_timeout_flag as rtsp_socket_timeout_flag,
    )

sys.modules[__name__] = _implementation
