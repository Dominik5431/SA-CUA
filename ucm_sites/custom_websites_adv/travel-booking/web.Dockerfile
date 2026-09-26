FROM nginx:alpine

# Install Python for simple log server
RUN apk add --no-cache python3

# Copy static website files to the nginx html directory
COPY travel-booking /usr/share/nginx/html
COPY shared /usr/share/nginx/html

# Fix permissions so nginx can read the files
RUN chmod -R 755 /usr/share/nginx/html && \
    chown -R nginx:nginx /usr/share/nginx/html

# Copy log server script
COPY shared/log_server.py /usr/local/bin/log_server.py
RUN chmod +x /usr/local/bin/log_server.py

# Create logs directory with write permissions
RUN mkdir -p /usr/share/nginx/html/logs && chmod 777 /usr/share/nginx/html/logs

# Configure nginx to proxy log requests and disable caching for JS/CSS
RUN echo 'server { \
    listen 80; \
    root /usr/share/nginx/html; \
    index index.html; \
    location ~* \.(js|css|html)$ { \
        add_header Cache-Control "no-cache, no-store, must-revalidate"; \
        add_header Pragma "no-cache"; \
        add_header Expires "0"; \
    } \
    location /logs/write { \
        proxy_pass http://127.0.0.1:8889; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
    } \
    location /logs/elements { \
        proxy_pass http://127.0.0.1:8889; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
    } \
    location / { \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/conf.d/default.conf

# Start log server and nginx
CMD python3 /usr/local/bin/log_server.py & nginx -g "daemon off;"
