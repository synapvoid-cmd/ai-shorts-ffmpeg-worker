#!/usr/bin/env python3
"""
Self-Hosted FFmpeg + yt-dlp HTTP Worker for n8n Cloud
=====================================================
Processes video/audio via HTTP using open-source FFmpeg (CPU libx264).
Downloads YouTube videos via yt-dlp. No NVIDIA GPU required.

Endpoints:
  POST /download              — Download YouTube video via yt-dlp (synchronous, returns binary)
  POST /ffmpeg/job            — Submit FFmpeg job (multipart form: file, full_command, output_extension)
  GET  /ffmpeg/job/<id>       — Get job status
  GET  /ffmpeg/job/<id>/download — Download processed file
  GET  /health               — Health check (always open)

Auth: Set API_KEY env var to require X-API-Key header on protected endpoints.

Deployment:
  Docker:  docker build -t ffmpeg-worker . && docker run -p 7860:7860 ffmpeg-worker
  Python:  pip install -r requirements.txt && python app.py
"""

import logging
import mimetypes
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from functools import wraps
from flask import Flask, jsonify, request, send_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app = Flask(__name__)

# ═══ Configuration (standardized env vars) ═══
PORT = int(os.getenv("PORT", "7860"))
API_KEY = os.getenv("API_KEY", "").strip()
STORAGE_DIR = os.getenv("STORAGE_DIR", "/tmp/ffmpeg-worker")
FILE_RETENTION_HOURS = float(os.getenv("FILE_RETENTION_HOURS", "24"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
MAX_JOB_TIMEOUT_SECONDS = int(os.getenv("MAX_JOB_TIMEOUT_SECONDS", "1800"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "500"))
MAX_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("MAX_DOWNLOAD_TIMEOUT_SECONDS", "300"))

os.makedirs(STORAGE_DIR, exist_ok=True)

# ═══ In-memory job store (thread-safe) ═══
# NOTE: Must use 1 gunicorn worker — in-memory dict is NOT shared across processes.
jobs = {}
jobs_lock = threading.Lock()

# ═══ MIME types ═══
CUSTOM_MIME = {
    "mp3": "audio/mpeg", "mp4": "video/mp4", "wav": "audio/wav",
    "webm": "video/webm", "mov": "video/quicktime", "avi": "video/x-msvideo",
}

# ═══ Command safety validation ═══
BLOCKED_PATTERNS = [
    "h264_nvenc", "hevc_nvenc", "h264_amf",  # GPU-only encoders
    "exec",   # command injection
    "; rm",   # shell chaining
    "&& rm",
    "| sh", "| bash",
    "> /etc", "> /dev",
    "curl ", "wget ",
    "nc ", "ncat ",
]

def validate_command(command: str) -> bool:
    """Block dangerous FFmpeg commands."""
    cmd_lower = command.lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return False
    return True

# ═══ Auth decorator ═══
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if API_KEY:
            provided = request.headers.get("X-API-Key", "")
            if not provided or provided != API_KEY:
                return jsonify({"error": "Unauthorized: invalid or missing X-API-Key"}), 401
        return f(*args, **kwargs)
    return decorated

# ═══ FFmpeg job processor ═══
def run_ffmpeg_job(job_id, cmd_args, input_path, output_path):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "processing"

    logging.info(f"Job {job_id}: running FFmpeg")
    try:
        result = subprocess.run(
            cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=MAX_JOB_TIMEOUT_SECONDS
        )
        with jobs_lock:
            if job_id in jobs:
                if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    jobs[job_id]["status"] = "finished"
                    logging.info(f"Job {job_id}: completed")
                else:
                    jobs[job_id]["status"] = "failed"
                    jobs[job_id]["error"] = (result.stderr or "")[-500:] or f"FFmpeg exit code {result.returncode}"
                    logging.error(f"Job {job_id}: failed")
    except subprocess.TimeoutExpired:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"FFmpeg timed out after {MAX_JOB_TIMEOUT_SECONDS}s"
    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
    finally:
        if os.path.exists(input_path):
            try: os.remove(input_path)
            except: pass

# ═══ Periodic cleanup ═══
def periodic_cleanup():
    while True:
        try:
            now = time.time()
            cutoff = now - (FILE_RETENTION_HOURS * 3600)
            if os.path.exists(STORAGE_DIR):
                for fname in os.listdir(STORAGE_DIR):
                    fpath = os.path.join(STORAGE_DIR, fname)
                    try:
                        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                            os.remove(fpath)
                            logging.info(f"Cleaned: {fpath}")
                    except: pass
            with jobs_lock:
                expired = [jid for jid, j in jobs.items() if j.get("created_at", now) < cutoff]
                for jid in expired:
                    del jobs[jid]
        except Exception as e:
            logging.error(f"Cleanup error: {e}")
        time.sleep(CLEANUP_INTERVAL_SECONDS)

cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health_check():
    """Health check — always public."""
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ytdlp_ok = shutil.which("yt-dlp") is not None
    return jsonify({
        "status": "ok" if ffmpeg_ok else "degraded",
        "ffmpeg": "available" if ffmpeg_ok else "missing",
        "yt-dlp": "available" if ytdlp_ok else "missing",
        "auth": "enabled" if API_KEY else "disabled",
        "version": "2.0"
    }), 200

@app.route("/download", methods=["POST"])
@require_api_key
def download_video():
    """Download a YouTube video using yt-dlp (synchronous, returns binary).
    
    Request: JSON {"url": "https://youtube.com/watch?v=..."}
    Success: binary video/mp4 file
    Failure: JSON {"error": "message"} with appropriate HTTP status
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    
    if not url:
        return jsonify({"error": "Missing 'url' in JSON body"}), 400
    
    job_id = str(uuid.uuid4())
    output_path = os.path.join(STORAGE_DIR, f"{job_id}_download.mp4")
    
    logging.info(f"Download {job_id}: {url}")
    
    try:
        cmd = [
            "yt-dlp",
            "-o", output_path,
            "-f", "best[ext=mp4][height<=1080]/best[height<=1080]/best",
            "--no-playlist",
            "--no-warnings",
            url
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=MAX_DOWNLOAD_TIMEOUT_SECONDS
        )
        
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error")[-500:]
            logging.error(f"Download {job_id}: yt-dlp failed: {err}")
            return jsonify({"error": f"yt-dlp failed: {err}"}), 500
        
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Downloaded file is empty or missing"}), 500
        
        file_size = os.path.getsize(output_path)
        if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            os.remove(output_path)
            return jsonify({"error": f"File too large ({file_size // (1024*1024)}MB, max {MAX_FILE_SIZE_MB}MB)"}), 413
        
        logging.info(f"Download {job_id}: success ({file_size} bytes)")
        
        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"video_{job_id}.mp4"
        )
    
    except subprocess.TimeoutExpired:
        logging.error(f"Download {job_id}: timed out")
        return jsonify({"error": f"yt-dlp timed out after {MAX_DOWNLOAD_TIMEOUT_SECONDS}s"}), 504
    except Exception as e:
        logging.error(f"Download {job_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/ffmpeg/job", methods=["POST"])
@require_api_key
def create_job():
    """Submit FFmpeg processing job.
    
    Multipart form data:
      file: binary video/audio file
      full_command: FFmpeg command with {input} and {output} placeholders
      output_extension: e.g. 'mp3', 'mp4'
    
    Returns: JSON {"job_id": "uuid", "status": "queued"}
    """
    if "file" not in request.files:
        return jsonify({"error": "Missing 'file' in multipart/form-data"}), 400
    
    uploaded = request.files["file"]
    if not uploaded or uploaded.filename == "":
        return jsonify({"error": "No file selected"}), 400
    
    full_command = request.form.get("full_command", "").strip()
    if "{input}" not in full_command or "{output}" not in full_command:
        return jsonify({"error": "full_command must contain {input} and {output}"}), 400
    
    if not validate_command(full_command):
        return jsonify({"error": "Command contains blocked patterns (GPU codecs, shell injection)"}), 400
    
    output_ext = request.form.get("output_extension", "mp4").strip().lstrip(".") or "mp4"
    job_id = str(uuid.uuid4())
    
    # Save uploaded file
    input_ext = os.path.splitext(uploaded.filename or "")[1] or ".tmp"
    input_path = os.path.join(STORAGE_DIR, f"{job_id}_input{input_ext}")
    output_path = os.path.join(STORAGE_DIR, f"{job_id}_output.{output_ext}")
    
    uploaded.save(input_path)
    
    # Check file size
    file_size = os.path.getsize(input_path)
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        os.remove(input_path)
        return jsonify({"error": f"File too large ({file_size // (1024*1024)}MB, max {MAX_FILE_SIZE_MB}MB)"}), 413
    
    # Build FFmpeg command
    command_str = full_command.replace("{input}", input_path).replace("{output}", output_path)
    cmd_args = shlex.split(command_str)
    if cmd_args and cmd_args[0] != "ffmpeg":
        cmd_args.insert(0, "ffmpeg")
    
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id, "status": "queued", "created_at": time.time(),
            "input_path": input_path, "output_path": output_path,
            "output_extension": output_ext, "error": None
        }
    
    thread = threading.Thread(target=run_ffmpeg_job, args=(job_id, cmd_args, input_path, output_path), daemon=True)
    thread.start()
    
    return jsonify({"job_id": job_id, "status": "queued"}), 200

@app.route("/ffmpeg/job/<job_id>", methods=["GET"])
@require_api_key
def get_job_status(job_id):
    """Get job status."""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({"error": "Job not found", "job_id": job_id, "status": "unknown"}), 404
    
    resp = {"job_id": job["job_id"], "status": job["status"]}
    if job["status"] == "failed" and job.get("error"):
        resp["error"] = job["error"]
    return jsonify(resp), 200

@app.route("/ffmpeg/job/<job_id>/download", methods=["GET"])
@require_api_key
def download_job_output(job_id):
    """Download processed output file."""
    with jobs_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "finished":
        return jsonify({"error": f"Job not finished (status: {job['status']})"}), 400
    
    output_path = job.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file missing"}), 404
    
    ext = job.get("output_extension", "mp4").lower()
    mimetype = CUSTOM_MIME.get(ext, "application/octet-stream")
    
    return send_file(output_path, mimetype=mimetype, as_attachment=True, download_name=f"output_{job_id}.{ext}")

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large"}), 413

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logging.info(f"FFmpeg Worker v2.0 starting on port {PORT}")
    logging.info(f"Storage: {STORAGE_DIR}")
    logging.info(f"Auth: {'enabled' if API_KEY else 'disabled'}")
    logging.info(f"Max file: {MAX_FILE_SIZE_MB}MB | Max job timeout: {MAX_JOB_TIMEOUT_SECONDS}s")
    
    for tool in ["ffmpeg", "yt-dlp"]:
        if shutil.which(tool):
            logging.info(f"{tool}: available")
        else:
            logging.warning(f"{tool}: NOT FOUND — install with: apt-get install {tool}")
    
    app.run(host="0.0.0.0", port=PORT, threaded=True)
