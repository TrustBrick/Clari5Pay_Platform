FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
# Build-time API base URL (baked into the bundle). Override via the compose build arg.
#   http://localhost:8001  → local dev (browser calls the published backend port)
#   "" (empty)             → production: same-origin; the browser calls /api on the
#                            same host and nginx proxies it to the backend container.
ARG VITE_API_BASE_URL=http://localhost:8001
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]