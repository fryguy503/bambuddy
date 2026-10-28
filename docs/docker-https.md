# Native HTTPS in Docker

Bambuddy can serve its web UI, API and WebSockets over HTTPS directly from
Uvicorn. HTTPS is optional; the default listener uses HTTP on port 8000. An
existing reverse proxy can continue terminating HTTPS as before.

## Configure certificates

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

The private key must be readable by the configured `PUID:PGID` (1000:1000 by
default). Keep its permissions restricted; Bambuddy does not change ownership
or permissions on the read-only certificate mount. Do not commit certificate
keys or include them in the image. The conventional `certs/` directory is
excluded from the Docker build context.

For an encrypted private key, Uvicorn also accepts
`UVICORN_SSL_KEYFILE_PASSWORD`. Supply it through your deployment's secret
configuration. Bambuddy does not generate or renew web certificates.

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

## Health checks

The image probes HTTP by default and HTTPS when `UVICORN_SSL_CERTFILE` is set.
Its local HTTPS probe checks the server's exact certificate against the
mounted public certificate and checks its validity dates. It can therefore
check a certificate for your public hostname without requiring that it also
contain `localhost`. This probe uses the public certificate's identity and
requires no private-key password. It does not weaken browser or outbound HTTPS
verification, and it bypasses HTTP proxy environment variables.

A missing certificate, expired certificate, mismatched server certificate or
failed `/health` response makes the container unhealthy. During certificate
renewal, health may be unhealthy until the service is restarted with the new
certificate. Mutual TLS client authentication is not configured by this setup.
