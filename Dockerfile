FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

ARG ML_DEPS=full
RUN uv pip install --system . \
    && if [ "$ML_DEPS" = "full" ]; then \
         uv pip install --system "lightgbm>=4.5.0" "numpy>=2.1.0" "pandas>=2.2.0" "scikit-learn>=1.5.0"; \
       elif [ "$ML_DEPS" = "inference" ]; then \
         uv pip install --system "lightgbm>=4.5.0" "numpy>=2.1.0"; \
       fi

CMD ["uvicorn", "knightbus.main:app", "--host", "0.0.0.0", "--port", "56709"]
