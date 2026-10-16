FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY chatgpt_channel ./chatgpt_channel
COPY dashboard ./dashboard-source
RUN python -m chatgpt_channel.dashboard --source /app/dashboard-source --output /app/dashboard && \
    rm -rf /app/dashboard-source && pip install --no-cache-dir . && pip check && \
    useradd --uid 10001 --system --home /nonexistent chatgpt-channel && \
    mkdir -p /data/chatgpt && chown 10001:10001 /data/chatgpt
ENV CHATGPT_DASHBOARD_DIR=/app/dashboard \
    CHATGPT_STATE_DB=/data/chatgpt/state.sqlite3 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "chatgpt_channel.server"]
