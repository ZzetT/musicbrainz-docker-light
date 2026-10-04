ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-alpine

ARG PSYCOPG_VERSION=3.3.6
RUN pip install --no-cache-dir "psycopg[binary]==${PSYCOPG_VERSION}"

COPY pgsearch.py /usr/local/bin/pgsearch.py

USER nobody
EXPOSE 8000
CMD ["python", "/usr/local/bin/pgsearch.py"]
