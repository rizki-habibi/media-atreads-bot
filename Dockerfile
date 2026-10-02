FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libzbar0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/media

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "app.py"]
