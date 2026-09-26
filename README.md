# Silence Remove + Speed Match (web app)

A small Flask app: upload an "upper" and "lower" video, it removes silence
from the lower video, then speeds up the upper video so its duration
matches. Runs ffmpeg on the server.

## Run locally

```bash
pip install -r requirements.txt
# ffmpeg must be installed and on PATH locally too
python app.py
# open http://localhost:8080
```

## Deploy to Railway

### Option A — Railway CLI (fastest, no GitHub needed)

1. Install the CLI and log in:
   ```bash
   npm i -g @railway/cli
   railway login
   ```
2. From inside this folder:
   ```bash
   railway init
   railway up
   ```
3. Once deployed, generate a public URL:
   ```bash
   railway domain
   ```
4. Open the printed URL in your browser.

### Option B — GitHub + Railway dashboard

1. Push this folder to a new GitHub repo.
2. On [railway.com](https://railway.com), New Project → Deploy from GitHub repo → select the repo.
3. Railway will detect the `Dockerfile` (via `railway.toml`) and build automatically.
4. Under Settings → Networking, click "Generate Domain" to get a public URL.

## Important notes on Railway's free tier

- New accounts get a **one-time $5 trial credit** for 30 days. After that,
  the account drops to the Free plan, which only gives **$1/month** of
  usage credit.
- ffmpeg encoding is CPU- and RAM-heavy and Railway bills by the minute
  for compute — a single video job can use a meaningful chunk of that
  credit, especially on longer clips. This is fine for occasional personal
  use/testing, not for regular or heavy use without upgrading to a paid plan.
- `MAX_CONTENT_LENGTH` in `app.py` caps total upload size (default 300 MB).
  Lower it if you're worried about running out of credit from large uploads.
- Uploaded/processed files are stored in `tmp/<job_id>/` and auto-deleted
  after 1 hour (see `cleanup_old_jobs()` in `app.py`). Railway's free/trial
  plan only gives 0.5 GB of storage, so don't let jobs pile up.
- Processing is synchronous (the browser request stays open until ffmpeg
  finishes). Gunicorn's timeout is set to 600s in the `Dockerfile` — raise
  it if you expect longer videos, or convert this to a background job queue
  for anything beyond quick clips.
- **Out-of-memory kills:** on the Free/Trial plan (0.5–1GB RAM), encoding a
  1080p+ video can get the ffmpeg process killed by the OS if it's allowed
  to use too many threads/too much buffering. `video_processing.py` caps
  ffmpeg to 2 threads and uses the `veryfast` preset by default to keep
  memory use down. Tune with environment variables in the Railway
  dashboard (Service → Variables) if needed:
  - `FFMPEG_THREADS` (default `2`) — lower for very tight RAM, raise if
    you upgrade to a plan with more RAM/CPU.
  - `FFMPEG_PRESET` (default `veryfast`) — `medium`/`slow` gives smaller,
    higher-quality output but uses noticeably more RAM.
  If you still see "ffmpeg was killed" errors, the video's resolution or
  length is likely too much for the current plan — try a shorter/smaller
  clip or upgrade.

## Project structure

```
app.py                 Flask routes (upload, process, download)
video_processing.py    ffmpeg/ffprobe logic (silence removal, speed match)
templates/index.html   Upload form
templates/result.html  Download links + stats
Dockerfile             Installs ffmpeg + runs gunicorn
railway.toml           Tells Railway to build from the Dockerfile
requirements.txt       flask, gunicorn, werkzeug
```
