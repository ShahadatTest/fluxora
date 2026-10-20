ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY cashflow_copilot /app/cashflow_copilot
COPY deploy/vps/healthcheck.py /app/healthcheck.py
RUN chmod -R a+rX /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 CMD ["python", "/app/healthcheck.py"]
CMD ["python", "-m", "cashflow_copilot.hosted"]
