# CLAUDE.md — ansible-core

## Project Overview

ansible-core is the engine behind Ansible: a Python (3.10+) IT automation system that pushes configuration, deploys applications and orchestrates multi-node workflows over SSH without installing agents on managed nodes. This repository contains the controller-side engine, the command-line tools (`ansible`, `ansible-playbook`, `ansible-galaxy`, `ansible-doc`, `ansible-vault`, `ansible-test`, …), the core modules and plugins that ship with ansible-core, and the `ansible-test` tooling. Content collections (community modules and roles) live in separate repositories and are not part of this checkout. The project is licensed GPLv3+ and follows a deprecation cycle for any user-visible behaviour change.

## Repository Structure

- `lib/ansible/` — the `ansible` package, the only importable code in the repo. Key subpackages:
  - `cli/` — entry points for every CLI tool (`playbook.py`, `galaxy.py`, `adhoc.py`, `doc.py`, `vault.py`, …).
  - `executor/` — how tasks run: `playbook_executor.py`, `task_queue_manager.py`, `play_iterator.py`, `task_executor.py`, `module_common.py` (module packaging for transfer), `process/worker.py`.
  - `playbook/` — the playbook object model: `Play`, `Block`, `Task`, `Role`, conditionals, loops, attribute definitions and `helpers.py`.
  - `parsing/` — YAML loading (`dataloader.py`), vault, `mod_args.py` (module argument parsing), splitter and quoting helpers.
  - `plugins/` — plugin types: `action/`, `become/`, `cache/`, `callback/`, `connection/`, `filter/`, `inventory/`, `lookup/`, `shell/`, `strategy/`, `test/`, `vars/`. Each package's `__init__.py` defines the base class; `plugins/loader.py` discovers and loads plugins.
  - `modules/` — the core modules (copy, file, command, shell, yum/dnf, git, …).
  - `module_utils/` — code shipped to managed nodes and imported by modules; `basic.py` provides `AnsibleModule`; `common/` holds argument-spec validation, text converters, collections helpers.
  - `template/` — `Templar` and the Jinja2 integration; `vars/` — `VariableManager` and variable precedence; `inventory/` — inventory data and host/group objects; `galaxy/` — collection/role installation; `config/` — the settings manager and `base.yml`, the source of truth for every configuration option; `utils/` — `display.py`, unsafe proxies, path helpers, collection loader; `errors/` — the `AnsibleError` family; `constants.py`, `context.py`, `release.py` (version string).
- `bin/` — thin wrappers for the CLI tools.
- `test/units/` — pytest unit tests mirroring `lib/ansible/` (code in `lib/ansible/playbook/task.py` is tested by `test/units/playbook/test_task.py`; module tests live in `test/units/modules/`). `test/units/mock/` holds shared mocks (`loader.py` → `DictDataLoader`, `vault_helper.py`, `path.py`); `test/units/modules/utils.py` provides `set_module_args`, `exit_json`/`fail_json` replacements, `AnsibleExitJson`/`AnsibleFailJson` and `ModuleTestCase`.
- `test/integration/` — integration targets, run only through `ansible-test`. `test/sanity/` — sanity test config and ignore files. `test/lib/ansible_test/` — the `ansible-test` implementation. `test/support/` — support content for tests.
- `changelogs/` — `changelog.yaml`, `config.yaml` (section names) and `fragments/` (one YAML file per change).
- `hacking/` — `env-setup` (run from a checkout), `test-module.py` (run a module locally), `return_skeleton_generator.py`.
- `packaging/`, `licenses/`, `setup.cfg`, `pyproject.toml`, `requirements.txt`, `MANIFEST.in`.

## Development Workflow

### Environment

- Running from the checkout: `source ./hacking/env-setup` puts `lib/` on `PYTHONPATH` and `bin/` on `PATH`. Runtime dependencies: `pip install -r requirements.txt` (jinja2, PyYAML, cryptography, packaging, resolvelib). Unit-test extras: `pip install -r test/units/requirements.txt` (bcrypt, passlib, pexpect, pywinrm).
- The package is imported from `lib/`, which is not on `sys.path` by default. Any direct Python invocation of tests or tools needs `PYTHONPATH=lib` (or the env-setup script).

### Common Commands

```bash
# one unit test file
PYTHONPATH=lib python -m pytest test/units/utils/test_helpers.py -x -q
# one test
PYTHONPATH=lib python -m pytest test/units/playbook/test_task.py::TestTask::test_load_task_simple -q
# one unit area
PYTHONPATH=lib python -m pytest test/units/parsing -q
# official runners (need ansible-test's own environment / containers)
ansible-test units --python 3.12 utils
ansible-test sanity --python 3.12 lib/ansible/playbook/task.py
ansible-test integration ping
# exercise a module locally
./hacking/test-module.py -m lib/ansible/modules/ping.py -a "data=hello"
# quick syntax check
python -m py_compile lib/ansible/playbook/task.py
```

Do not run `test/integration/` or `test/sanity/` with plain pytest; they need the `ansible-test` harness.

## Testing

- Unit tests are pytest. Older areas use `unittest.TestCase` classes, newer tests use plain functions with fixtures; either is acceptable, match the file you are editing.
- Mock heavily with `unittest.mock`. `DictDataLoader` (`test/units/mock/loader.py`) replaces file access for playbook and vars tests; `test/units/mock/vault_helper.py` builds vault secrets.
- Module tests: patch `AnsibleModule.exit_json`/`fail_json` with the helpers in `test/units/modules/utils.py`, call `set_module_args({...})`, run the module's `main()`, and assert on the raised `AnsibleExitJson`/`AnsibleFailJson`.
- Tests must not touch the network, must not need root, and must write only under `tmp_path`.
- Sanity tests (run in CI via `ansible-test sanity`) enforce pep8, pylint, compile/import checks, module documentation and argument-spec validation, and deprecation-version checks. Write code that passes them: keep `DOCUMENTATION` in sync with `argument_spec`, and use the proper deprecation helpers.

## Architecture Notes: how a play runs

`ansible-playbook` (`cli/playbook.py`) builds a `PlaybookExecutor`, which loads the playbook through the `DataLoader` and `VariableManager`, then hands each `Play` to the `TaskQueueManager`. A strategy plugin (`linear` by default) iterates hosts and tasks via `PlayIterator`, dispatching work to `WorkerProcess`es that run `TaskExecutor`. For each task the executor templates arguments with `Templar`, picks the action plugin (which usually wraps a module), opens a connection plugin to the host, and the action plugin transfers and executes the module built by `module_common`. Results flow back through callback plugins to the display. Knowing which layer owns a behaviour (parsing, playbook object, executor, action plugin, module) usually tells you which file to change.

## Coding Standards and Conventions

- Python 3.10+ only on the controller; no `six` or Python 2 shims. `module_utils` code executes on managed nodes: keep it free of controller-only imports, dependency-free, and importable on the minimum supported target Python versions.
- Modules: `AnsibleModule(argument_spec=..., supports_check_mode=...)`, `DOCUMENTATION`/`EXAMPLES`/`RETURN` docstring blocks, results via `module.exit_json(...)`/`module.fail_json(msg=...)`; honour `check_mode` and `diff`.
- Text handling: use `to_text`/`to_bytes`/`to_native` from `ansible.module_utils.common.text.converters`; never assume `str` versus `bytes`.
- Controller messaging goes through `Display` (`from ansible.utils.display import Display; display = Display()`): `display.warning`, `display.vvv`, `display.deprecated(msg, version=...)`. In modules use `module.warn` and `module.deprecate`.
- Raise `AnsibleError` subclasses (`AnsibleParserError`, `AnsibleUndefinedVariable`, `AnsibleActionFail`, …) from `lib/ansible/errors`, with `obj=` where the error relates to YAML data so the user gets a file/line pointer.
- Plugins subclass their type's base class and declare options in `DOCUMENTATION`; read them with `self.get_option(...)` after `set_options` rather than reading config directly.
- Variable precedence is implemented in `vars/manager.py`; templating in `template/`; wrap untrusted strings with `wrap_var` so they stay `AnsibleUnsafe`.
- Style: 4-space indent, pep8 with a 160-column limit, no unrelated reformatting, no new top-level dependencies. Keep functions small and error messages actionable.
- Backwards compatibility matters: changing public behaviour requires a deprecation cycle, not a silent change.

## Changelog, Commits and Pull Requests

- Every user-visible change needs a changelog fragment: a new YAML file in `changelogs/fragments/` named after the change (for example `changelogs/fragments/12345-fix-template-lookup.yml`) using one section from `changelogs/config.yaml`:

```yaml
bugfixes:
  - template - fix lookup of undefined variables in nested loops (https://github.com/ansible/ansible/issues/12345).
```

- Pull requests target the `devel` branch, stay focused on one change, and include unit tests for bug fixes. Keep the diff minimal, do not create new files unless required, and do not modify generated or vendored code (`lib/ansible/_vendor/`).

## Things to Avoid

- Importing controller code (`ansible.utils`, `ansible.plugins`, …) from `module_utils` or modules.
- Running tests without `PYTHONPATH=lib`; the import errors that follow are environmental, not bugs.
- Editing `test/sanity/ignore-*.txt` to silence a sanity failure instead of fixing the code.
- Reformatting files you did not need to change.
