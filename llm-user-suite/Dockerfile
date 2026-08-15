FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/shared
WORKDIR /app
COPY llm-user-suite/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN useradd --create-home --uid 10001 suite
COPY --chown=suite:suite llm-user-suite/llm_user_suite /app/llm_user_suite
COPY --chown=suite:suite backend/llm_eval_core /app/shared/llm_eval_core
RUN mkdir -p /app/data/artifacts && chown -R suite:suite /app/data
USER suite
EXPOSE 8080
CMD ["uvicorn", "llm_user_suite.main:app", "--host", "0.0.0.0", "--port", "8080"]
