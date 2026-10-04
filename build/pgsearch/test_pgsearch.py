"""Unit tests for the query parser and SQL compilation (no database needed).

Run with: python -m unittest test_pgsearch
"""

import unittest

from pgsearch import And, BadQuery, Leaf, Or, Unsupported, build_search_sql, parse_query


def leaves(node):
    if isinstance(node, Leaf):
        return [(node.field, node.text, node.phrase, node.prefix)]
    return [leaf for item in node.items for leaf in leaves(item)]


class ParserTest(unittest.TestCase):
    def test_droppedneedle_release(self):
        node = parse_query('release:"Discovery" AND artist:"Daft Punk"')
        self.assertIsInstance(node, And)
        self.assertEqual(leaves(node), [
            ('release', 'Discovery', True, False),
            ('artist', 'Daft Punk', True, False),
        ])

    def test_droppedneedle_artist_with_boosts_and_bare_text(self):
        node = parse_query('artist:"daft punk"^3 OR artistaccent:"daft punk"^3 '
                           'OR alias:"daft punk"^2 OR daft punk')
        self.assertIsInstance(node, Or)
        self.assertEqual(leaves(node)[-1], (None, 'daft punk', False, False))
        self.assertEqual(len(node.items), 4)

    def test_droppedneedle_release_group(self):
        node = parse_query('(releasegroup:"Homework" OR release:"Homework") AND artist:"Daft Punk"')
        self.assertIsInstance(node, And)
        self.assertIsInstance(node.items[0], Or)

    def test_droppedneedle_tag(self):
        node = parse_query('tag:"hip hop"^3 OR tag:"hip-hop"^2')
        self.assertEqual(leaves(node), [('tag', 'hip hop', True, False), ('tag', 'hip-hop', True, False)])

    def test_soulsync_grouped_field(self):
        node = parse_query(r'Around the World AND artist:(Daft Punk \(FR\))')
        self.assertEqual(leaves(node), [
            (None, 'Around the World', False, False),
            ('artist', 'Daft Punk (FR)', False, False),
        ])

    def test_soulsync_arid(self):
        node = parse_query('arid:056e4f3e-d505-4dad-8ec1-d04f521cbb56 AND recording:"One More Time"')
        self.assertEqual(leaves(node)[0], ('arid', '056e4f3e-d505-4dad-8ec1-d04f521cbb56', False, False))

    def test_escaped_characters(self):
        node = parse_query(r'release:"AC\/DC \"Live\""')
        self.assertEqual(leaves(node), [('release', 'AC/DC "Live"', True, False)])

    def test_words_with_punctuation(self):
        self.assertEqual(leaves(parse_query('Jay-Z Wham!')), [(None, 'Jay-Z Wham!', False, False)])

    def test_prefix(self):
        self.assertEqual(leaves(parse_query('artist:daf*')), [('artist', 'daf', False, True)])

    def test_negation(self):
        node = parse_query('artist:"Daft Punk" -tag:live')
        self.assertIsInstance(node, Or)
        self.assertEqual(len(node.exclude), 1)

    def test_errors(self):
        with self.assertRaises(BadQuery):
            parse_query('release:"unterminated')
        with self.assertRaises(Unsupported):
            parse_query('date:[2000 TO 2010]')
        with self.assertRaises(Unsupported):
            parse_query('-tag:live')


class CompileTest(unittest.TestCase):
    def test_placeholders_match_params(self):
        for entity, query in [
            ('artist', 'artist:"x"^3 OR alias:"x"^2 OR x'),
            ('release', '(release:"x" AND artist:"y") OR barcode:"123"'),
            ('release-group', '(releasegroup:"x" OR release:"x") AND artist:"y"'),
            ('release-group', 'tag:"rock"^3 OR tag:"rock-n-roll"^2'),
            ('recording', 'arid:056e4f3e-d505-4dad-8ec1-d04f521cbb56 AND recording:"x"'),
            ('recording', 'isrc:GBAYE0601498'),
            ('label', 'label:"x"'),
            ('series', 'x'),
        ]:
            for trgm in (True, False):
                sql, params = build_search_sql(entity, query, trgm)
                self.assertEqual(sql.count('%s'), len(params) + 2, (entity, query))

    def test_unknown_field(self):
        with self.assertRaises(Unsupported):
            build_search_sql('artist', 'ipi:123', True)


if __name__ == '__main__':
    unittest.main()
