FROM node:20-alpine

RUN apk add --no-cache tini

WORKDIR /app/templates

COPY templates/react/package.json ./react/package.json
COPY templates/react/vite.config.js templates/react/index.html ./react/
COPY templates/react/src ./react/src
RUN cd react && npm install --no-audit --no-fund

COPY templates/vue/package.json ./vue/package.json
COPY templates/vue/vite.config.js templates/vue/index.html ./vue/
COPY templates/vue/src ./vue/src
RUN cd vue && npm install --no-audit --no-fund

WORKDIR /workspace
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3000
ENTRYPOINT ["tini", "--"]
CMD ["/entrypoint.sh"]
