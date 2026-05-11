# Videos Downloader

![Preview 1](image/1.png)
![Preview 2](image/2.png)
![Preview 3](image/3.png)

A Flask-based social media downloader with single and bulk workflows, live progress tracking, quality selection, and platform-aware extraction.

## Table of Contents

1. [Features](#features)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [Prerequisites](#prerequisites)
5. [Local Setup](#local-setup)
6. [Run the Application](#run-the-application)
7. [Environment Configuration](#environment-configuration)
8. [Deploy to Render (Free Tier)](#deploy-to-render-free-tier)
9. [Docker Deployment](#docker-deployment)
10. [Troubleshooting](#troubleshooting)
11. [Important Storage Note](#important-storage-note)
12. [Git Workflow](#git-workflow)
13. [Disclaimer](#disclaimer)

## Features

- Single and bulk URL download support
- Download progress with size, speed, and ETA
- Video quality selection (platform-dependent)
- Thumbnail/title preview pipeline
- Local download management endpoints

## Tech Stack

- Python (Flask)
- `yt-dlp`
- `instaloader`
- `gunicorn` for production server
- Optional: `ffmpeg` for media processing compatibility

## Project Structure

```text
Videos-Downloader/
|- app.py
|- requirements.txt
|- Procfile
|- Dockerfile
|- templates/
|- static/
|- scripts/
|- downloads/           # local runtime output (ignored in git)
|- .gitignore
```

## Prerequisites

1. Python 3.10 or newer (3.11 recommended)
2. Git
3. Stable internet connection
4. Optional but recommended: FFmpeg

## Local Setup

### 1) Clone the repository

```powershell
git clone https://github.com/saifullahtanim/Videos-Downloader.git
cd Videos-Downloader
```

### 2) Create and activate virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

## Run the Application

```powershell
python app.py
```

Open in browser:

```text
http://127.0.0.1:5000
```

## Environment Configuration

The application reads the following variables:

- `PORT` (default: `5000`)
- `DEBUG` (default: `False`)
- `DOWNLOAD_DIR` (for hosted environments, recommended: `/tmp/downloads`)

Example:

```powershell
$env:DEBUG="False"
$env:DOWNLOAD_DIR="/tmp/downloads"
python app.py
```

## Deploy to Render (Free Tier)

1. Sign in to Render.
2. Create a new **Web Service**.
3. Connect GitHub and select this repository.
4. Use branch `main`.
5. Set **Start Command**:

```text
gunicorn --bind 0.0.0.0:$PORT app:app
```

6. Add environment variables:

```text
DOWNLOAD_DIR=/tmp/downloads
DEBUG=False
```

7. Deploy and wait for build completion.
8. Open the generated Render URL and test download flow.

## Docker Deployment

Build and run locally:

```powershell
docker build -t videos-downloader .
docker run -e PORT=8080 -p 8080:8080 videos-downloader
```

## Troubleshooting

### App does not start

```powershell
.\.venv\Scripts\python.exe app.py
```

### Missing package error

```powershell
pip install -r requirements.txt
```

### Push rejected (`fetch first`)

```powershell
git pull origin main --allow-unrelated-histories
git push origin main
```

## Important Storage Note

On local machine, files are saved in `downloads/`.
On most free hosts (including Render free tier), storage is ephemeral.
Use one of the following production-safe approaches:

1. Stream files directly to the user, then delete temporary files.
2. Upload outputs to object storage (S3/Backblaze) and return signed links.

## Git Workflow

```powershell
git add .
git commit -m "Update project"
git push origin main
```

## Disclaimer

You are responsible for legal and policy-compliant usage.
Always respect platform terms of service and copyright laws.
