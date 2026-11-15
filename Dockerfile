FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8789
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && groupadd -g 10001 likebridge && useradd -u 10001 -g 10001 -M likebridge && mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 likebridge ./likebridge
USER 10001:10001
VOLUME /data
EXPOSE 8789
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8789/healthz',timeout=2)"
CMD ["python", "-m", "likebridge"]
