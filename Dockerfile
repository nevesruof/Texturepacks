FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY texturebot ./texturebot
COPY config.toml curated.json ./
RUN mkdir -p /app/data /app/logs
CMD ["python", "-m", "texturebot", "run"]
