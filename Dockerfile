FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY requirements.txt ./
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY . ./
ENV PATH="/opt/venv/bin:$PATH"
EXPOSE 10000
CMD ["sh", "-c", "uvicorn invoice_app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
