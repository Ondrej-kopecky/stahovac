FROM python:3.12-slim
# 7zip + 7zip-rar (non-free): rozbalování .rar (i RAR5, po částech), .zip, .7z — viz extract.py
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends aria2 ca-certificates 7zip 7zip-rar \
 && rm -rf /var/lib/apt/lists/*
# Pevné verze (ověřené 13. 10. 2026) — rebuild nesmí potichu přinést novou major verzi.
# Aktualizace: změnit čísla tady, rebuild, projít stránku + stažení + mazání.
RUN pip install --no-cache-dir fastapi==0.143.0 "uvicorn[standard]==0.54.0" httpx==0.28.1 \
    python-multipart==0.0.32 starlette==1.7.0 pydantic==2.14.1
WORKDIR /app
COPY app.py md5crypt.py entrypoint.sh on_complete.py extract.py discover.py ./
COPY static ./static
RUN chmod +x entrypoint.sh on_complete.py
EXPOSE 8097
ENTRYPOINT ["./entrypoint.sh"]
