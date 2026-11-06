# PayCanary offline demo image. Zero keys, zero network needed: payouts are simulated.
# Live PayPal/Stripe/Haiku use is configured by environment variables (see INSTALL.md); this image defaults to the demo.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh .
RUN useradd -m paycanary && mkdir /data && chown -R paycanary /app /data
USER paycanary
ENV PAYCANARY_DEMO=1 PAYCANARY_DEMO_OFFLINE=1 PAYCANARY_DB=/data/paycanary.sqlite3
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
ENTRYPOINT ["sh", "docker-entrypoint.sh"]
