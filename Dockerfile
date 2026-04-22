# Use Playwright's official Python image - Chrome and all its dependencies
# are pre-installed, which saves us from debugging apt packages.
# Must match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Install Python dependencies (cached in this layer if requirements don't change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY app/ ./app/

# Railway sets PORT via env var. Default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# `sh -c` is needed so $PORT gets expanded at runtime, not at build time
CMD sh -c "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"
