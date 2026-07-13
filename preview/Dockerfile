# Build from the REPO ROOT, not this directory -- see broker/Dockerfile
# for why. docker-compose.yml handles this for you; to build by hand:
#   docker build -f preview/Dockerfile -t eink-preview .

FROM python:3.12-slim

WORKDIR /app

# python:3.12-slim ships with NO TrueType fonts at all. Without this,
# shared/dashboard_render/fonts.py can't find DejaVu Sans (Bold), falls
# back all the way to PIL's ImageFont.load_default() -- which ignores
# whatever size _fit_font() asks for, silently capping every
# auto-sized widget number at a tiny fixed size no matter how large the
# widget's box is. pi_client/install.sh installs this same package on the
# physical Pi; this is the equivalent for the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY preview/requirements.txt preview/requirements.txt
RUN pip install --no-cache-dir -r preview/requirements.txt

COPY shared/ shared/
COPY preview/ preview/

WORKDIR /app/preview
EXPOSE 9090

# Config comes from environment variables (DASHBOARD_TOKEN, BROKER_URL --
# see .env.example and docker-compose.yml at the repo root).
CMD ["python", "server.py"]
