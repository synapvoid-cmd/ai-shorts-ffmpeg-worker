# Self-Hosted FFmpeg + yt-dlp HTTP Worker v2.0

Open-source video processing worker for the **AI Viral Shorts Generator V4** n8n workflow.

Replaces paid video-processing APIs (Upload-Post, Klap, etc.) with a free, self-hosted alternative.

## What This Worker Does

| Endpoint | Purpose | Used By |
|----------|---------|---------|
| `POST /download` | Download YouTube video via yt-dlp | n8n: Download via Worker node |
| `POST /ffmpeg/job` | Process video/audio with FFmpeg | n8n: Extract Audio, Cut & Crop |
| `GET /ffmpeg/job/{id}` | Check job status | n8n: Check Audio/Clip Status |
| `GET /ffmpeg/job/{id}/download` | Download processed file | n8n: Download Audio/Clip |
| `GET /health` | Health check | Monitoring |

**100% free. 100% open-source. CPU encoding (libx264). No NVIDIA GPU required.**

---

## Quick Start

### Docker (Recommended)

```bash
cd ffmpeg-worker
docker build -t ffmpeg-worker .
docker run -d -p 7860:7860 --name ffmpeg-worker ffmpeg-worker
# Your worker URL: http://YOUR_IP:7860

# With API key auth:
docker run -d -p 7860:7860 -e API_KEY="your-secret" --name ffmpeg-worker ffmpeg-worker
```

### Python (VPS)

```bash
sudo apt-get install ffmpeg
pip install -r requirements.txt
# Also install yt-dlp: pip install yt-dlp (or apt-get install yt-dlp)
python app.py
```

### Verify

```bash
curl http://localhost:7860/health
# {"status":"ok","ffmpeg":"available","yt-dlp":"available","auth":"disabled","version":"2.0"}
```

---

## Architecture

```
n8n Cloud
  ↓ POST /download (YouTube URL)
  ↓ Returns video binary
  ↓
n8n Cloud
  ↓ POST /ffmpeg/job (video + FFmpeg command)
  ↓ Poll GET /ffmpeg/job/{id}
  ↓ GET /ffmpeg/job/{id}/download (processed file)
  ↓
n8n Cloud → Google Drive → Publishing
```

### Process Model
- **1 Gunicorn worker** (in-memory job dict is NOT shared across processes)
- **4 threads** per worker (handles concurrent HTTP requests)
- **600s gunicorn timeout** (long enough for video processing)
- Jobs stored in thread-safe in-memory dictionary
- Files cleaned up periodically (default: 24h retention)

---

## API Reference

### POST /download
Download a YouTube video using yt-dlp.

```bash
curl -X POST http://localhost:7860/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=..."}'
# Returns: binary video/mp4 file
```

### POST /ffmpeg/job
Submit FFmpeg processing job.

```bash
curl -X POST http://localhost:7860/ffmpeg/job \
  -F "file=@video.mp4" \
  -F "full_command=ffmpeg -y -i {input} -map a:0 -vn -ac 1 -ar 16000 -b:a 32k -c:a libmp3lame {output}" \
  -F "output_extension=mp3"
# Returns: {"job_id": "uuid", "status": "queued"}
```

### GET /ffmpeg/job/{id}
```bash
curl http://localhost:7860/ffmpeg/job/{job_id}
# Returns: {"job_id": "uuid", "status": "finished"}
```

### GET /ffmpeg/job/{id}/download
```bash
curl http://localhost:7860/ffmpeg/job/{job_id}/download -o output.mp3
# Returns: binary file
```

### GET /health
```bash
curl http://localhost:7860/health
# Returns: {"status":"ok","ffmpeg":"available","yt-dlp":"available","version":"2.0"}
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `7860` | HTTP port |
| `API_KEY` | `""` | Auth key (empty = no auth). Requires X-API-Key header on protected endpoints. |
| `STORAGE_DIR` | `/tmp/ffmpeg-worker` | Temp file storage |
| `FILE_RETENTION_HOURS` | `24` | Auto-cleanup age (hours) |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | Cleanup check frequency |
| `MAX_JOB_TIMEOUT_SECONDS` | `1800` | FFmpeg max runtime |
| `MAX_FILE_SIZE_MB` | `500` | Max upload/download size |
| `MAX_DOWNLOAD_TIMEOUT_SECONDS` | `300` | yt-dlp max download time |

---

## Security

### Authentication
- If `API_KEY` is set: all `/ffmpeg/*` and `/download` endpoints require `X-API-Key` header
- `/health` is always public
- If `API_KEY` is empty: no authentication

### Command Safety
The worker blocks dangerous FFmpeg commands:
- GPU-only codecs: `h264_nvenc`, `hevc_nvenc`, `h264_amf`
- Shell injection: `exec`, `;`, `|`, `&&`, `>`
- Dangerous commands: `rm`, `curl`, `wget`, `nc`

### Production Deployment
Use HTTPS (reverse proxy) + set `API_KEY`:
```nginx
server {
    listen 443 ssl;
    server_name worker.example.com;
    client_max_body_size 500M;
    location / {
        proxy_pass http://localhost:7860;
        proxy_read_timeout 600s;
    }
}
```

---

## n8n Configuration

After importing the workflow:
1. Create credential **"FFmpeg Worker API Key"** (HTTP Header Auth)
   - Header Name: `X-API-Key`
   - Value: your API key (or any value if no auth)
2. In the n8n form, enter your worker URL in the **"FFmpeg Worker URL"** field
   - Example: `https://worker.example.com`

---

## FFmpeg Commands Used by n8n

### Audio Extraction
```
ffmpeg -y -i {input} -map a:0 -vn -ac 1 -ar 16000 -b:a 32k -c:a libmp3lame {output}
```

### Clip Cutting (Vertical 9:16, CPU encoding)
```
ffmpeg -y -hide_banner -loglevel error
  -ss {start} -t {duration} -i {input}
  -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:centre,setsar=1"
  -c:v libx264 -crf 23 -preset fast -pix_fmt yuv420p
  -c:a aac -b:a 128k -ar 44100 -ac 2
  -movflags +faststart {output}
```

**Center crop fallback — NOT face-aware.** Smart crop would require OpenCV on the worker.

---

## Troubleshooting

| Issue | Solution |
|-------|---------|
| `ffmpeg: not found` | `apt-get install ffmpeg` |
| `yt-dlp: not found` | `pip install yt-dlp` |
| `File too large` | Increase `MAX_FILE_SIZE_MB` |
| `Job timeout` | Increase `MAX_JOB_TIMEOUT_SECONDS` |
| `Command blocked` | Check for blocked patterns (GPU codecs, injection) |
| n8n can't connect | Check firewall + worker URL |
| Multi-worker issues | Use exactly 1 gunicorn worker (in-memory state) |
| Download timeout | Increase `MAX_DOWNLOAD_TIMEOUT_SECONDS` |
