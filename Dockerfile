FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN uv pip install --system .

CMD ["uvicorn", "knightbus.main:app", "--host", "0.0.0.0", "--port", "56709"]
