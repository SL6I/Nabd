FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# One image, many services: SERVICE picks the module to serve.
CMD ["sh", "-c", "if [ \"$SERVICE\" = traffic ]; then exec python traffic.py; else exec uvicorn $(echo $SERVICE | tr - _):app --host 0.0.0.0 --port 8000 --no-access-log --log-level warning; fi"]
