"""Synthetic native-shape cloud config archives; no accounts or network calls."""

import io
import json
import stat
import unittest
import zipfile
from typing import Any
from unittest.mock import AsyncMock, patch

from backend.app.services.anycubic_cloud import AnycubicCloudAuthError
from backend.app.services.anycubic_cloud_profiles import (
    CONFIG_ROUTE,
    AnycubicProfileError,
    load_profiles,
    parse_config_archive,
)

NAMESPACE = "anycubic:global:123"
URL = "https://workbentch.s3.us-east-2.amazonaws.com/public/fixture/paramInfo.zip?signature=fixture"


def archive(entries: list[tuple[str | zipfile.ZipInfo, Any]]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in entries:
            payload = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            bundle.writestr(name, payload)
    return result.getvalue()


def preset(kind="filament", name="Custom PLA", **changes):
    return {
        "name": name,
        "type": kind,
        "version": "2.0.0.3",
        "inherits": "Anycubic Generic PLA",
        "from": "User",
        "filament_type": ["PLA Matte"],
        "nozzle_temperature": ["215"],
        **changes,
    }


class ArchiveTests(unittest.TestCase):
    def test_all_native_preset_kinds_and_nested_account_root(self):
        bundle = archive(
            [
                ("user/123/filament/custom.json", preset()),
                ("user/123/machine/s1.json", preset("machine", "Kobra S1 custom", printer_model="Anycubic Kobra S1")),
                ("user/123/process/fine.json", preset("process", "Fine 0.08", layer_height="0.08")),
            ]
        )
        profiles = parse_config_archive(bundle, NAMESPACE)
        self.assertEqual({p["type"] for p in profiles}, {"filament", "printer", "process"})
        self.assertEqual(len({p["setting_id"] for p in profiles}), 3)
        self.assertTrue(all(p["setting_id"].startswith("anycubic_") and "/" not in p["setting_id"] for p in profiles))

    def test_preserves_name_inheritance_version_and_original_options(self):
        original = preset(name="ユーザー PLA+ @ S1", enable_pressure_advance=["1"], pressure_advance=["0.034"])
        profile = parse_config_archive(archive([("filament/custom.json", original)]), NAMESPACE)[0]
        self.assertEqual(profile["name"], original["name"])
        self.assertEqual(profile["version"], "2.0.0.3")
        self.assertEqual(profile["inherits"], "Anycubic Generic PLA")
        self.assertEqual(profile["setting"], original)
        self.assertEqual(profile["setting"]["filament_type"], ["PLA Matte"])

    def test_exact_source_setting_id_types_stay_distinct(self):
        integer = parse_config_archive(archive([("filament/p.json", preset(setting_id=7))]), NAMESPACE)[0]
        string = parse_config_archive(archive([("filament/p.json", preset(setting_id="7"))]), NAMESPACE)[0]
        self.assertNotEqual(integer["setting_id"], string["setting_id"])
        self.assertEqual(integer["source_id"], "7")
        self.assertEqual(integer["source_id_type"], "integer")
        self.assertEqual(integer["setting"]["setting_id"], 7)
        self.assertEqual(string["source_id_type"], "string")

    def test_server_id_is_not_invented_from_display_name_or_filament_id(self):
        profile = parse_config_archive(
            archive([("filament/p.json", preset(filament_id="material-identifier"))]), NAMESPACE
        )[0]
        self.assertNotIn("source_id", profile)
        self.assertEqual(profile["source_path"], "filament/p.json")

    def test_ids_are_stable_for_option_updates_but_bound_to_account_region_path_and_source(self):
        def identity(namespace=NAMESPACE, path="filament/p.json", **changes):
            return parse_config_archive(archive([(path, preset(setting_id="server-id", **changes))]), namespace)[0][
                "setting_id"
            ]

        expected = identity()
        self.assertEqual(expected, identity(nozzle_temperature=["220"]))
        self.assertNotEqual(expected, identity(namespace="anycubic:global:124"))
        self.assertNotEqual(expected, identity(namespace="anycubic:cn:123"))
        self.assertNotEqual(expected, identity(path="filament/renamed.json"))

    def test_same_display_name_different_files_do_not_collapse(self):
        profiles = parse_config_archive(
            archive([("filament/a.json", preset()), ("filament/b.json", preset())]), NAMESPACE
        )
        self.assertEqual(len({p["setting_id"] for p in profiles}), 2)

    def test_unknown_preset_kind_preserves_original_and_is_explicitly_unsupported(self):
        original = preset("future_kind", "Future preset")
        result = parse_config_archive(archive([("filament/future.json", original)]), NAMESPACE)[0]
        self.assertEqual(result["type"], "future_kind")
        self.assertEqual(result["source_type"], "future_kind")
        self.assertEqual(result["setting"], original)

    def test_legacy_json_without_type_uses_known_directory(self):
        value = preset()
        del value["type"]
        result = parse_config_archive(archive([("user/filament/p.json", value)]), NAMESPACE)[0]
        self.assertEqual(result["type"], "filament")

    def test_typed_profile_without_name_uses_native_filename(self):
        value = preset()
        del value["name"]
        result = parse_config_archive(archive([("filament/Special PLA.json", value)]), NAMESPACE)[0]
        self.assertEqual(result["name"], "Special PLA")
        self.assertNotIn("name", result["setting"])

    def test_application_metadata_is_not_presented_as_a_preset(self):
        result = parse_config_archive(
            archive(
                [
                    ("settings.json", {"window": [100, 100]}),
                    ("account.json", {"name": "Account metadata", "token": "synthetic-private-value"}),
                    ("filament/index.json", {"count": 0}),
                    ("filament/p.info", "sync_info=update"),
                    ("filament/", b""),
                ]
            ),
            NAMESPACE,
        )
        self.assertEqual(result, [])

    def test_known_kind_directory_conflict_is_rejected(self):
        with self.assertRaises(AnycubicProfileError):
            parse_config_archive(archive([("filament/p.json", preset("machine"))]), NAMESPACE)

    def test_utf8_bom_is_supported(self):
        bundle = archive([("filament/p.json", b"\xef\xbb\xbf" + json.dumps(preset()).encode())])
        self.assertEqual(len(parse_config_archive(bundle, NAMESPACE)), 1)

    def test_invalid_json_and_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for value in (
            b"{bad",
            b'{"name":"one","name":"two"}',
            b'{"name":"bad","value":NaN}',
            b'{"name":"bad","value":1e999}',
            b"[]",
            b"\xff\xfe\xff",
        ):
            with self.subTest(value=value), self.assertRaises(AnycubicProfileError):
                parse_config_archive(archive([("filament/p.json", value)]), NAMESPACE)

    def test_invalid_types_names_source_ids_and_lone_surrogates_are_rejected(self):
        for value in (
            preset(name=""),
            preset(name="bad\nname"),
            preset(type=True),
            preset(setting_id=True),
            preset(setting_id={"id": 1}),
        ):
            with self.subTest(value=value), self.assertRaises(AnycubicProfileError):
                parse_config_archive(archive([("filament/p.json", value)]), NAMESPACE)
        with self.assertRaises(AnycubicProfileError):
            parse_config_archive(
                archive([("filament/p.json", b'{"name":"name","type":"filament","value":"\\ud800"}')]), NAMESPACE
            )

    def test_unsafe_archive_paths_and_duplicate_casefold_paths_are_rejected(self):
        for path in (
            "../filament/p.json",
            "/filament/p.json",
            "C:/filament/p.json",
            "filament//p.json",
            "filament/./p.json",
            "filament/../p.json",
        ):
            with self.subTest(path=path), self.assertRaises(AnycubicProfileError):
                parse_config_archive(archive([(path, preset())]), NAMESPACE)
        with self.assertRaises(AnycubicProfileError):
            parse_config_archive(archive([("filament/p.json", preset()), ("filament/P.json", preset())]), NAMESPACE)

    def test_raw_zip_backslashes_and_nul_names_are_rejected(self):
        # ZipFile.writestr sanitizes Windows separators and NULs while creating
        # fixtures. Alter the same-length local/central names to model an
        # untrusted producer rather than testing that constructor sanitization.
        safe = b"filament/pXhidden.json"
        bundle = archive([(safe.decode(), preset())])
        for unsafe in (b"filament\\pXhidden.json", b"filament/p\x00hidden.json"):
            with self.subTest(unsafe=unsafe), self.assertRaises(AnycubicProfileError):
                parse_config_archive(bundle.replace(safe, unsafe), NAMESPACE)

    def test_symlinks_are_rejected_without_filesystem_extraction(self):
        link = zipfile.ZipInfo("filament/link.json")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(AnycubicProfileError):
            parse_config_archive(archive([(link, b"/etc/passwd")]), NAMESPACE)

    def test_archive_file_entry_and_expanded_limits(self):
        bundle = archive([("filament/p.json", preset())])
        for setting, maximum in (
            ("MAX_ARCHIVE_BYTES", 1),
            ("MAX_FILE_BYTES", 1),
            ("MAX_EXPANDED_BYTES", 1),
            ("MAX_ENTRIES", 0),
        ):
            with (
                self.subTest(setting=setting),
                patch("backend.app.services.anycubic_cloud_profiles." + setting, maximum),
                self.assertRaises(AnycubicProfileError),
            ):
                parse_config_archive(bundle, NAMESPACE)

    def test_empty_zip_is_a_truthful_empty_inventory(self):
        self.assertEqual(parse_config_archive(archive([]), NAMESPACE), [])
        for raw in (b"", b"not a ZIP", b"PK\x03\x04malformed"):
            with self.subTest(raw=raw), self.assertRaises(AnycubicProfileError):
                parse_config_archive(raw, NAMESPACE)

    def test_nested_json_limit(self):
        value: Any = "nested"
        for _ in range(40):
            value = {"child": value}
        with self.assertRaises(AnycubicProfileError):
            parse_config_archive(archive([("filament/p.json", value)]), NAMESPACE)


class Transport:
    def __init__(self):
        self.validated = True
        self.namespace = NAMESPACE
        self.validate_token = AsyncMock(side_effect=self._validate)
        self.request = AsyncMock(return_value={"code": 1, "data": {"url": URL, "preSignUrl": "upload-only"}})
        self.download_config = AsyncMock(return_value=archive([("filament/p.json", preset())]))

    def _validate(self):
        self.validated = True
        return {"user_id": "123"}

    @property
    def account_namespace(self):
        if not self.validated:
            raise AnycubicCloudAuthError()
        return self.namespace


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_uses_exact_read_only_native_route_and_data_url(self):
        service = Transport()
        result = await load_profiles(service)
        service.request.assert_awaited_once_with("POST", CONFIG_ROUTE)
        service.download_config.assert_awaited_once_with(URL)
        self.assertEqual(len(result), 1)
        self.assertNotIn("url", result[0])
        self.assertNotIn("signature", repr(result))

    async def test_already_validated_service_does_not_duplicate_identity_request(self):
        service = Transport()
        await load_profiles(service)
        service.validate_token.assert_not_awaited()

    async def test_unvalidated_service_validates_before_fetching_config(self):
        service = Transport()
        service.validated = False
        await load_profiles(service)
        service.validate_token.assert_awaited_once()

    async def test_missing_archive_reference_is_empty_not_a_fabricated_catalog(self):
        for data in (None, {}, {"url": ""}):
            service = Transport()
            service.request.return_value = {"code": 1, "data": data}
            self.assertEqual(await load_profiles(service), [])
            service.download_config.assert_not_awaited()

    async def test_error_or_malformed_config_is_not_treated_as_empty(self):
        for response in (
            {"code": 0, "data": None},
            {"code": True},
            {"data": None},
            {"code": 1, "data": []},
            {"code": 1, "data": {"url": 42}},
        ):
            service = Transport()
            service.request.return_value = response
            with self.subTest(response=response), self.assertRaises(AnycubicProfileError):
                await load_profiles(service)
            service.download_config.assert_not_awaited()

    async def test_changed_account_cannot_receive_old_account_presets(self):
        service = Transport()

        async def download(url):
            service.namespace = "anycubic:global:999"
            return archive([("filament/p.json", preset())])

        service.download_config.side_effect = download
        with self.assertRaises(AnycubicProfileError):
            await load_profiles(service)

    async def test_transport_auth_failure_propagates_before_config_request(self):
        service = Transport()
        service.validated = False
        service.validate_token.side_effect = AnycubicCloudAuthError()
        with self.assertRaises(AnycubicCloudAuthError):
            await load_profiles(service)
        service.request.assert_not_awaited()
