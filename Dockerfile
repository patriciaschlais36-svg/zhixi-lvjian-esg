FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    ESG_PLATFORM_HOST=0.0.0.0 \
    ESG_PLATFORM_PORT=8000 \
    ESG_PLATFORM_RUNTIME_DIR=/data \
    ESG_PLATFORM_JOB_TIMEOUT_SECONDS=7200 \
    ESG_PLATFORM_MAX_UPLOAD_BYTES=31457280

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY 算法源码/依赖环境清单.txt /tmp/依赖环境清单.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /tmp/依赖环境清单.txt

COPY 算法源码/ /app/算法源码/
COPY 平台服务/ /app/平台服务/
COPY 前端界面/ /app/前端界面/
COPY 正式数据产物/平台公开演示数据库.sqlite /app/正式数据产物/平台公开演示数据库.sqlite

RUN mkdir -p /data \
    && python /app/算法源码/检查运行环境.py

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).read()" || exit 1

CMD ["python", "/app/平台服务/平台接口.py"]

