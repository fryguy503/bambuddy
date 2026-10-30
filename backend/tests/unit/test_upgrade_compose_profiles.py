"""Parse deployment profiles with Compose without contacting a Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCKER = shutil.which("docker")
pytestmark = pytest.mark.skipif(DOCKER is None, reason="Docker Compose CLI is unavailable")


def compose_config(tmp_path, profile, overrides=None):
    assert DOCKER is not None
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("BAMBUDDY_")}
    environment.update(overrides or {})
    return subprocess.run(
        [DOCKER, "compose", "--env-file", str(empty_env), "-f", str(ROOT / profile), "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("profile", ["docker-compose.synology.yml", "docker-compose.testing.yml"])
def test_upgrade_requires_an_explicit_bind_address(tmp_path, profile):
    result = compose_config(tmp_path, profile)
    assert result.returncode != 0
    assert "BAMBUDDY_BIND_ADDRESS" in result.stderr


@pytest.mark.parametrize("target", ["synology", "testing"])
def test_upgrade_profiles_reuse_existing_storage_and_https_port(tmp_path, target):
    result = compose_config(tmp_path, f"docker-compose.{target}.yml", {"BAMBUDDY_BIND_ADDRESS": "192.0.2.20"})
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    service = config["services"]["bambuddy"]
    prefix = "bambuddy-synology" if target == "synology" else "bambuddy-temp"
    assert config["name"] == prefix
    assert service["image"] == "bambuddy:printer-modularization-https"
    assert service["pull_policy"] == "never"
    for kind in ("data", "logs"):
        volume = config["volumes"][f"bambuddy_{kind}"]
        assert volume["external"] is True
        assert volume["name"] == f"{prefix}_bambuddy_{kind}"
    assert service["environment"]["BAMBUDDY_HTTPS"] == "true"
    assert service["environment"]["BAMBUDDY_HTTPS_HOSTNAMES"] == "192.0.2.20"
    if target == "synology":
        assert service["network_mode"] == "host"
        assert not service.get("ports")
        assert service["environment"]["HOST"] == "192.0.2.20"
        assert service["environment"]["PORT"] == "18443"
    else:
        port = service["ports"][0]
        assert (port["host_ip"], port["published"], port["target"]) == ("192.0.2.20", "18443", 8000)
        assert config["networks"]["default"]["name"] == "bambuddy-temp_default"
        assert config["networks"]["default"]["external"] is True


@pytest.mark.parametrize("target", ["synology", "testing"])
def test_upgrade_retains_operator_storage_image_and_ownership_overrides(tmp_path, target):
    result = compose_config(
        tmp_path,
        f"docker-compose.{target}.yml",
        {
            "BAMBUDDY_BIND_ADDRESS": "192.0.2.20",
            "BAMBUDDY_DATA_VOLUME": "existing-data",
            "BAMBUDDY_LOGS_VOLUME": "existing-logs",
            "BAMBUDDY_NETWORK": "existing-network",
            "BAMBUDDY_IMAGE": "bambuddy:reviewed-build",
            "PUID": "1026",
            "PGID": "100",
        },
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    service = config["services"]["bambuddy"]
    assert service["image"] == "bambuddy:reviewed-build"
    assert service["environment"]["PUID"] == "1026"
    assert service["environment"]["PGID"] == "100"
    assert config["volumes"]["bambuddy_data"]["name"] == "existing-data"
    assert config["volumes"]["bambuddy_logs"]["name"] == "existing-logs"
    if target == "testing":
        assert config["networks"]["default"]["name"] == "existing-network"
