# jobwatch container: runs one profile. Data lives in /data (mount a volume there).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    JOBWATCH_DATA=/data \
    JOBWATCH_DOCS=/data/docs \
    JOBWATCH_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py cvgen.py ./
COPY fonts ./fonts
COPY templates ./templates
COPY tools ./tools
COPY static ./static
COPY docker-entrypoint.sh ./

RUN chmod +x docker-entrypoint.sh && mkdir -p /data
# Runs as root on purpose: a Fly volume mounts root-owned, and a non-root user could not write to it.
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "app.py"]
