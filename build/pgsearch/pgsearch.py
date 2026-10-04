#!/usr/bin/env python3

"""MusicBrainz web service search backed by Postgres instead of Solr.

Answers `GET /ws/2/<entity>?query=...&fmt=json` for the artist, label,
recording, release, release-group and series entities, using the
full-text (GIN) indexes that the MusicBrainz database already has.

It understands the subset of the Lucene query syntax that client
applications commonly send: fields, phrases, terms, trailing wildcards,
boosts (ignored), AND/OR/NOT, +/- and parentheses. It finds the matching
entities in Postgres, ranks them, then fetches each one from the local
MusicBrainz web service (lookup) so that results have the usual shape,
and adds the `score` field like Solr search does.

Anything it cannot answer is reported with HTTP status 501 (unsupported
query or format) or 504 (query too slow), which the gateway can forward
to musicbrainz.org.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

LOG = logging.getLogger('pgsearch')

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


class Unsupported(Exception):
    """The query cannot be answered by this backend (HTTP 501)."""


class BadQuery(Exception):
    """The query is malformed (HTTP 400)."""


################################################################################
# Lucene query parser
################################################################################

class Leaf:
    """A field clause: `field:"phrase"`, `field:term`, `field:pre*` or bare text."""

    def __init__(self, field, text, phrase=False, prefix=False):
        self.field = field
        self.text = text
        self.phrase = phrase
        self.prefix = prefix

    def __repr__(self):
        kind = 'phrase' if self.phrase else 'prefix' if self.prefix else 'terms'
        return f'Leaf({self.field}, {kind}, {self.text!r})'


class And:
    def __init__(self, items, exclude=()):
        self.items = list(items)
        self.exclude = list(exclude)

    def __repr__(self):
        return f'And({self.items}, not={self.exclude})'


class Or:
    def __init__(self, items, exclude=()):
        self.items = list(items)
        self.exclude = list(exclude)

    def __repr__(self):
        return f'Or({self.items}, not={self.exclude})'


class Not:
    def __init__(self, item):
        self.item = item


_SPECIAL = set('()":^~[]{}')


def tokenize(query):
    tokens = []
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
        elif c in '()':
            tokens.append((c, c))
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and query[i] != '"':
                if query[i] == '\\' and i + 1 < n:
                    i += 1
                buf.append(query[i])
                i += 1
            if i >= n:
                raise BadQuery('unterminated phrase')
            i += 1
            tokens.append(('PHRASE', ''.join(buf)))
        elif c in '^~':
            i += 1
            while i < n and (query[i].isdigit() or query[i] == '.'):
                i += 1
            # Boosts and proximity are irrelevant to matching
        elif c == ':':
            tokens.append((':', ':'))
            i += 1
        elif c in '[]{}':
            raise Unsupported('range queries')
        elif c in '+-!' and (i + 1 < n and not query[i + 1].isspace()) and (
                not tokens or tokens[-1][0] in ('(', 'OP', 'NOT', 'PLUS', 'MINUS', ':')
                or query[i - 1].isspace()):
            tokens.append(({'+': 'PLUS', '-': 'MINUS', '!': 'NOT'}[c], c))
            i += 1
        else:
            buf = []
            while i < n and not query[i].isspace() and query[i] not in _SPECIAL:
                if query[i] == '\\' and i + 1 < n:
                    i += 1
                buf.append(query[i])
                i += 1
            word = ''.join(buf)
            if word in ('AND', '&&'):
                tokens.append(('OP', 'AND'))
            elif word in ('OR', '||'):
                tokens.append(('OP', 'OR'))
            elif word == 'NOT':
                tokens.append(('NOT', 'NOT'))
            else:
                tokens.append(('TERM', word))
    return tokens


class Parser:
    def __init__(self, query):
        self.tokens = tokenize(query)
        self.pos = 0

    def peek(self, offset=0):
        if self.pos + offset < len(self.tokens):
            return self.tokens[self.pos + offset]
        return (None, None)

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def parse(self):
        if not self.tokens:
            raise BadQuery('empty query')
        node = self.parse_or(None)
        if self.pos != len(self.tokens):
            raise BadQuery(f'unexpected {self.peek()[1]!r}')
        if isinstance(node, Not):
            raise Unsupported('purely negative queries')
        return node

    def starts_clause(self):
        kind = self.peek()[0]
        return kind in ('(', 'PHRASE', 'TERM', 'NOT', 'PLUS', 'MINUS')

    def parse_or(self, field):
        items = [self.parse_and(field)]
        while True:
            kind, value = self.peek()
            if kind == 'OP' and value == 'OR':
                self.take()
                items.append(self.parse_and(field))
            elif self.starts_clause():
                # Juxtaposition means OR by default
                items.append(self.parse_and(field))
            else:
                break
        return combine(Or, items)

    def parse_and(self, field):
        items = [self.parse_unary(field)]
        while self.peek() == ('OP', 'AND'):
            self.take()
            items.append(self.parse_unary(field))
        return combine(And, items)

    def parse_unary(self, field):
        kind, _ = self.peek()
        if kind in ('NOT', 'MINUS'):
            self.take()
            return Not(self.parse_unary(field))
        if kind == 'PLUS':
            self.take()
            return self.parse_unary(field)
        return self.parse_primary(field)

    def parse_primary(self, field, field_implicit=True):
        kind, value = self.take()
        if kind == '(':
            node = self.parse_or(field)
            if self.take()[0] != ')':
                raise BadQuery('missing )')
            return node
        if kind == 'TERM' and self.peek()[0] == ':':
            self.take()
            field_name = value.lower()
            kind, value = self.peek()
            if kind == '(':
                self.take()
                node = self.parse_or(field_name)
                if self.take()[0] != ')':
                    raise BadQuery('missing )')
                return node
            if kind not in ('TERM', 'PHRASE'):
                raise BadQuery(f'missing value for field {field_name}')
            return self.parse_primary(field_name, field_implicit=False)
        if kind == 'PHRASE':
            leaf = Leaf(field, value, phrase=True)
        elif kind == 'TERM':
            # Consecutive plain terms are kept together as one clause
            # (all words required), rather than Lucene's OR of each word,
            # which would match far too much without Solr's ranking.
            words = [value]
            while (field_implicit and self.peek()[0] == 'TERM'
                   and self.peek(1)[0] != ':' and not words[-1].endswith('*')):
                words.append(self.take()[1])
            text = ' '.join(words)
            if text == '*':
                raise Unsupported('match-all queries')
            if '?' in text or '*' in text.rstrip('*'):
                raise Unsupported('wildcards inside terms')
            leaf = Leaf(field, text.rstrip('*'), prefix=text.endswith('*'))
        else:
            raise BadQuery(f'unexpected {value!r}')
        return leaf


def combine(cls, items):
    if len(items) == 1:
        return items[0]
    positives = [item for item in items if not isinstance(item, Not)]
    negatives = [item.item for item in items if isinstance(item, Not)]
    if not positives:
        raise Unsupported('purely negative queries')
    if len(positives) == 1 and not negatives:
        return positives[0]
    return cls(positives, negatives)


def parse_query(query):
    return Parser(query).parse()


################################################################################
# SQL compilation
################################################################################

def tsv(column):
    return f'mb_simple_tsvector({column}::text)'


def tsquery(leaf):
    if leaf.phrase:
        return "phraseto_tsquery('mb_simple', mb_lower(%s))"
    if leaf.prefix:
        return "(plainto_tsquery('mb_simple', mb_lower(%s))::text || ':*')::tsquery"
    return "plainto_tsquery('mb_simple', mb_lower(%s))"


def name_set(table, column='name'):
    def build(leaf):
        return f'SELECT id FROM {table} WHERE {tsv(column)} @@ {tsquery(leaf)}', [leaf.text]
    return build


def alias_set(table, fk):
    def build(leaf):
        q = tsquery(leaf)
        return (f'SELECT {fk} AS id FROM {table}_alias WHERE {tsv("name")} @@ {q}'
                f' UNION SELECT {fk} FROM {table}_alias WHERE {tsv("sort_name")} @@ {q}',
                [leaf.text, leaf.text])
    return build


def tag_set(table, fk):
    def build(leaf):
        return (f'SELECT et.{fk} AS id FROM {table}_tag et JOIN tag ON tag.id = et.tag'
                f' WHERE {tsv("tag.name")} @@ {tsquery(leaf)}', [leaf.text])
    return build


def gid_set(sql):
    """`sql` selects entity ids for one `%s::uuid` parameter."""
    def build(leaf):
        value = leaf.text.strip()
        if not UUID_RE.match(value):
            return 'SELECT NULL::integer AS id WHERE false', []
        return sql, [value]
    return build


def exact_set(sql, transform=lambda v: v.strip()):
    def build(leaf):
        return sql, [transform(leaf.text)]
    return build


def credit_set(table):
    def build(leaf):
        q = tsquery(leaf)
        return (f'SELECT e.id FROM {table} e WHERE e.artist_credit IN ('
                f'SELECT id FROM artist_credit WHERE {tsv("name")} @@ {q}'
                f' UNION SELECT artist_credit FROM artist_credit_name WHERE {tsv("name")} @@ {q})',
                [leaf.text, leaf.text])
    return build


def union_of(*builders):
    def build(leaf):
        parts, params = [], []
        for builder in builders:
            sql, p = builder(leaf)
            parts.append(sql)
            params.extend(p)
        return ' UNION '.join(f'({part})' for part in parts), params
    return build


def arid_set(table):
    return gid_set(
        f'SELECT e.id FROM {table} e'
        f' JOIN artist_credit_name acn ON acn.artist_credit = e.artist_credit'
        f' JOIN artist a ON a.id = acn.artist WHERE a.gid = %s::uuid')


def type_set(table, type_table):
    return exact_set(
        f'SELECT e.id FROM {table} e JOIN {type_table} t ON t.id = e.type'
        f' WHERE lower(t.name) = lower(%s)')


RECORDING_RELEASE_JOIN = (
    'SELECT t.recording AS id FROM track t'
    ' JOIN medium m ON m.id = t.medium JOIN release r ON r.id = m.release')


ENTITIES = {
    'artist': {
        'table': 'artist', 'plural': 'artists', 'inc': 'aliases+tags',
        'credit': False,
        'fields': {
            'artist': name_set('artist'),
            'artistaccent': name_set('artist'),
            'name': name_set('artist'),
            'sortname': name_set('artist', 'sort_name'),
            'alias': alias_set('artist', 'artist'),
            'primary_alias': alias_set('artist', 'artist'),
            'tag': tag_set('artist', 'artist'),
            'arid': gid_set('SELECT id FROM artist WHERE gid = %s::uuid'),
            'type': type_set('artist', 'artist_type'),
        },
        'default': ('artist', 'sortname', 'alias'),
        'name_fields': ('artist', 'artistaccent', 'name'),
    },
    'label': {
        'table': 'label', 'plural': 'labels', 'inc': 'aliases+tags',
        'credit': False,
        'fields': {
            'label': name_set('label'),
            'labelaccent': name_set('label'),
            'name': name_set('label'),
            'alias': alias_set('label', 'label'),
            'tag': tag_set('label', 'label'),
            'laid': gid_set('SELECT id FROM label WHERE gid = %s::uuid'),
            'type': type_set('label', 'label_type'),
        },
        'default': ('label', 'alias'),
        'name_fields': ('label', 'labelaccent', 'name'),
    },
    'series': {
        'table': 'series', 'plural': 'series', 'inc': 'aliases+tags',
        'credit': False,
        'fields': {
            'series': name_set('series'),
            'seriesaccent': name_set('series'),
            'name': name_set('series'),
            'alias': alias_set('series', 'series'),
            'tag': tag_set('series', 'series'),
            'sid': gid_set('SELECT id FROM series WHERE gid = %s::uuid'),
            'type': type_set('series', 'series_type'),
        },
        'default': ('series', 'alias'),
        'name_fields': ('series', 'seriesaccent', 'name'),
    },
    'release': {
        'table': 'release', 'plural': 'releases',
        'inc': 'artist-credits+labels+release-groups+media+tags',
        'credit': True,
        'fields': {
            'release': name_set('release'),
            'releaseaccent': name_set('release'),
            'alias': alias_set('release', 'release'),
            'artist': credit_set('release'),
            'artistname': credit_set('release'),
            'creditname': credit_set('release'),
            'arid': arid_set('release'),
            'reid': gid_set('SELECT id FROM release WHERE gid = %s::uuid'),
            'rgid': gid_set(
                'SELECT r.id FROM release r JOIN release_group rg ON rg.id = r.release_group'
                ' WHERE rg.gid = %s::uuid'),
            'releasegroup': lambda leaf: (
                'SELECT r.id FROM release r WHERE r.release_group IN ('
                f'SELECT id FROM release_group WHERE {tsv("name")} @@ {tsquery(leaf)})',
                [leaf.text]),
            'barcode': exact_set('SELECT id FROM release WHERE barcode = %s'),
            'status': exact_set(
                'SELECT r.id FROM release r JOIN release_status s ON s.id = r.status'
                ' WHERE lower(s.name) = lower(%s)'),
            'primarytype': exact_set(
                'SELECT r.id FROM release r JOIN release_group rg ON rg.id = r.release_group'
                ' JOIN release_group_primary_type t ON t.id = rg.type'
                ' WHERE lower(t.name) = lower(%s)'),
            'tag': tag_set('release', 'release'),
        },
        'default': ('release',),
        'name_fields': ('release', 'releaseaccent'),
    },
    'release-group': {
        'table': 'release_group', 'plural': 'release-groups',
        'inc': 'artist-credits+releases+tags',
        'credit': True,
        'fields': {
            'releasegroup': name_set('release_group'),
            'releasegroupaccent': name_set('release_group'),
            'alias': alias_set('release_group', 'release_group'),
            'release': lambda leaf: (
                f'SELECT release_group AS id FROM release WHERE {tsv("name")} @@ {tsquery(leaf)}',
                [leaf.text]),
            'artist': credit_set('release_group'),
            'artistname': credit_set('release_group'),
            'creditname': credit_set('release_group'),
            'arid': arid_set('release_group'),
            'rgid': gid_set('SELECT id FROM release_group WHERE gid = %s::uuid'),
            'reid': gid_set('SELECT release_group AS id FROM release WHERE gid = %s::uuid'),
            'primarytype': type_set('release_group', 'release_group_primary_type'),
            'secondarytype': exact_set(
                'SELECT j.release_group AS id FROM release_group_secondary_type_join j'
                ' JOIN release_group_secondary_type t ON t.id = j.secondary_type'
                ' WHERE lower(t.name) = lower(%s)'),
            'tag': tag_set('release_group', 'release_group'),
        },
        'default': ('releasegroup',),
        'name_fields': ('releasegroup', 'releasegroupaccent'),
    },
    'recording': {
        'table': 'recording', 'plural': 'recordings',
        'inc': 'artist-credits+releases+release-groups+media+isrcs+tags',
        'credit': True,
        'fields': {
            'recording': name_set('recording'),
            'recordingaccent': name_set('recording'),
            'alias': alias_set('recording', 'recording'),
            'artist': credit_set('recording'),
            'artistname': credit_set('recording'),
            'creditname': credit_set('recording'),
            'arid': arid_set('recording'),
            'rid': gid_set('SELECT id FROM recording WHERE gid = %s::uuid'),
            'reid': gid_set(RECORDING_RELEASE_JOIN + ' WHERE r.gid = %s::uuid'),
            'rgid': gid_set(
                RECORDING_RELEASE_JOIN + ' JOIN release_group rg ON rg.id = r.release_group'
                ' WHERE rg.gid = %s::uuid'),
            'release': lambda leaf: (
                RECORDING_RELEASE_JOIN + f' WHERE {tsv("r.name")} @@ {tsquery(leaf)}',
                [leaf.text]),
            'isrc': exact_set('SELECT recording AS id FROM isrc WHERE isrc = %s',
                              lambda v: v.strip().upper()),
            'tag': tag_set('recording', 'recording'),
        },
        'default': ('recording',),
        'name_fields': ('recording', 'recordingaccent'),
    },
}

ARTIST_FIELDS = ('artist', 'artistname', 'creditname')


def compile_node(spec, node):
    """Return (sql, params) selecting the ids of matching entities."""
    if isinstance(node, Leaf):
        fields = spec['fields']
        if node.field is None:
            builders = [fields[name] for name in spec['default']]
            return union_of(*builders)(node)
        builder = fields.get(node.field)
        if builder is None:
            raise Unsupported(f'field {node.field!r}')
        return builder(node)

    operator = ' INTERSECT ' if isinstance(node, And) else ' UNION '
    parts, params = [], []
    for item in node.items:
        sql, p = compile_node(spec, item)
        parts.append(f'({sql})')
        params.extend(p)
    sql = operator.join(parts)
    if node.exclude:
        sql = f'({sql})'
        for item in node.exclude:
            excluded, p = compile_node(spec, item)
            sql += f' EXCEPT ({excluded})'
            params.extend(p)
    return sql, params


def positive_leaves(node):
    if isinstance(node, Leaf):
        yield node
    elif isinstance(node, (And, Or)):
        for item in node.items:
            yield from positive_leaves(item)


def rank_terms(spec, node, trgm):
    """Pick the strings used to rank candidates by similarity."""
    name_text = artist_text = tag_leaf = None
    for leaf in positive_leaves(node):
        if leaf.field is None or leaf.field in spec['name_fields']:
            name_text = name_text or leaf.text
        elif leaf.field in ARTIST_FIELDS and spec['credit']:
            artist_text = artist_text or leaf.text
        elif leaf.field == 'tag':
            tag_leaf = tag_leaf or leaf

    parts, params = [], []

    def similarity(column, text, weight):
        parts.append(f'{weight * 2} * (mb_lower({column}) = mb_lower(%s))::int')
        params.append(text)
        if trgm:
            parts.append(f'{weight} * similarity(mb_lower({column}), mb_lower(%s))')
            params.append(text)

    if name_text:
        similarity('e.name', name_text, 1.0)
    if artist_text:
        similarity('ac.name', artist_text, 0.6)
    if tag_leaf and not name_text:
        table = spec['table']
        parts.append(
            f'(SELECT coalesce(max(et.count), 0) FROM {table}_tag et JOIN tag ON tag.id = et.tag'
            f' WHERE et.{table} = e.id AND {tsv("tag.name")} @@ {tsquery(tag_leaf)})')
        params.append(tag_leaf.text)
    if not parts:
        return '0', []
    return ' + '.join(parts), params


def build_search_sql(entity, query, trgm):
    spec = ENTITIES[entity]
    node = parse_query(query)
    candidates_sql, candidates_params = compile_node(spec, node)
    rank_sql, rank_params = rank_terms(spec, node, trgm)
    table = spec['table']
    join_credit = ' JOIN artist_credit ac ON ac.id = e.artist_credit' if spec['credit'] else ''
    sql = (
        f'WITH candidates AS (SELECT DISTINCT id FROM ({candidates_sql}) matches)'
        ' SELECT gid, rank, count(*) OVER () AS total, max(rank) OVER () AS top'
        f' FROM (SELECT e.gid::text AS gid, e.name, e.id, {rank_sql} AS rank'
        f' FROM {table} e JOIN candidates c ON c.id = e.id{join_credit}) ranked'
        ' ORDER BY rank DESC, name, id LIMIT %s OFFSET %s')
    return sql, candidates_params + rank_params


################################################################################
# Database and web service access
################################################################################

class Database:
    def __init__(self, conninfo, size, statement_timeout_ms):
        import psycopg
        self.psycopg = psycopg
        self.conninfo = conninfo
        self.statement_timeout_ms = statement_timeout_ms
        self.pool = Queue()
        for _ in range(size):
            self.pool.put(None)
        self.trgm = self._setup()

    def _connect(self):
        conn = self.psycopg.connect(self.conninfo, autocommit=True)
        conn.execute('SET search_path = musicbrainz, public')
        conn.execute(f'SET statement_timeout = {int(self.statement_timeout_ms)}')
        conn.execute('SET default_transaction_read_only = on')
        return conn

    def _setup(self):
        try:
            with self.psycopg.connect(self.conninfo, autocommit=True) as conn:
                conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
            return True
        except Exception as error:  # pylint: disable=broad-except
            LOG.warning('pg_trgm unavailable, ranking on exact matches only: %s', error)
            return False

    def query(self, sql, params):
        conn = self.pool.get()
        try:
            if conn is None or conn.closed:
                conn = self._connect()
            return conn.execute(sql, params).fetchall()
        except self.psycopg.OperationalError:
            if conn is not None:
                conn.close()
            conn = None
            raise
        finally:
            self.pool.put(conn)


class Lookups:
    def __init__(self, base_url, workers, user_agent):
        self.base_url = base_url.rstrip('/')
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.user_agent = user_agent

    def fetch(self, entity, mbid, inc):
        url = f'{self.base_url}/ws/2/{entity}/{mbid}?fmt=json&inc={inc}'
        request = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def fetch_all(self, entity, mbids, inc):
        return list(self.executor.map(lambda mbid: self.fetch(entity, mbid, inc), mbids))


def shape(entity, item, score):
    item = {'id': item['id'], 'score': score, **{k: v for k, v in item.items() if k != 'id'}}
    if entity == 'release' and 'media' in item and 'track-count' not in item:
        item['track-count'] = sum(medium.get('track-count') or 0 for medium in item['media'])
    return item


class SearchService:
    def __init__(self, database, lookups):
        self.database = database
        self.lookups = lookups

    def search(self, entity, query, limit, offset):
        spec = ENTITIES[entity]
        sql, params = build_search_sql(entity, query, self.database.trgm)
        try:
            rows = self.database.query(sql, params + [limit, offset])
        except self.database.psycopg.errors.QueryCanceled as error:
            raise TimeoutError(str(error)) from error

        total = rows[0][2] if rows else 0
        items = self.lookups.fetch_all(entity, [row[0] for row in rows], spec['inc'])

        results = []
        for (_, rank, _, top), item in zip(rows, items):
            if item is None:
                continue
            # Like Solr, the best match overall scores 100
            score = max(1, round(100 * float(rank) / float(top))) if top else 100
            results.append(shape(entity, item, score))

        return {
            'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            'count': total,
            'offset': offset,
            spec['plural']: results,
        }


################################################################################
# HTTP server
################################################################################

PATH_RE = re.compile(r'^/ws/2/([a-z-]+)/?$')


class Handler(BaseHTTPRequestHandler):
    service = None
    server_version = 'mb-pgsearch'

    def log_message(self, fmt, *args):  # pylint: disable=arguments-differ
        LOG.info('%s %s', self.address_string(), fmt % args)

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_HEAD(self):  # pylint: disable=invalid-name
        self.do_GET()

    def do_GET(self):  # pylint: disable=invalid-name
        url = urllib.parse.urlsplit(self.path)
        if url.path in ('/health', '/ws/2/health'):
            self.send_json(200, {'status': 'ok'})
            return

        match = PATH_RE.match(url.path)
        if not match or match.group(1) not in ENTITIES:
            self.send_json(404, {'error': 'Not found'})
            return
        entity = match.group(1)

        args = urllib.parse.parse_qs(url.query)
        query = (args.get('query') or [''])[0]
        fmt = (args.get('fmt') or [''])[0]
        wants_json = fmt == 'json' or (not fmt and 'application/json' in self.headers.get('Accept', ''))
        if not wants_json:
            self.send_json(501, {'error': 'Only fmt=json is supported by this search backend'})
            return
        if (args.get('dismax') or [''])[0] == 'true':
            query = '"' + query.replace('\\', '\\\\').replace('"', '\\"') + '"'

        try:
            limit = min(MAX_LIMIT, max(1, int((args.get('limit') or [DEFAULT_LIMIT])[0])))
            offset = max(0, int((args.get('offset') or [0])[0]))
        except ValueError:
            self.send_json(400, {'error': 'Invalid limit or offset'})
            return

        started = time.monotonic()
        try:
            body = self.service.search(entity, query, limit, offset)
        except BadQuery as error:
            self.send_json(400, {'error': f'Invalid query: {error}'})
            return
        except Unsupported as error:
            self.send_json(501, {'error': f'Unsupported by this search backend: {error}'})
            return
        except TimeoutError:
            self.send_json(504, {'error': 'Search took too long'})
            return
        except Exception:  # pylint: disable=broad-except
            LOG.exception('search failed: %s %r', entity, query)
            self.send_json(500, {'error': 'Internal error'})
            return
        LOG.debug('%s %r took %.3fs', entity, query, time.monotonic() - started)
        self.send_json(200, body)


def main():
    logging.basicConfig(
        level=os.environ.get('PGSEARCH_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(message)s')

    conninfo = ' '.join([
        f"host={os.environ.get('MUSICBRAINZ_POSTGRES_READONLY_SERVER', 'db')}",
        f"port={os.environ.get('MUSICBRAINZ_POSTGRES_PORT', '5432')}",
        f"dbname={os.environ.get('MUSICBRAINZ_POSTGRES_DATABASE', 'musicbrainz_db')}",
        f"user={os.environ.get('POSTGRES_USER', 'musicbrainz')}",
        f"password={os.environ.get('POSTGRES_PASSWORD', 'musicbrainz')}",
        'application_name=pgsearch',
    ])
    database = Database(
        conninfo,
        size=int(os.environ.get('PGSEARCH_CONNECTIONS', '4')),
        statement_timeout_ms=int(os.environ.get('PGSEARCH_STATEMENT_TIMEOUT_MS', '15000')))
    lookups = Lookups(
        os.environ.get('PGSEARCH_MUSICBRAINZ_URL', 'http://musicbrainz:5000'),
        workers=int(os.environ.get('PGSEARCH_LOOKUP_WORKERS', '4')),
        user_agent='musicbrainz-docker-light-pgsearch/1.0')

    Handler.service = SearchService(database, lookups)
    port = int(os.environ.get('PGSEARCH_PORT', '8000'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    server.daemon_threads = True
    LOG.info('Listening on port %d (pg_trgm ranking: %s)', port, database.trgm)
    server.serve_forever()


if __name__ == '__main__':
    main()
