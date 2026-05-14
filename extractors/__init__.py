"""Code-extraction plugins.

The extractor framework is regex-driven and DB-backed: patterns live in the
`extractor_patterns` table (see db/init/01_seed.sql for builtin ones).
Hot-loaded Python plugins for more complex logic land in this package later.
"""
