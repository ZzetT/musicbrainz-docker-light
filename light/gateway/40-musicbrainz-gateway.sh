#!/bin/sh

# Generate the nginx configuration of the MusicBrainz web service gateway.
#
# Requests are answered by the local mirror, except:
# - search queries (`/ws/2/<entity>?query=...`) for entities whose Solr
#   collection is not listed in MB_SEARCH_CORES,
# - requests that need a MusicBrainz account (collections, any non-GET),
# which are forwarded to MB_UPSTREAM_HOST (rate limited) when it is set,
# or answered with 503 otherwise.

set -e -u

MB_SEARCH_CORES="${MB_SEARCH_CORES:-artist label recording release release-group series}"
MB_UPSTREAM_HOST="${MB_UPSTREAM_HOST-musicbrainz.org}"
MB_UPSTREAM_RATE="${MB_UPSTREAM_RATE:-1r/s}"
MB_LOCAL_SERVER="${MB_LOCAL_SERVER:-musicbrainz:5000}"

local_cores=$(echo "$MB_SEARCH_CORES" | tr ', ' '\n' | grep -v '^$' | paste -s -d '|' -)
if [ -z "$local_cores" ]
then
  local_cores='-no-local-search-core-'
fi

if [ -n "$MB_UPSTREAM_HOST" ]
then
  upstream_action="limit_req zone=mb_upstream burst=30;
        set \$mb_upstream_host $MB_UPSTREAM_HOST;
        proxy_pass https://\$mb_upstream_host;
        proxy_ssl_server_name on;
        proxy_set_header Host \$mb_upstream_host;
        proxy_set_header X-Forwarded-For \"\";
        proxy_set_header X-Real-IP \"\";"
else
  upstream_action="default_type application/json;
        return 503 '{\"error\": \"Not available on this mirror (no local search index and no upstream configured)\"}';"
fi

cat > /etc/nginx/conf.d/default.conf <<EOF
resolver 127.0.0.11 valid=300s ipv6=off;

limit_req_zone \$mb_upstream_key zone=mb_upstream:1m rate=$MB_UPSTREAM_RATE;
limit_req_status 503;

map \$uri \$mb_entity {
    "~^/ws/2/(?<entity>[a-z-]+)/?\$" \$entity;
    default "";
}

map \$arg_query \$mb_is_search {
    "" 0;
    default 1;
}

map "\$mb_is_search:\$mb_entity" \$mb_search_route {
    "~^1:($local_cores)\$" local;
    "~^1:" upstream;
    default local;
}

map "\$request_method:\$uri" \$mb_method_route {
    "~^(GET|HEAD|OPTIONS):/ws/2/collection" upstream;
    "~^(GET|HEAD|OPTIONS):" local;
    default upstream;
}

map "\$mb_method_route:\$mb_search_route" \$mb_route {
    "local:local" local;
    default upstream;
}

map \$mb_route \$mb_upstream_key {
    upstream "upstream";
    default "";
}

server {
    listen 80 default_server;
    server_name _;

    location = /ws/2/health {
        default_type text/plain;
        return 200 "ok\n";
    }

    location /ws/2/ {
        error_page 418 = @upstream;
        if (\$mb_route = upstream) {
            return 418;
        }

        set \$mb_local_server $MB_LOCAL_SERVER;
        proxy_pass http://\$mb_local_server;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    location @upstream {
        $upstream_action
    }

    location / {
        default_type application/json;
        return 404 '{"error": "This mirror serves the web service API only, under /ws/2/"}';
    }
}
EOF

echo "$0: local search cores: $local_cores; upstream: ${MB_UPSTREAM_HOST:-(disabled)}"
