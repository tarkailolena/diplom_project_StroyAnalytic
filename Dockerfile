FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY migrate.py .
COPY dashboard.py .
COPY data ./data
COPY .env .

CMD ["python", "bot.py"]