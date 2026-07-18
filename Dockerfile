FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run 會注入 PORT；背景生成任務跑在同容器（<500 使用者足夠，之後再抽 Cloud Tasks）
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 75
