FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY server.js ./
COPY src ./src
COPY sandbox-image ./sandbox-image
EXPOSE 4000
CMD ["node", "server.js"]
