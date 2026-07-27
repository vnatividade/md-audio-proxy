FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py transform.js .
COPY icon-192.png icon-512.png apple-touch-icon.png landing.html ./

ENV PORT=8000
EXPOSE 8000

# 1 processo (fila em memória compartilhada) + threads pra atender navegador e
# worker ao mesmo tempo. Fila protegida por lock.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} -w 1 --threads 8 -k gthread --timeout 120 app:app"]
