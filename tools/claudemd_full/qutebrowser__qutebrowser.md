# CLAUDE.md — qutebrowser

## Project Overview

qutebrowser is a keyboard-driven, vim-like web browser written in Python on top of Qt through PyQt5. Pages are rendered by QtWebEngine (Chromium) with a legacy QtWebKit backend still supported. It has a minimal GUI, a modal keybinding system, a `:command` system, hint mode for following links, a completion widget and a configuration system driven by a YAML settings definition. Small JavaScript helpers (`qutebrowser/javascript/`) are injected into pages. The project is GPLv3 licensed and maintained by a small team, so pull requests should be small, tested and follow the house style closely.

## Repository Structure

- `qutebrowser/` — the application package (each package has a module docstring explaining its role).
  - `app.py` (startup and initialisation), `qutebrowser.py` (entry point), `browser/` (tab and page handling: `browsertab.py` defines the backend-independent tab API; `webengine/` and `webkit/` implement it; plus `downloads.py`, `hints.py`, `history.py`, `navigate.py`, `urlmarks.py`, `qutescheme.py`, `commands.py`), `mainwindow/` (`mainwindow.py`, `tabbedbrowser.py`, `tabwidget.py`, `prompt.py`, `messageview.py`, `statusbar/`), `commands/` (command parsing and running, `cmdexc.py` exceptions, `command.py`), `api/` (the public extension API: `cmdutils`, `config`, `hook`, `message`, `apitypes`), `components/` (features built on the API: adblock, caret, scroll, zoom, …), `completion/` (widget and completion models), `config/` (`config.py` core, `configdata.yml` — every setting with type, default and description, `configtypes.py` — the setting types, `configfiles.py` — `config.py`/`autoconfig.yml` handling, `configcommands.py`, `configinit.py`, `configutils.py`), `keyinput/` (modes, `basekeyparser.py`, `modeparsers.py`, `keyutils.py`), `misc/` (editor, sessions, ipc, crashdialog, utilcmds, backendproblem, elf, …), `utils/` (`urlutils`, `log`, `message`, `objreg`, `standarddir`, `qtutils`, `usertypes`, `utils`, `version`, `debug`, `javascript`), `extensions/`, `html/`, `javascript/`.
- `tests/` — `conftest.py`; `unit/` mirrors the package (code in `qutebrowser/utils/urlutils.py` is tested by `tests/unit/utils/test_urlutils.py`); `end2end/` (BDD `.feature` files run with pytest-bdd against a real qutebrowser subprocess); `helpers/` (`fixtures.py`, `stubs.py`, `logfail.py`, `testutils.py`); `manual/`.
- `doc/` — asciidoc documentation: `doc/help/commands.asciidoc` and `doc/help/settings.asciidoc` are generated from the source, `doc/changelog.asciidoc`, `doc/contributing.asciidoc` (style guide and workflow), `doc/faq.asciidoc`.
- `scripts/dev/` — developer tooling: `misc_checks.py`, `run_vulture.py`, `run_pylint_on_tests.py`, `src2asciidoc.py` (regenerates `doc/help/` from `cmdutils` and `configdata.yml`), `check_coverage.py`, `recompile_requirements.py`, `pylint_checkers/`.
- `misc/requirements/` — pinned requirement files per tox environment (`requirements-tests.txt`, `requirements-pyqt*.txt`, `requirements-flake8.txt`, `requirements-pylint.txt`, `requirements-mypy.txt`, …). Config files: `tox.ini`, `pytest.ini`, `.flake8`, `.pylintrc`, `.mypy.ini`, `.pydocstylerc`, `.coveragerc`, `setup.py`, `MANIFEST.in`.

## Development Workflow

### Setup

- `pip install -r requirements.txt` plus PyQt5 and PyQtWebEngine (`misc/requirements/requirements-pyqt.txt`); test tooling comes from `misc/requirements/requirements-tests.txt` (pytest, pytest-qt, pytest-bdd, pytest-mock, pytest-benchmark, pytest-instafail, pytest-rerunfailures, pytest-xvfb, hypothesis, …).
- Run from the checkout with `python3 -m qutebrowser` (a display is required).

### Common Commands

```bash
# unit tests for one file
python -m pytest tests/unit/utils/test_urlutils.py -x -q
# one test
python -m pytest tests/unit/utils/test_urlutils.py -k test_is_url -q
# all unit tests (GUI-marked tests need a display: xvfb-run or pytest-xvfb)
python -m pytest tests/unit -q
# full tox matrix pieces
tox -e py38-pyqt515      # tests with a pinned PyQt
tox -e flake8
tox -e pylint
tox -e mypy
tox -e vulture
tox -e misc              # scripts/dev/misc_checks.py: git, vcs, spelling, userscripts
# regenerate generated docs after changing a command or a setting
python scripts/dev/src2asciidoc.py
```

`pytest.ini` sets `--strict-markers --strict-config` and lists required plugins; if a plugin is missing, pytest refuses to start, which is an environment problem rather than a test failure. Install `misc/requirements/requirements-tests.txt` first.

## Testing

- Unit tests use pytest with pytest-qt (`qtbot`), pytest-mock (`mocker`) and the shared fixtures in `tests/helpers/fixtures.py` (`config_stub`, `key_config_stub`, `fake_web_tab`, `tabbed_browser_stubs`, `message_mock`, `web_tab`, `qapp`, `data_tmpdir`, …). Stubs in `tests/helpers/stubs.py` (`FakeWebFrame`, `FakeUrl`, `FakeSignal`, …) avoid real Qt objects where possible.
- Markers defined in `pytest.ini`: `gui`, `posix`/`linux`/`windows`/`mac`, `integration`, `end2end`, `qtwebengine_skip`/`qtwebkit_skip`/`qtwebengine_todo`, `no_xvfb`, `ci`/`no_ci`. Mark tests that need a backend, a platform or a display.
- End-to-end tests (`tests/end2end/`) start the real browser and are slow and environment-heavy; do not run them for ordinary changes. Hypothesis property tests appear in several `utils` test modules.
- Prefer targeting the single non-GUI test file that covers your change, and add a regression test next to the existing tests for that module.

## Coding Standards and Conventions

- PEP 8 plus the Google Python style guide with the qutebrowser additions documented in `doc/contributing.asciidoc`: PEP 257 docstrings with Google-style sections; class docstrings carry `Attributes:`, `Class attributes:` and `Signals:` sections; no `Raise:` section; methods overriding Qt methods keep Qt's camelCase, everything else (including slots) is snake_case.
- Qt: at this revision the code imports PyQt5 directly (`from PyQt5.QtCore import pyqtSlot, QUrl`); use `qutebrowser.utils.qtutils` for version checks and Qt quirks; decorate slots with `@pyqtSlot`; never block the event loop.
- Commands are defined with `@cmdutils.register(...)` (`qutebrowser.api.cmdutils`); the docstring is split by `//` into the user-visible description and internal notes; argument types come from annotations, extra behaviour from `@cmdutils.argument`. Command names and docstrings feed the generated `doc/help/commands.asciidoc`.
- Settings are declared in `qutebrowser/config/configdata.yml` (name, type, default, desc, backend restrictions) with their types in `configtypes.py`; read them with `config.val.section.option`, change them through `config.instance`; a behaviour change in a setting needs the matching `configdata.yml` edit and regenerated docs.
- Shared objects are reached through `qutebrowser.utils.objreg` (`objreg.get('tabbed-browser', scope='window', window=win_id)`); user-facing messages go through `qutebrowser.utils.message` (`message.info`/`warning`/`error`); logging through `qutebrowser.utils.log` (`log.webview`, `log.config`, `log.misc`, …); enums live in `utils.usertypes`.
- Keep the QtWebEngine/QtWebKit split intact: backend-specific code goes under `browser/webengine/` or `browser/webkit/` behind the shared interface in `browser/browsertab.py`.
- flake8, pylint, mypy and vulture must stay clean (tox environments). Do not leave unused code; whitelist in `scripts/dev/run_vulture.py` only when the usage is dynamic.
- Line length and naming follow `.flake8`/`.pylintrc`; keep functions small, prefer early returns, and match the file you are editing.

## Changelog and Pull Requests

- Add an entry to `doc/changelog.asciidoc` under the topmost unreleased version, in the matching `Added`/`Changed`/`Fixed`/`Removed` section, phrased from the user's point of view.
- Pull requests target `master`, stay small, include unit tests, pass flake8/pylint locally, and do not touch unrelated files; commit messages explain the why.

## Things to Avoid

- Editing the generated files `doc/help/commands.asciidoc` and `doc/help/settings.asciidoc` by hand; change the source and regenerate.
- Importing QtWebEngine or QtWebKit modules from backend-independent code.
- Running the end-to-end suite or GUI tests without a display for a change that a unit test covers.
- Reformatting files or changing docstring style outside the lines you need to touch.
