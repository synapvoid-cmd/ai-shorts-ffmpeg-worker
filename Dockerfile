# FFmpeg + yt-dlp HTTP Worker — Dockerfile
# Open-source, CPU-only encoding (libx264). No NVIDIA GPU required.
# IMPORTANT: Use exactly 1 gunicorn worker — in-memory job dict is NOT shared across processes.

FROM python:3.11-slim

# Install FFmpeg (CPU build) and curl for healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      curl \
    && rm -rf /var/lib/apt/lists/*

# Verify FFmpeg installed
RUN ffmpeg -version | head -1

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Create storage directory
RUN mkdir -p /tmp/ffmpeg-worker

# ═══ Environment variables (must match app.py exactly) ═══
ENV PORT=7860
ENV API_KEY=""
ENV STORAGE_DIR=/tmp/ffmpeg-worker
ENV FILE_RETENTION_HOURS=24
ENV CLEANUP_INTERVAL_SECONDS=3600
ENV MAX_JOB_TIMEOUT_SECONDS=1800
ENV MAX_FILE_SIZE_MB=500
ENV MAX_DOWNLOAD_TIMEOUT_SECONDS=300

# Expose port (default 7860; cloud platforms override via PORT env)
EXPOSE 7860

# Health check (curl is installed in the image)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:${PORT:-7860}/health || exit 1

# Run with EXACTLY 1 worker (in-memory job dict requires single process)
# Shell form so ${PORT} is expanded at runtime — cloud platforms inject their own PORT
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 4 --timeout 600 app:app"]
