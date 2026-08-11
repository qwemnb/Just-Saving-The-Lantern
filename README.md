# Helios Room

Helios Room is a local, persistent multi-participant chat room. This first
milestone contains only the project skeleton and SQLite initialization from the
unchanged v1.2 schema. API calls, chat behavior, memory retrieval, and the web
interface are intentionally deferred.

## Initialize the database

Run from the project root:

```powershell
python -m app.main init-db
```

This creates `data/helios.db`, installs schema v1.2 when needed, and ensures the
following records exist:

- room `main` / `The Room`
- human participant `peter` / `Peter`
- AI participant `helios` / `Helios`
- active room membership for both participants
- one minimal OpenAI participant config for Helios labeled `initial`

The initial config intentionally leaves the model and system instructions
unset until the API-integration milestone.

## Run tests

```powershell
python -m unittest discover -s tests -v
```

