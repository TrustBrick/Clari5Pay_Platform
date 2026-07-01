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
# Which portal this build is: merchant | admin | superadmin | app (default = combined chooser).
ARG VITE_PORTAL=app
ENV VITE_PORTAL=$VITE_PORTAL
# "demo" shows the DemoBanner + (on the SA build) the Demo Tools reset page. Unset = "production".
ARG VITE_APP_ENV=production
ENV VITE_APP_ENV=$VITE_APP_ENV
# Portal base URLs baked into the bundle (see src/utils/portal.ts). Defaults are Production's
# domains; Demo/UAT builds override these to the demo-*.win365jackpot.com subdomains.
ARG VITE_MERCHANT_URL=https://win365jackpot.com
ENV VITE_MERCHANT_URL=$VITE_MERCHANT_URL
ARG VITE_ADMIN_URL=https://admin.win365jackpot.com
ENV VITE_ADMIN_URL=$VITE_ADMIN_URL
ARG VITE_SA_URL=https://sa.win365jackpot.com
ENV VITE_SA_URL=$VITE_SA_URL
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]