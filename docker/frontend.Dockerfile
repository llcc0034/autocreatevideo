FROM node:22-alpine AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/nginx-main.conf /etc/nginx/nginx.conf
COPY docker/security-headers.conf /etc/nginx/security-headers.conf
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -rf /usr/share/nginx/html/* \
    && mkdir -p /usr/share/nginx/html /tmp/nginx \
    && chown -R nginx:nginx /usr/share/nginx/html /tmp/nginx
COPY --chown=nginx:nginx --from=build /app/dist /usr/share/nginx/html

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -T 2 -O - http://127.0.0.1:8080/healthz >/dev/null || exit 1
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
