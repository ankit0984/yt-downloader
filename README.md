# YT Downloader — Backend

Paste a YouTube URL → list all formats → start a download → poll live progress →
the client streams the finished file to itself (a Flutter app picks the save
location). Runs locally with Docker and in production on Cloudflare Containers
behind a Worker front door.

## Stack

- **FastAPI** — HTTP API + in-memory task registry (no Redis)
- **ThreadPoolExecutor** — background downloads (no Celery; one container process)
- **yt-dlp + ffmpeg** — extraction and video+audio merge (up to 4K)
- **Cloudflare Workers + Containers** — production deployment

```text
Flutter ──HTTPS──> Worker (front door) ──> Container
                                            ├─ FastAPI (API + task registry)
                                            ├─ yt-dlp + ffmpeg (download/merge)
                                            └─ temp dir: file is deleted after the client fetches it
```

## Endpoints

`/ping` and `/health` are unauthenticated probes (Cloudflare's readiness check
covers `/ping`); every other endpoint requires the `X-API-Key` header.

| Method | Path                       | Purpose                                                 |
| ------ | -------------------------- | ------------------------------------------------------- |
| GET    | `/ping`                    | Container readiness probe (Cloudflare health check)     |
| GET    | `/health`                  | Liveness                                                |
| GET    | `/formats?url=`            | All available formats for a URL                         |
| POST   | `/download`                | Start a download → `{ task_id }`                        |
| GET    | `/status/{task_id}`        | Progress snapshot (poll every 1–2 s)                    |
| GET    | `/download/{task_id}/file` | Fetch the finished file (deleted after the first fetch) |

Statuses: `queued → started → downloading → processing → done | error` (plus `retrying`).

## Run locally

Requires Docker and `ffmpeg` only if you run the API outside Docker.

```bash
cp .env.example .env            # then edit API_KEYS
docker compose up --build       # http://localhost:8000
```

Or run on the host (ffmpeg on PATH required, e.g. `brew install ffmpeg`):

```bash
PYTHONPATH=src uv run uvicorn main:app --reload
```

## Deploy to Cloudflare (Containers)

Prerequisites: Workers **Paid** plan, Docker running locally (wrangler builds the
image from `./Dockerfile`), Node.js. The first deploy takes several minutes to
provision the container.

```bash
npm install
npx wrangler login
npx wrangler secret put API_KEYS        # paste e.g. ["your-secret-key"]
npx wrangler deploy                     # builds the image and deploys the Worker
npx wrangler containers list            # check provisioning status
```

Container behavior is configured in `wrangler.jsonc`:

- `max_instances: 1` — required: the task registry is in-process memory, so every
  request for a task must reach the same container.
- `instance_type: "standard-1"` (1/2 vCPU, 4 GiB, 8 GB disk) — bump to
  `standard-2` or higher if merges are CPU-throttled.
- The container sleeps after ~10 minutes without activity; frequent `/status`
  polling keeps it awake while a download runs.

## Flutter integration

```dart
final api = Uri.parse('https://<your-worker>.workers.dev');
const key = '<your API key>';

// 1. List formats
final meta = await http.get(
  api.replace(path: '/formats', queryParameters: {'url': videoUrl}),
  headers: {'X-API-Key': key},
);

// 2. Start the download
final started = await http.post(
  api.replace(path: '/download'),
  headers: {'X-API-Key': key, 'Content-Type': 'application/json'},
  body: jsonEncode({'url': videoUrl, 'format_id': chosenFormatId}),
);
final taskId = jsonDecode(started.body)['task_id'] as String;

// 3. Poll GET /status/$taskId every 1-2 s for { status, percent, speed, eta }

// 4. Stream the file to the user-chosen path
final req = http.Request('GET', api.replace(path: '/download/$taskId/file'));
req.headers['X-API-Key'] = key;
final resp = await http.Client().send(req);
final total = resp.contentLength ?? 0;
var received = 0;
final sink = File(chosenPath).openWrite();
await for (final chunk in resp.stream) {
  sink.add(chunk);
  received += chunk.length;
  // progress = total > 0 ? received / total : null
}
await sink.close();
```

## YouTube access (free, no cookies, no tokens)

YouTube gates its `web` client behind a "confirm you're not a bot" check and a
**PO token**. This project sidesteps both — for free — with the `tv_embedded`
player client, which returns the full format list directly:

```env
YTDLP_PLAYER_CLIENTS=tv_embedded
```

If a particular video fails with it, try fallbacks, e.g.
`YTDLP_PLAYER_CLIENTS=tv_embedded,android,ios`.

**Optional escape hatches** (only if needed — cookies can _reduce_ formats):

- `COOKIES_FROM_BROWSER=chrome` (or `COOKIE_FILE=/path/cookies.txt` on a server)
- `YTDLP_PO_TOKEN=...` — see <https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide>

Keep yt-dlp updated — YouTube changes often and fixes ship in yt-dlp releases.

## Notes and limits

- Only 360p exists as a single combined A/V stream; higher qualities are merged
  server-side by ffmpeg (that is why a container is required).
- Files are deleted after the first successful `/file` fetch — re-fetching returns
  404, start a new task instead. A stale-file sweep runs hourly.
- Merged downloads are not byte-resumable (no HTTP Range on merged output).
- Tuning knobs: `MAX_CONCURRENT_DOWNLOADS`, `MAX_FILE_AGE_HOURS`,
  `TASK_TTL_SECONDS`, `RATE_LIMIT`.
