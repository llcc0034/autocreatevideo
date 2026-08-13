FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 autoprompt \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --no-log-init --shell /usr/sbin/nologin autoprompt

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=autoprompt:autoprompt backend /app/backend
COPY --chown=autoprompt:autoprompt scripts/qwen_translate_once.py /app/scripts/qwen_translate_once.py
COPY --chown=autoprompt:autoprompt assets /app/assets
COPY --chown=autoprompt:autoprompt licenses /app/licenses
COPY --chown=autoprompt:autoprompt NOTICE /app/NOTICE
COPY docker/backend-entrypoint.sh /usr/local/bin/autoprompt-entrypoint
RUN chmod 0555 /usr/local/bin/autoprompt-entrypoint \
    && mkdir -p /app/backend/media /app/backend/staticfiles \
    && chown -R autoprompt:autoprompt /app/backend/media /app/backend/staticfiles

USER 10001:10001

ENTRYPOINT ["autoprompt-entrypoint"]
# Requests only validate and enqueue durable generation/assembly jobs; the two
# dedicated workers own long GPU and FFmpeg execution. Keep a bounded API timeout
# so a wedged request cannot occupy a Gunicorn worker for hours.
# Nginx owns the HTTP access log and suppresses the bearer-token report route.
# Leaving Gunicorn's access log disabled prevents that sensitive path from
# being logged a second time behind the proxy.
CMD ["gunicorn", "autoprompt_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--graceful-timeout", "30", "--keep-alive", "5", "--max-requests", "1000", "--max-requests-jitter", "100", "--error-logfile", "-"]
