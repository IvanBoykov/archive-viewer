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
cp .env.example .env      # then edit it
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`/healthz` reports the configured bucket and prefix.

## Configuration

All settings come from the environment, loaded from `.env` when present. See
`.env.example` for the full list. The important one is `ARCHIVE_HOST_MAP`:

| Incoming `Host` | Behavior |
| --- | --- |
| Present in `ARCHIVE_HOST_MAP` | The page links each clip to a presigned URL (1-hour expiry) pointing at the mapped endpoint, using the mapped path or virtual-hosted addressing style. The browser talks to S3 directly. |
| Absent from the map | The page links each clip to `/media/<key>`, which the backend proxies from S3. Credentials, the S3 `Host` header and SigV4 signing stay server side. |

The proxy forwards `Range` requests, returns `206` with `Content-Range`, serves
the full object when `If-Range` is stale, answers `416` for unsatisfiable
ranges, ignores malformed `Range` headers per RFC 9110, and refuses any key
that is not an `.mp4` under the configured common prefix.

Garage needs `signature_version=s3v4` for presigning, so that is always used.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs against `moto`'s fake S3 and covers the level parsing and label
rules, cross-boundary previous/next hour search across empty hours, days,
months and years, the browse routes and their 404 handling, Range-proxy
semantics, and host-driven proxy-versus-presign selection.

## Dependencies

`requirements.in` and `requirements-dev.in` hold the direct requirements; the
pinned `requirements.txt` and `requirements-dev.txt` are generated with:

```bash
pip-compile requirements.in
pip-compile requirements-dev.in
```
