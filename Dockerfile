FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "mcp>=1.0.0"

# Copy project
COPY . .

# Gateway URL — override at runtime if needed
ENV AGENTPAY_GATEWAY_URL=https://gateway-production-2cc2.up.railway.app

# STELLAR_SECRET_KEY must be passed at runtime:
#   docker run -e STELLAR_SECRET_KEY=S... agentpay-mcp

ENTRYPOINT ["python", "gateway/mcp_server.py"]
