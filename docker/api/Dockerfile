# Local dev: the FastAPI app in the Docker VM next to Typesense (docker-compose.yml, service "api").
# The code is mounted from the repository, so only the dependencies live in the image.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1
