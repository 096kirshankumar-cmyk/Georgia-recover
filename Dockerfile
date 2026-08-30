FROM python:3.12-slim

# freetype-py needs libfreetype runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 libfreetype-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# The reference Georgia font is downloaded at startup (or place ref_font/Georgia.TTF here).

ENV PORT=8000
EXPOSE 8000

# Use Railway's $PORT if provided, otherwise default to 8000.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
