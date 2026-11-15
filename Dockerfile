FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m aurelia && mkdir /app/data && chown aurelia:aurelia /app/data
COPY --chown=aurelia:aurelia aurelia_utils ./aurelia_utils
USER aurelia
CMD ["python", "-m", "aurelia_utils.bot"]
