FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home syncroom
COPY app.py plex_auth.py ./
COPY static ./static
RUN mkdir /data && chown syncroom:syncroom /data
ENV SYNCROOM_DATA_DIR=/data
USER syncroom
EXPOSE 8765
CMD ["python", "app.py"]
