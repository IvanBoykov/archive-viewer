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

Leaving `S3_PREFIX` empty drops the leading folder, so
cameras are read directly from the bucket root and pages are served at
`/<camera>/<YYYY>/...`. Folders that do not fit the layout at a given level - a
`latest` pointer, a month named `13`, a four-digit camera name that would
collide with a year - are listed as plain text rather than linked, so a stray
object cannot break a page or hide real data.

Folder names are read exactly as stored and never converted. `S3_KEY_TIMEZONE`
is display only: when it is set to a valid IANA zone, the page footer says which
zone the clip times are in. When it is unset the line is not rendered at all.

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
listing in its own right. Building it costs one extra list request per year,
never a scan of the bucket.

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
ranges, and ignores malformed `Range` headers per RFC 9110. It refuses - with
`404`, so a probe cannot tell "not archived" from "not permitted" - any key that
is not an `.mp4`, contains `..`, or (when `S3_PREFIX` is set) lies outside that
prefix. With an empty `S3_PREFIX` any `.mp4` in the bucket is proxiable, so
bucket policy still has to limit the read scope to the archive.

Presigning signs locally and performs no network request, so a mapped endpoint
only has to be reachable from the browser, never from the application server.
The server needs to reach `AWS_ENDPOINT_URL_S3` alone, because listing and
proxying go through it. Mapped endpoints are expected to be client-side
addresses, such as an internal LAN host that the server itself cannot route to.
The credentials resolved by the default boto3 chain (`AWS_ACCESS_KEY_ID` with
`AWS_SECRET_ACCESS_KEY`, a shared profile, or an instance role) produce those
signatures, so they must be valid for the mapped endpoint from the client's
location, and the endpoint's hostname must resolve for the client. The signing
region comes from `AWS_REGION` (or `AWS_DEFAULT_REGION`) and is required. The
region is baked into a presigned URL, so a wrong one cannot be corrected after
the browser has the link: the browser gets `400 AuthorizationHeaderMalformed`
and the clip simply fails to play. Garage names the region it expected in that
response body, which only helps a human fix the configuration.

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

`.github/workflows/docker-publish.yml` builds the image for `linux/amd64` and
`linux/arm64` on push to `main`, `master` or a `v*` tag, and pushes it to GHCR as
`ghcr.io/<owner>/<repo>`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs against `moto`'s fake S3 and covers the level parsing and label
rules, cross-boundary previous/next hour search across empty hours, days,
months and years, the month and day listings that link to the previous and next
period of their own level, the camera page that groups months under year
headings, the browse routes and their 404 handling, Range-proxy semantics,
host-driven proxy-versus-presign selection, and configuration parsing: required
variables, the region requirement, the prefix spellings and the host map.

## Dependencies

`requirements.in` and `requirements-dev.in` hold the direct requirements; the
pinned `requirements.txt` and `requirements-dev.txt` are generated with:

```bash
pip-compile --generate-hashes
pip-compile --allow-unsafe --generate-hashes requirements-dev.in
```

`--generate-hashes` is what lets both `pip install --require-hashes` and the
default hashed install work. `requirements-dev.in` needs `--allow-unsafe`
because `moto` pulls `setuptools` into the tree, and pip-tools refuses to pin
such a package without that flag. Both files must be compiled with the same
Python version the image and CI use (3.14), which is what the header of each
file records.

## License

Distributed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
