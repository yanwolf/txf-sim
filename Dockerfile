FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei \
    PORT=8080

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8080
CMD ["python", "-m", "app.main"]
