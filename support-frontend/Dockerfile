FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
# Build-time API base. "" (empty) = same-origin (browser calls /api on this host;
# nginx proxies it to the backend) — used in production behind Caddy.
ARG VITE_API_BASE_URL=http://localhost:8001
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
# "demo" shows the DemoBanner. Unset = "production".
ARG VITE_APP_ENV=production
ENV VITE_APP_ENV=$VITE_APP_ENV
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3002
CMD ["nginx", "-g", "daemon off;"]
