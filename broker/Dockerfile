# Build from the REPO ROOT, not this directory -- the broker needs
# shared/dashboard_render alongside it, same as when run natively.
# docker-compose.yml already sets this up correctly; to build by hand:
#   docker build -f broker/Dockerfile -t eink-broker .

FROM python:3.12-slim

WORKDIR /app

COPY broker/requirements.txt broker/requirements.txt
RUN pip install --no-cache-dir -r broker/requirements.txt

COPY shared/ shared/
COPY broker/ broker/

WORKDIR /app/broker
EXPOSE 8080

# Config comes from environment variables (DASHBOARD_TOKEN, see
# .env.example at the repo root) rather than a mounted config.yaml --
# keeps secrets out of the image and out of any bind-mounted file.
CMD ["python", "app.py"]
