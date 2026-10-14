FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home syncroom
COPY app.py .
COPY static ./static
USER syncroom
EXPOSE 8765
CMD ["python", "app.py"]
