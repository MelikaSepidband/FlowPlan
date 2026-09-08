# CLAUDE.md — Open Library

## Project Overview

Open Library (openlibrary.org) is the Internet Archive's open, editable catalog of books: one web page for every book ever published, with lending and reading integration with archive.org. The web application is Python 3.12 built on Infogami, a wiki framework that sits on the web.py micro framework and the Infobase document database (PostgreSQL). Search is backed by Solr, book covers by the coverstore service, and the front end mixes Templetor server-side templates with Vue components and JavaScript bundled by webpack. Development normally runs under Docker Compose. The code is AGPL licensed.

## Repository Structure

- `openlibrary/` — the main package.
  - `core/` — domain logic shared by everything: `models.py` (`Thing` subclasses `Edition`, `Work`, `Author`, `User`, … registered with `client.register_thing_class`), `db.py`, `cache.py` (memoize/cache decorators), `helpers.py` (functions exposed to templates), `ia.py` (Internet Archive APIs), `lending.py`, `bookshelves.py`, `imports.py`, `lists/`, `processors/`, `vendors.py`, `wikidata.py`, `edits.py`, `ratings.py`, `observations.py`.
  - `plugins/` — web.py applications. Each plugin's `code.py` registers URL handlers as classes deriving from `delegate.page` with `GET`/`POST` methods. `upstream/` (site pages, `addbook.py`, `merge_authors.py`, `borrow.py`, `account.py`, `utils.py` helpers), `openlibrary/` (core site handling, `api.py`, `lists.py`, `authors.py`, `processors.py`), `worksearch/` (search UI and Solr query building), `importapi/`, `admin/`, `books/`, `inside/` (full-text search), `recaptcha/`, `wikidata/`.
  - `views/` — view helpers; `templates/` — Templetor templates (files start with `$def with (...)`); `macros/` — templates callable from wikitext; `components/` — Vue single-file components built as web components.
  - `catalog/` — MARC parsing and import matching; `solr/` — indexing and the updater; `coverstore/`; `accounts/`; `i18n/` (Babel `.po`/`.pot` messages); `utils/`; `mocks/` (`mock_infobase.py`, `mock_memcache.py`, `mock_ia.py`); `conftest.py` (repo-wide pytest fixtures).
  - `tests/` — `accounts/`, `catalog/`, `core/`, `data/`, `solr/`. Other packages keep tests next to the code, for example `openlibrary/plugins/upstream/tests/`, `openlibrary/plugins/worksearch/tests/`, `openlibrary/utils/tests/`, `openlibrary/coverstore/tests/`.
- `infogami/` — the vendored framework (git submodule). Treat it as an external dependency; do not modify it.
- `scripts/` — service entry points and maintenance scripts (`solr_builder/`, `i18n-messages`, `copydocs.py`, …).
- `static/` — CSS (LESS), JS, images; built assets land in `static/build` (never committed). `conf/`, `config/` — service configuration. `docker/` and `compose*.yaml` — containers. `vendor/` — third-party code. Top-level `tests/` — Docker Compose smoke checks only.
- Tooling: `pyproject.toml` (ruff, black, mypy, pytest with `asyncio_mode = strict`, codespell), `.pre-commit-config.yaml`, `requirements.txt`, `requirements_test.txt`, `package.json` (webpack, eslint, stylelint, vue-cli), `Makefile` (`css`, `js`, `components`, `i18n`, `git` targets).

## Development Workflow

### Setup

- Full environment: `docker compose up` (see `docker/`); `make git` initialises the `infogami` submodule.
- Python dependencies: `requirements.txt`; tests and tooling: `requirements_test.txt` (pytest 7.4, pytest-asyncio, pytest-cov, ruff 0.4.1, mypy 1.10, safety).
- Front end: `npm install`, then `make css js components i18n` or `npm run build-assets`.

### Common Commands

```bash
# one test file, from the repo root
python -m pytest openlibrary/tests/core/test_helpers.py -x -q
# tests that live next to a plugin
python -m pytest openlibrary/plugins/upstream/tests/test_merge_authors.py -q
# one test
python -m pytest openlibrary/tests/core/test_helpers.py::test_datestr -q
# the full Python suite inside Docker
docker compose run --rm home make test
# lint / format / types
ruff check .
ruff format .
mypy openlibrary
pre-commit run --all-files
# translations
python ./scripts/i18n-messages extract
python ./scripts/i18n-messages compile
```

Run pytest from the repository root so that both `openlibrary.*` and `infogami.*` resolve.

## Testing

- Repo-wide fixtures in `openlibrary/conftest.py`: `no_requests` (autouse; any `requests` call fails, so tests are offline by construction), `no_sleep`, `monkeytime`, `wildcard`, `render_template`. Mocks in `openlibrary/mocks/` stand in for Infobase (`mock_site`), memcache and Internet Archive APIs.
- Unit tests must run without PostgreSQL, Solr, Docker or the network. Use the mocks and `monkeypatch`; do not add tests that need a live site.
- Async code is tested with pytest-asyncio in strict mode: coroutine tests need `@pytest.mark.asyncio`.
- When fixing a bug, add a regression test in the `tests/` directory closest to the code you changed, following the naming of neighbouring tests.

## Architecture Notes: request lifecycle

A request URL is matched by `infogami.utils.delegate` against the `path` regex of the `delegate.page` subclasses registered by the plugins. The handler reads parameters with `web.input()`, talks to the document store through `web.ctx.site` (`site.get(key)`, `site.things(query)`, `site.save(doc)`), works with model objects from `openlibrary/core/models.py`, and renders a Templetor template with `render_template('name', ...)`; redirects are `raise web.seeother(url)`. Template helpers come from `openlibrary/core/helpers.py` and `openlibrary/plugins/upstream/utils.py`. Book data flows in through `catalog/` and `importapi/`, out to search through `solr/`.

## Coding Standards and Conventions

- Python 3.12 with type hints where practical; ruff enforces style (see the `ignore` list in `pyproject.toml`), black formatting with single quotes preserved (`skip-string-normalization`), codespell for prose.
- Web handlers: subclass `delegate.page`, set `path`, implement `GET`/`POST`; never build responses by string concatenation, use templates.
- Data model: `Thing` subclasses in `core/models.py`; go through the site API rather than raw SQL; a new field must be handled in the model, the templates that display it and, if searchable, the Solr schema/updater.
- Templates: keep logic thin, wrap user-visible strings in `_()` for i18n, escape by default (`$:` only for trusted HTML), and put non-trivial JavaScript in `static/js` or a Vue component rather than inline.
- Caching: `openlibrary/core/cache.py` decorators; invalidate or bump keys when the shape of cached data changes.
- Do not edit `infogami/` or `vendor/`; do not commit `static/build`; do not add dependencies without updating `requirements*.txt`.
- Keep changes minimal and local to the feature; match neighbouring code style; avoid drive-by refactors and unrelated formatting.

## Commits and Pull Requests

- Pre-commit hooks (ruff, trailing-whitespace, end-of-file, YAML/TOML checks, requirements ordering, auto-walrus) run on commit; `pre-commit run --all-files` must be clean.
- Pull requests are rebased on `master`, cover one topic, include tests for the change and describe the user-facing effect; follow the "How we use git" wiki.

## Things to Avoid

- Tests or code that reach the network, a real database or Solr.
- Editing generated translation files by hand (`openlibrary/i18n/**/messages.po` come from the extract/compile scripts).
- Touching `infogami/` (submodule) to work around a site-level problem; fix it in `openlibrary/`.
- Creating new top-level modules when the change belongs in an existing plugin or core module.
