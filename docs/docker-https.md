# Native HTTPS in Docker

Bambuddy's Docker image serves its web UI, API and WebSockets over HTTPS on
port **8000** by default. First startup generates a persistent self-signed
certificate; no certificate setup is required to start the service.

For a fresh NAS installation, use the [Synology deployment guide](synology.md).

## Default generated certificate

For an existing `bambuddy-temp` test deployment, `docker-compose.testing.yml`
builds this checkout and reuses its existing data/log volumes and network.
Set `BAMBUDDY_BIND_ADDRESS` in the checkout's `.env` to the Docker host's LAN IP;
the test HTTPS port defaults to 18443. Stop any proxy already using that port
before the first switch. Subsequent source updates use:

```sh
git pull --ff-only
docker compose -f docker-compose.testing.yml up -d --build --no-deps --wait bambuddy
```

The test Compose file deliberately requires existing volumes/network. It does
not create an empty replacement database. Optional `BAMBUDDY_DATA_VOLUME`,
`BAMBUDDY_LOGS_VOLUME` and `BAMBUDDY_NETWORK` variables select other existing
resource names.

```sh
docker compose up -d --build bambuddy
```

Open `https://your-bambuddy-host:8000`. A browser trust warning is expected until
you trust the generated certificate or supply a certificate from a trusted CA.

The private key and certificate persist together in
`DATA_DIR/tls/server.pem` (normally `/app/data/tls/server.pem`) in the existing
data volume. The directory is private to the application user and the combined
PEM has mode `0600`. Restart or container recreation keeps the same certificate
when the data volume and configured names are retained.

The generated certificate lasts one year. At startup, it is renewed within
30 days of expiry or when its configured names change, keeping the same private
key. Renewal can require trusting the renewed certificate again. Restart a
continuously running service before expiry so it can renew the certificate.

Defaults cover `localhost`, `bambuddy`, `127.0.0.1` and `::1`. Optionally add your
LAN hostname/IP in Compose, then recreate the service:

```yaml
environment:
  BAMBUDDY_HTTPS_HOSTNAMES: bambuddy.home.arpa,192.168.1.20
```

Including a name does not establish browser trust. Export only the public
certificate for your device/browser's trust store:

```sh
docker cp bambuddy:/app/data/tls/cert.pem ./bambuddy-cert.pem
```

Keep `server.pem` private; it contains the private key. Removing the data volume
loses the certificate and Bambuddy's other stored data.

## Supply your own certificate

Provide a PEM certificate (or full chain, leaf certificate first) and its PEM
private key. The certificate must cover the hostname or IP address used in the
browser. Mount these files into the container at runtime:

```yaml
services:
  bambuddy:
    volumes:
      - ./certs:/app/certs:ro
    environment:
      UVICORN_SSL_CERTFILE: /app/certs/fullchain.pem
      UVICORN_SSL_KEYFILE: /app/certs/privkey.pem
```

Merge these entries into the existing service's volumes and environment. If
using the supplied Compose file's list-style environment, uncomment its
matching `UVICORN_SSL_*` lines and certificate mount instead.

Leave `BAMBUDDY_HTTPS=true`, then recreate the service with
`docker compose up -d --force-recreate bambuddy`. Supplied certificate settings
take precedence over generated files. Startup checks validity and the matching
private key. A missing, invalid or unreadable supplied certificate stops startup;
it does not silently change certificates or fall back to HTTP. The generated
certificate is retained and reused if the supplied settings are later removed.

The private key must be readable by the configured `PUID:PGID` (1000:1000 by
default). Keep its permissions restricted; Bambuddy does not change ownership
or permissions on the read-only certificate mount. Do not commit certificate
keys or include them in the image. The conventional `certs/` directory is
excluded from the Docker build context.

For an encrypted private key, Uvicorn also accepts
`UVICORN_SSL_KEYFILE_PASSWORD`. Supply it through your deployment's secret
configuration. Bambuddy does not generate, modify or renew supplied certificates.

Uvicorn also accepts a combined certificate/private-key PEM: set
`UVICORN_SSL_CERTFILE` to that file and omit `UVICORN_SSL_KEYFILE`.

## Build and connect

Build this checkout and recreate the service so its updated health check is
included:

```sh
docker compose up -d --build bambuddy
```

Open `https://your-bambuddy-host:8000`. HTTPS replaces HTTP on the configured
`PORT`; the same listener does not also serve HTTP or redirect it. Change
`PORT` if you want another port. With host networking, that is the host port.
With bridge networking, publish the matching container port, for example
`8443:8000` to expose HTTPS at `https://your-bambuddy-host:8443`.

Browsers must trust the certificate's issuer. A private CA or self-signed
certificate requires trust configuration on each browser/device. Uvicorn's
native settings are documented in its
[HTTPS settings](https://uvicorn.dev/settings/#https).

The frontend automatically uses secure WebSockets (`wss://`) when opened over
HTTPS. Existing absolute HTTP URLs, such as bookmarks, integrations and OIDC
redirect registrations, need the new HTTPS address.

After replacing renewed certificate files, restart the service so Uvicorn
loads them:

```sh
docker compose restart bambuddy
```

Mount the complete certificate directory tree if your certificate manager uses
symlinks. Renewal of supplied certificates remains externally managed.

## HTTP behind an existing reverse proxy

Unset `UVICORN_SSL_*` certificate settings, set `BAMBUDDY_HTTPS=false`, then
recreate the service to keep an HTTP backend. Your reverse proxy can terminate
HTTPS and connect to `http://bambuddy:8000`. Preserve WebSocket forwarding and
`X-Forwarded-Proto`.

## Health checks

Startup and the passive health check resolve the same generated/supplied
certificate. The local HTTPS probe checks the server's exact certificate and
its validity dates before requesting `/health`. It can therefore
check a certificate for your public hostname without requiring that it also
contain `localhost`. This probe uses the public certificate's identity and
requires no private-key password. It does not weaken browser or outbound HTTPS
verification, and it bypasses HTTP proxy environment variables.

The health check never generates or repairs certificates and follows the
explicit HTTP setting.

A missing certificate, expired certificate, mismatched server certificate or
failed `/health` response makes the container unhealthy. During certificate
renewal, health may be unhealthy until the service is restarted with the new
certificate. Mutual TLS client authentication is not configured by this setup.
