# DealSynq web app — Docker image WITH Playwright + Chromium, so the live Registry-of-Deeds
# lookup (which drives a real headless browser to beat Incapsula) works on the cloud, not
# just from a local machine. The baseline (requests+urllib3 only) ran fine on Render's
# native Python runtime; this image adds the browser for the deeds feature.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# App deps + Playwright's Chromium. `--with-deps` apt-installs the system libraries
# Chromium needs (this layer is why we use Docker rather than Render's native runtime,
# which has no apt access). Kept as one layer for a smaller image.
COPY deploy/requirements.txt deploy/requirements.txt
RUN pip install --no-cache-dir -r deploy/requirements.txt playwright \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Render injects $PORT; server.py reads it (defaults to 8770 locally). HOST=0.0.0.0 above.
CMD ["python", "fivetownplaza/webapp/server.py"]
