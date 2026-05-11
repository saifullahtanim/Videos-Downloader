# Deploy Guide (Bangla)

Ei file-ta shudhu deploy-er quick-guide — Vercel/Fly/Render somporke short notes.

Important: Ei project-e `yt-dlp` + optional `ffmpeg` ache. Free serverless (Vercel) e lamba-running downloads ba system packages (ffmpeg) thaka hard.

Option A — Frontend on Vercel, backend elsewhere (recommended if you like Vercel):
- Push repo to GitHub.
- Deploy only frontend (static templates) on Vercel or serve static UI there.
- Host backend (Flask) on Render/Fly/Render Free and set environment variable `API_URL` in Vercel to point to backend.

Option B — Full backend deploy with Docker (recommended if you need ffmpeg):
- Use the included `Dockerfile`.
- Deploy to Fly.io or any Docker-capable host.
  - Fly example:
    1. `flyctl launch --name my-videos-downloader`
    2. `flyctl deploy`

Option C — Render (no Docker) quick deploy (good when ffmpeg not required):
- Add `Procfile` (already included) and ensure `requirements.txt` has `gunicorn`.
- On Render: create new Web Service → connect GitHub → set Start Command `gunicorn --bind 0.0.0.0:$PORT app:app` → Deploy.

Storage notes:
- Free hosts usually have ephemeral disks. Files under `DOWNLOAD_DIR` should be temporary.
- For persistent availability: upload final files to S3/Backblaze and serve signed URLs.

If you want, I can:
- create a small `README.md` with step-by-step GitHub→Render deploy, or
- push a deploy-ready commit (Procfile present, `Dockerfile` present). Tell me which.
