FROM node:22-bookworm-slim
# Chromium from Debian (puppeteer's own download is skipped): whatsapp-web.js drives WhatsApp Web in it.
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates && rm -rf /var/lib/apt/lists/*
ENV PUPPETEER_SKIP_DOWNLOAD=true CHROMIUM=/usr/bin/chromium NODE_ENV=production
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY index.js ./
RUN useradd --uid 10002 --create-home whatsapp && mkdir /data && chown whatsapp /data
USER whatsapp
CMD ["node", "index.js"]
