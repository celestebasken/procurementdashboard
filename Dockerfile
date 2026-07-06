# Docker deploy chosen over Render's native Python runtime specifically
# because WeasyPrint (PDF report generation, lib/pdf_report.py) needs the
# Pango/cairo/gdk-pixbuf system libraries below -- Render's native runtime
# doesn't give apt-get access, Docker does.
FROM python:3.13-slim

# WeasyPrint 69's actual native deps (verified against the dlopen() calls in
# its installed text/ffi.py, not just its docs -- newer WeasyPrint dropped
# the old cairo/GDK-Pixbuf chain in favor of Pillow + its own PDF backend,
# so it needs only glib/pango/harfbuzz/fontconfig now):
# libgobject-2.0-0 (from libglib2.0-0), libpango-1.0-0, libpangoft2-1.0-0,
# libharfbuzz.so.0 (from libharfbuzz0b), libfontconfig1. fonts-liberation
# provides real fonts to actually render with; shared-mime-info covers
# embedded-image MIME detection.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY lib/ lib/
COPY reference/ reference/

# Placeholder mount point for the Render Persistent Disk holding
# procurement.db -- see render.yaml's `disk` block and README.md's
# deployment runbook. Not populated at build time; the real db is uploaded
# post-deploy since it contains private campus purchasing data that never
# enters this (public) git repo or Docker image.
RUN mkdir -p /var/data

ENV PROCUREMENT_DB_PATH=/var/data/procurement.db
ENV SHOW_ADMIN_PAGE=false

EXPOSE 8501

CMD streamlit run app/Home.py --server.port ${PORT:-8501} --server.address 0.0.0.0 --server.headless true
