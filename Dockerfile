FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py import_users.py ./

CMD ["uvicorn", "bridge:app", "--host", "127.0.0.1", "--port", "8080"]
