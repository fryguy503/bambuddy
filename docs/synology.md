# Synology: fresh Anycubic testing deployment

Use `docker-compose.synology.yml` for a fresh installation on Synology Container
Manager. It builds the Git checkout, publishes native HTTPS on port 18443 and
keeps data/logs in persistent folders. It requires no Caddy container and has no
dependency on volumes or networks from the old Linux testing host.

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
mkdir data logs
```

The UID/GID values let the application own its persistent folders without
running as root. The entrypoint performs initial volume ownership setup, then
generates its private HTTPS key after dropping privileges.

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
before starting or replacing the service; retain the checkout, `.env`, `data/`
and `logs/` and resume from this section after fixing the reported error.

### Verify the service

Open `https://<NAS-LAN-IP>:18443`. The initial self-signed certificate requires
browser trust. Persistent settings, database, tokens and generated certificate
live in `data/`; logs live in `logs/`. Both folders and `.env` are excluded from
Git and the image's build context.

Once the service is healthy, export only its public certificate for a verified
HTTPS check. The redirect creates a file owned by the SSH user:

```sh
sudo docker compose -f docker-compose.synology.yml exec -T bambuddy \
  cat /app/data/tls/cert.pem > data/bambuddy-public-cert.pem
curl --fail --show-error --cacert ./data/bambuddy-public-cert.pem https://<NAS-LAN-IP>:18443/health
```

Bridge networking publishes only the web endpoint. Add the Kobra S1 manually by
its LAN IP; Anycubic's unicast LAN discovery/pairing does not require host
networking. See [LAN printer setup](anycubic.md). If DSM's firewall restricts the
connection, allow your clients to reach the chosen web port and the container
to reach the printer's LAN endpoints. Virtual Printer listener ports are not
published by this deployment.

For a supplied certificate, add a read-only certificate mount and the Uvicorn
certificate settings described in [HTTPS setup](docker-https.md). The image
validates those files at startup and uses them in preference to generated files.

## Source updates

```sh
cd /volume1/docker/bambuddy-anycubic
git pull --ff-only
sudo env DOCKER_BUILDKIT=1 docker compose -f docker-compose.synology.yml build bambuddy
sudo docker compose -f docker-compose.synology.yml up -d --no-deps --pull never bambuddy
```

Building first leaves the current container running if a dependency download
or build fails. Recreating the service retains the `data/` and `logs/` folders.
Changing those paths or deleting their contents resets the installation.
