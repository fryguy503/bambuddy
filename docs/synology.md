# Synology: fresh Anycubic testing deployment

Use `docker-compose.synology.yml` for a fresh installation on Synology Container
Manager. It builds the Git checkout, publishes native HTTPS on port 18443 and
keeps data/logs in Docker-managed persistent volumes. It requires no Caddy
container and has no dependency on volumes or networks from the old Linux
testing host.

Synology lists the [DS1819+ as x86_64](https://kb.synology.com/en-global/DSM/tutorial/What_kind_of_CPU_does_my_NAS_have)
and supports it in [Container Manager](https://www.synology.com/en-us/dsm/packages/ContainerManager).
These examples assume Container Manager, SSH, Git, Docker Compose and a
BuildKit-capable builder are available. Check over SSH before building:

```sh
id
uname -m
git --version
sudo docker version
sudo docker compose version
```

Some installations expose `docker-compose` instead of `docker compose`; use the
installed Compose command consistently. The repository Dockerfile uses BuildKit
cache mounts. If the NAS reports a missing builder/buildx component, resolve it
before using the normal build command. Docker 24 installations whose runtime
cannot apply seccomp profiles can use the compatibility build below. Keep this
branch's source image: the upstream release does not contain these changes.

## Initial checkout

Choose a new directory inside your existing Docker shared folder. The example
below assumes that share is `/volume1/docker`; verify that path exists first and
substitute the actual volume path if necessary. Do not reuse a directory that
already contains another application's data.

```sh
test -d /volume1/docker || exit 1
test ! -e /volume1/docker/bambuddy-anycubic || exit 1
sudo mkdir /volume1/docker/bambuddy-anycubic
sudo chown "$(id -u):$(id -g)" /volume1/docker/bambuddy-anycubic
cd /volume1/docker/bambuddy-anycubic

git clone --depth 1 --branch codex/anycubic-https-testing \
  https://github.com/fryguy503/bambuddy.git .
```

Configure the NAS LAN address and the DSM SSH user's numeric IDs in the new
checkout's `.env` file. Replace the example address with your NAS address:

```sh
printf 'BAMBUDDY_BIND_ADDRESS=192.168.1.20\nPUID=%s\nPGID=%s\nTZ=America/New_York\n' \
  "$(id -u)" "$(id -g)" > .env
```

The UID/GID values let the application own its persistent volumes without
running as root. The entrypoint performs initial volume ownership setup, then
generates its private HTTPS key after dropping privileges. Docker creates the
`bambuddy-synology_bambuddy_data` and `bambuddy-synology_bambuddy_logs` volumes;
application data is separate from the source checkout's DSM shared-folder ACLs.

## Build and start

```sh
sudo docker compose -f docker-compose.synology.yml config --quiet
sudo env DOCKER_BUILDKIT=1 docker compose -f docker-compose.synology.yml build bambuddy
sudo docker compose -f docker-compose.synology.yml up -d --no-deps --pull never bambuddy
sudo docker compose -f docker-compose.synology.yml ps
sudo docker compose -f docker-compose.synology.yml logs --tail 80 bambuddy
```

### Docker 24 seccomp compatibility build

If the first `RUN` instruction fails with
`seccomp: config provided but seccomp not supported`, the checkout succeeded but
BuildKit could not start its build process. On Docker 24, use the existing
[classic builder](https://docs.docker.com/reference/cli/docker/image/build/)
through the compatibility helper instead:

```sh
(
set -eu
cd /volume1/docker/bambuddy-anycubic
git pull --ff-only
sudo sh deploy/build-synology.sh
sudo docker compose -f docker-compose.synology.yml up -d --no-build --no-deps --pull never bambuddy
sudo docker compose -f docker-compose.synology.yml ps
sudo docker compose -f docker-compose.synology.yml logs --tail 80 bambuddy
)
```

The helper derives a temporary Dockerfile from the main one, removes only its
two BuildKit dependency-cache mounts, and sets `DOCKER_BUILDKIT=0` for that build.
It removes the temporary file on exit. Dependencies, application code, generated
HTTPS certificate and runtime settings remain the same. It does not change the
Docker daemon configuration or enable privileged containers. Disabling BuildKit
alone with the unmodified Dockerfile would fail on its `RUN --mount` options.
The classic builder is deprecated; this fallback targets the NAS's Docker 24
installation, rather than newer engines that may remove it.

Use `sudo sh deploy/build-synology.sh` for subsequent builds on this NAS, followed
by the same Compose `up --no-build` command. If a build fails, the subshell stops
before starting or replacing the service; retain the checkout, `.env` and
persistent volumes and resume from this section after fixing the reported error.

### Verify the service

Open `https://<NAS-LAN-IP>:18443`. The initial self-signed certificate requires
browser trust. Persistent settings, database, tokens and generated certificate
live in the `bambuddy_data` volume; logs live in `bambuddy_logs`. Compose prefixes
both names with `bambuddy-synology` unless you select a different project name.
These volumes survive container replacement and normal `docker compose down`.
`docker compose down --volumes` deletes them and resets the installation.

Once the service is healthy, export only its public certificate for a verified
HTTPS check. The redirect creates a file owned by the SSH user:

```sh
mkdir -p certs
sudo docker compose -f docker-compose.synology.yml exec -T bambuddy \
  cat /app/data/tls/cert.pem > certs/bambuddy-public-cert.pem
curl --fail --show-error --cacert ./certs/bambuddy-public-cert.pem https://<NAS-LAN-IP>:18443/health
```

### Recover a failed first startup with shared-folder mounts

Earlier revisions mounted `./data` and `./logs`. On a fresh installation,
certificate setup runs before database initialization. If it failed with
`PermissionError` creating `/app/data/tls`, update this fresh installation to the
managed-volume configuration and recreate its container using the image already
built:

```sh
(
set -eu
cd /volume1/docker/bambuddy-anycubic
git pull --ff-only
sudo docker compose -f docker-compose.synology.yml up -d --no-build --no-deps --pull never --wait --wait-timeout 120 bambuddy
sudo docker compose -f docker-compose.synology.yml ps
)
```

This starts separate data/log volumes; files in the earlier `./data` and `./logs`
folders are not imported. For an existing working installation with settings or
print history, migrate that data into the volumes before starting this version;
see Docker's [volume backup and restore guide](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes).
The fresh-install recovery above does not change DSM share permissions.

This Linux profile uses host networking so discovery can enumerate the NAS's
actual interface addresses and netmasks, including secondary addresses. No LAN
subnet or `/24` mask is assumed. The web server binds only to
`BAMBUDDY_BIND_ADDRESS:BAMBUDDY_HTTPS_PORT` (port 18443 by default), preserving
the previous browser URL when updating from bridge mode. Keep optional Virtual
Printer listeners disabled unless their host ports have been reviewed. See
[LAN printer setup](anycubic.md). If DSM's firewall restricts the connection,
allow clients to reach the configured HTTPS endpoint and Bambuddy to reach the
printer's LAN endpoints. Bridge-mode deployments can still scan an explicitly
selected subnet, but their network namespace cannot expose the host's LAN mask.

For a supplied certificate, add a read-only certificate mount and the Uvicorn
certificate settings described in [HTTPS setup](docker-https.md). The image
validates those files at startup and uses them in preference to generated files.

## Source updates

```sh
cd /volume1/docker/bambuddy-anycubic
git pull --ff-only
sudo sh deploy/build-synology.sh
sudo docker compose -f docker-compose.synology.yml up -d --no-deps --pull never --wait --wait-timeout 120 bambuddy
sudo docker compose -f docker-compose.synology.yml ps
```

The classic builder works on DSM without a Buildx package. Updates preserve the
Docker-managed data/log volumes and generated certificate. Do not use
`down --volumes` when updating. The host-network profile retains the configured
NAS address and port 18443 while exposing real interface masks to discovery.

Building first leaves the current container running if a dependency download
or build fails. Recreating the service reuses the managed data/log volumes.
Keep the same Compose project name and volumes to retain the installation.
