FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000
# Cloud RunでHTTP/1だとリクエストボディが32MBに制限され、動画アップロードで
# 413になる。HTTP/2(h2c)で受けるためuvicornではなくhypercornを使う。
CMD ["sh", "-c", "hypercorn server:app --bind 0.0.0.0:${PORT}"]
