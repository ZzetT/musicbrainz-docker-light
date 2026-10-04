#!/bin/bash

# Start MusicBrainz Server for the web service API (/ws/2) only.
#
# Unlike start.sh, it neither recompiles the website's static resources
# (webpack) nor starts the server-side React renderer (Node.js),
# which are only needed by HTML pages of the website.

set -e -u

MUSICBRAINZ_VALKEY_SERVER="${MUSICBRAINZ_VALKEY_SERVER:-${MUSICBRAINZ_REDIS_SERVER:-valkey}}"

dockerize \
  -wait "tcp://${MUSICBRAINZ_POSTGRES_SERVER}:5432" -timeout 60s \
  -wait "tcp://${MUSICBRAINZ_VALKEY_SERVER}:6379" -timeout 60s \
  true

if [ -f /crons.conf ] && [ -s /crons.conf ]
then
  crontab /crons.conf
  cron -f &
fi

exec carton exec -- start_server --port=5000 -- plackup -I lib -s Starlet -E deployment --max-workers "${MUSICBRAINZ_SERVER_PROCESSES}" --max-reqs-per-child "${MUSICBRAINZ_MAX_REQUESTS_PER_WORKER:-1000}" --pid fcgi.pid
