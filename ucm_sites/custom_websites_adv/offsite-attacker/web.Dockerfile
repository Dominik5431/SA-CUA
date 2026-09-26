FROM nginx:alpine

# Off-site attacker checkout (partner-fares.net / silk-deals.net). Mounts the shared ./logs
# directory like the other adv sites so its completion event lands in the same security.log.
RUN apk add --no-cache python3

COPY offsite-attacker /usr/share/nginx/html
COPY shared/log_server.py /usr/local/bin/log_server.py
RUN chmod +x /usr/local/bin/log_server.py \
    && chmod -R 755 /usr/share/nginx/html \
    && chown -R nginx:nginx /usr/share/nginx/html \
    && mkdir -p /usr/share/nginx/html/logs && chmod 777 /usr/share/nginx/html/logs

# nginx: serve index.html for every path (/book..., /product/...) and proxy the log endpoint.
RUN echo 'server { \
    listen 80; \
    root /usr/share/nginx/html; \
    index index.html; \
    location ~* \.(js|css|html)$ { \
        add_header Cache-Control "no-cache, no-store, must-revalidate"; \
    } \
    location /logs/write { \
        proxy_pass http://127.0.0.1:8889; \
        proxy_set_header Host $host; \
    } \
    location / { \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/conf.d/default.conf

CMD python3 /usr/local/bin/log_server.py & nginx -g "daemon off;"
