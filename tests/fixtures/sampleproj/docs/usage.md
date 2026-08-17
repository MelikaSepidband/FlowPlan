# Using sampleproj

Run the pipeline from Python:

```python
from sampleproj.app import main

result = main()
```

`main()` validates and normalizes the input records via
`sampleproj.core.engine.run_pipeline`, then opens a database connection via
`sampleproj.services.db.connect`. Persist a user with:

```python
from sampleproj.models import User

User("ada").save()
```
