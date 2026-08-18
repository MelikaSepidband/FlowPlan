# sampleproj

A small but realistic Python package used as the shared fixture repository
for repr-study module tests.

## Layout

- `sampleproj/app.py` -- entry point; `main()` runs the pipeline and connects
  to the database.
- `sampleproj/core/engine.py` -- `run_pipeline()`, `transform()`,
  `validate_input()`.
- `sampleproj/core/utils.py` -- `normalize()`, `log_message()`,
  `helper_unused()`.
- `sampleproj/services/db.py` -- `connect()`.
- `sampleproj/models.py` -- `User` with `save()`.

See `docs/usage.md` for usage and `config.yaml` for configuration.
