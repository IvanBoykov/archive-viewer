# Archive Viewer

Read-only web viewer for a motion-detection video archive stored in an
S3-compatible bucket (tested against Garage). Browsing is fully server-side
rendered; JavaScript is used only for the accessible modal video player on the
hourly list page.

## Archive layout

```
<common_prefix>/<camera>/<YYYY>/<MM>/<DD>/<HH>/<MM>-<SS>[-truncated].mp4
```

Hours contain 0 to 60 clips and whole hours, days or months can be empty. Every
level is listed with a single `ListObjectsV2` call using `Prefix` plus
`Delimiter='/'`; the bucket is never scanned in full.

Setting `ARCHIVE_COMMON_PREFIX` to an empty value drops the leading folder, so
cameras are read directly from the bucket root and pages are served at
`/<camera>/<YYYY>/...`. Folders that do not fit the layout at a given level - a
`latest` pointer, a month named `13`, a four-digit camera name that would
collide with a year - are listed as plain text rather than linked, so a stray
object cannot break a page or hide real data.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp env.example .env       # then edit it
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`/healthz` reports the configured bucket and prefix.

## Navigation

Every listing level is server-rendered; JavaScript is used only by the player on
the hour page.

| Page | Links |
| --- | --- |
| Cameras | None: the listing already shows every entry. |
| Camera | Recorded months, grouped under a heading per year. |
| Years | None: the listing already shows every entry. |
| Months, days | Previous and next period, at the top and the bottom of the listing. |
| Hours | Previous and next hour, consumed by both the page and the player. |

The camera page lists every month of every year, so a heading per year keeps the
months readable; the heading itself links to the year page, which remains a
listing in its own right. Building it uses one extra list request per year, never a scan of the bucket.

Month and day links jump to the nearest period that actually holds clips, so an
empty period is never one click away. A month page links to a month page and a
day page to a day page, because the target is trimmed to the level of the page
you are on.

## Configuration

All settings come from the environment, loaded from `.env` when present. See
`env.example` for the full list. The important one is `ARCHIVE_HOST_MAP`:

| Incoming `Host` | Behavior |
| --- | --- |
| Present in `ARCHIVE_HOST_MAP` | The page links each clip to a presigned URL (1-hour expiry) pointing at the mapped endpoint, using the mapped path or virtual-hosted addressing style. The browser talks to S3 directly. |
| Absent from the map | The page links each clip to `/media/<key>`, which the backend proxies from S3. Credentials, the S3 `Host` header and SigV4 signing stay server side. |

The proxy forwards `Range` requests, returns `206` with `Content-Range`, serves
the full object when `If-Range` is stale, answers `416` for unsatisfiable
ranges, ignores malformed `Range` headers per RFC 9110, and refuses any key
that is not an `.mp4` under the configured common prefix.

Presigning signs locally and performs no network request, so a mapped endpoint
only has to be reachable from the browser, never from the application server.
The server needs to reach `ARCHIVE_S3_ENDPOINT_URL` alone, because listing and
proxying go through it. Mapped endpoints are expected to be client-side
addresses, such as an internal LAN host that the server itself cannot route to.
The static credentials in `ARCHIVE_S3_ACCESS_KEY_ID` and
`ARCHIVE_S3_SECRET_ACCESS_KEY` produce those signatures, so they must be
valid for the mapped endpoint from the client's location, and the endpoint's
hostname must resolve for the client.

Garage needs `signature_version=s3v4` for presigning, so that is always used.

## Container

`Dockerfile` builds a multi-stage image that runs as a non-root user on port
8000, and `compose.yaml` runs it with `env_file: .env`, which is required.

```bash
docker build -t archive-viewer .
docker compose up --build
```

The compose file publishes port 8000 on every host interface and the
application has no authentication of its own, so bind it to loopback and put an
authenticating reverse proxy in front when the host is reachable from outside.

`.github/workflows/docker-publish.yml` builds the image for `linux/amd64`,
`linux/arm64` and `linux/arm/v7` on push to the default branch or a `v*` tag,
and pushes it to GHCR as `ghcr.io/<owner>/<repo>`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs against `moto`'s fake S3 and covers the level parsing and label
rules, cross-boundary previous/next hour search across empty hours, days,
months and years, the month and day listings that link to the previous and next
period of their own level, the camera page that groups months under year
headings, the browse routes and their 404 handling, Range-proxy
semantics, and host-driven proxy-versus-presign selection.

## Dependencies

`requirements.in` and `requirements-dev.in` hold the direct requirements; the
pinned `requirements.txt` and `requirements-dev.txt` are generated with:

```bash
pip-compile requirements.in
pip-compile requirements-dev.in
```

## License

Distributed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
