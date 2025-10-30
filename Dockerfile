# =========================
# BASE IMAGE
# =========================
FROM python:3.11-slim

# =========================
# ENV & WORKDIR
# =========================
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# =========================
# INSTALL SYSTEM DEPENDENCIES
# =========================
# Includes Node.js (required for subprocess JS), npm, and git
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg git build-essential \
 && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
 && apt-get install -y nodejs \
 && npm install -g meriyah estraverse \
 && rm -rf /var/lib/apt/lists/*

# =========================
# COPY AND INSTALL PYTHON DEPS
# =========================
COPY requirements.txt ./
RUN pip install -r requirements.txt

# =========================
# COPY PROJECT FILES
# =========================
COPY . .

# =========================
# EXPOSE PORT & RUN APP
# =========================
EXPOSE 8080
CMD ["python", "main.py"]
