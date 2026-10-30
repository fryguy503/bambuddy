# Synology: temporary modularization + HTTPS upgrade

This branch is `codex/printer-modularization-https`. It combines the modular
printer core with native HTTPS and the Synology build helper. Its image tag is
`bambuddy:printer-modularization-https`; it includes the Bambu plugin. Other
printer providers are installed separately as `.bbp` packages.

The upgrade profile reuses **existing** Docker-managed data/log volumes and
keeps Compose project `bambuddy-synology`. Missing volumes are an error, so a
mistyped name does not silently open a new empty installation. Do not run two
application containers against the same database.

## Identify the current installation

SSH into the NAS and enter the existing checkout. Its old directory name can
remain unchanged, for example `/volume1/docker/bambuddy-anycubic`. Use the
Compose command installed on the NAS (`docker compose` below).

Before changing the checkout, record the existing container and its mounts:

```sh
cd /volume1/docker/bambuddy-anycubic
sudo docker compose -f docker-compose.synology.yml ps
cid=$(sudo docker compose -f docker-compose.synology.yml ps -q bambuddy)
test -n "$cid" || exit 1
sudo docker inspect --format '{{range .Mounts}}{{println .Type .Name .Source "->" .Destination}}{{end}}' "$cid"
old_image=$(sudo docker inspect --format '{{.Image}}' "$cid")
data_volume=$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$cid")
logs_volume=$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/logs"}}{{.Name}}{{end}}{{end}}' "$cid")
test -n "$data_volume" && test -n "$logs_volume" || exit 1
sudo docker volume inspect "$data_volume" "$logs_volume" >/dev/null
```

The last commands deliberately stop for bind-mounted storage. If the existing
installation uses bind mounts, preserve those exact mount entries in a local
Compose override; do not substitute newly created named volumes.

## Take a consistent rollback snapshot

The new application runs database migrations. Retain the old image and a
pre-upgrade snapshot of the complete data volume. Reverting the image alone
is not a database rollback. If `DATABASE_URL` selects PostgreSQL, also take a
consistent PostgreSQL backup before continuing.

From the same SSH session:

```sh
umask 077
backup="$(pwd)/backups/before-modularization-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
printf '%s\n' "$old_image" > "$backup/image.before"
printf '%s\n' "$data_volume" > "$backup/data-volume.before"
printf '%s\n' "$logs_volume" > "$backup/logs-volume.before"
cp docker-compose.synology.yml "$backup/compose.before.yml"
test ! -f .env || cp .env "$backup/env.before"
sudo docker compose -f docker-compose.synology.yml stop -t 30 bambuddy
sudo docker run --rm --network none --user 0:0 --entrypoint sh \
  --mount "type=volume,source=$data_volume,target=/snapshot,readonly" \
  --mount "type=bind,source=$backup,target=/backup" \
  "$old_image" -c 'umask 077; tar -czf /backup/data.tar.gz -C /snapshot .'
sudo test -s "$backup/data.tar.gz"
```

This uses the existing image as a backup helper, without starting its app or
pulling a helper image. The snapshot includes SQLite/WAL files, archives,
library content stored in that volume, account keys, plugin state and the
persistent HTTPS certificate. Back up any separately mounted library/archive
folders using their existing backup procedure. Keep the backup private: it
contains credentials and private keys. The logs volume remains intact.

## Select the branch and preserve settings

```sh
git status --short
git fetch origin
git switch --track -c codex/printer-modularization-https \
  origin/codex/printer-modularization-https
```

If that local branch already exists, use `git switch codex/printer-modularization-https`
and `git pull --ff-only`. Resolve local tracked-file edits before switching;
`.env` is ignored and remains in place.

Keep or edit these values in `.env` using the actual recorded volume names,
NAS LAN address and existing numeric UID/GID. Do not copy the example IDs if
the current installation uses different ones.

```dotenv
BAMBUDDY_BIND_ADDRESS=192.168.1.20
BAMBUDDY_HTTPS_PORT=18443
BAMBUDDY_HTTPS_HOSTNAMES=192.168.1.20
BAMBUDDY_DATA_VOLUME=bambuddy-synology_bambuddy_data
BAMBUDDY_LOGS_VOLUME=bambuddy-synology_bambuddy_logs
PUID=1000
PGID=1000
TZ=America/New_York
```

The Synology profile uses host networking for LAN discovery and binds its web
listener to the explicitly supplied address/port. Keep optional virtual-printer
listeners disabled unless their NAS ports have been checked. The default image
tag is new and leaves the old image available for rollback. Remove a stale
`BAMBUDDY_IMAGE` override from `.env`, or use the same override for both the
build helper and Compose commands.

Old provider identities are preserved during migration. A printer whose plugin
is absent remains unavailable; it is not connected through Bambu's protocol.

## Build, then replace the container

Validate the rendered profile first:

```sh
sudo docker compose -f docker-compose.synology.yml config --quiet
```

For a normal BuildKit-capable engine:

```sh
sudo env DOCKER_BUILDKIT=1 docker compose -f docker-compose.synology.yml build bambuddy
```

For the Docker 24/seccomp issue encountered on the NAS, use the existing classic
builder fallback instead:

```sh
sudo sh deploy/build-synology.sh
```

The helper removes only the dependency-cache `RUN --mount` flags from a temporary
Dockerfile, sets `DOCKER_BUILDKIT=0`, and removes the temporary file afterward.
It does not change daemon security settings. This fallback needs an engine that
still provides the classic builder.

Only continue after the build succeeds:

```sh
sudo docker compose -f docker-compose.synology.yml up -d --no-build --no-deps --pull never bambuddy
sudo docker compose -f docker-compose.synology.yml ps
sudo docker compose -f docker-compose.synology.yml logs --tail 100 bambuddy
```

Open `https://<NAS-LAN-IP>:18443`. Existing generated certificates are reused
from `/app/data/tls`; a changed hostname/address can renew the generated
certificate and require browser trust again. This branch does not add an HTTP
redirect. Supplied certificates and reverse-proxy HTTP backends are described
in [native HTTPS configuration](docker-https.md).

The image's readiness probe verifies the listener's certificate. To verify the
NAS endpoint separately, export only its public certificate:

```sh
mkdir -p certs
sudo docker compose -f docker-compose.synology.yml exec -T bambuddy \
  cat /app/data/tls/cert.pem > certs/bambuddy-public-cert.pem
curl --fail --show-error --cacert certs/bambuddy-public-cert.pem \
  https://<NAS-LAN-IP>:18443/health
```

## Roll back without deleting the upgraded data

Stop the new container. Restore the snapshot into a **new** named volume, so
both the upgraded volume and the original backup remain available. Set `backup`
to the directory created above if using a new SSH session.

```sh
sudo docker compose -f docker-compose.synology.yml stop -t 30 bambuddy
old_image=$(cat "$backup/image.before")
rollback_volume="bambuddy-synology-rollback-$(date +%Y%m%d-%H%M%S)"
sudo docker volume create "$rollback_volume"
sudo docker run --rm --network none --user 0:0 --entrypoint sh \
  --mount "type=volume,source=$rollback_volume,target=/restore" \
  --mount "type=bind,source=$backup,target=/backup,readonly" \
  "$old_image" -c 'tar -xzf /backup/data.tar.gz -C /restore'
sudo env BAMBUDDY_IMAGE="$old_image" BAMBUDDY_DATA_VOLUME="$rollback_volume" \
  docker compose -f docker-compose.synology.yml up -d --no-build --no-deps --pull never bambuddy
sudo docker compose -f docker-compose.synology.yml ps
```

Keep the old address, port, UID/GID and other settings while reverting. This
uses the new profile to run the retained old image against the restored old
data. For PostgreSQL, restore its corresponding pre-upgrade database backup as
well. After verifying the reverted app, persist the chosen image/volume names
in the deployment configuration for subsequent restarts. Do not use
`docker compose down --volumes` during an upgrade or rollback.

## A different existing Compose project

`docker-compose.testing.yml` is the separate bridge-network profile for the old
`bambuddy-temp` project. It requires that project's existing volumes and network;
it is not the Synology profile. Use the profile matching the actual installation
and preserve its project name, published address/port and storage.
