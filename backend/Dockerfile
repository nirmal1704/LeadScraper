FROM python:3.12-slim

# Install system deps for Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
    fonts-liberation libappindicator3-1 xdg-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Create __init__.py files
RUN touch llm/__init__.py scrapers/__init__.py enrichment/__init__.py \
    workers/__init__.py routes/__init__.py output/__init__.py

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
