# Blind faults · الأعطال السرية

**بالعربي باختصار:** هذا الملف لزميل في الفريق **ما شارك في كتابة قواعد نبض**. اكتب عطل أو أكثر في ملف `faults.json` داخل هذا المجلد، ولا تُطلع أحد على محتواه، بما فيهم Claude. نبض يشغّلها بدون ما يعرف وش هي، ونقيس هل اكتشفها وأصلحها.

## Why

Every fault in Nabd's catalog was written by the same people who wrote its rules, so scoring well on them proves less than it seems. A blind fault is the honest test: someone else breaks the store, in a way the authors have not seen, and Nabd has to cope.

## How to write one

1. Copy `faults.example.json` to `faults.json` in this folder. Edit it and don't show it to anyone.
2. Restart Nabd so it reads the file: `docker compose up -d nabd`.
3. Run it from the War Room ("Break the store", then "Multi-fault scenarios and held-out faults", then "Blind faults"), or run the whole blind set with `python eval/run_all.py --set blind`.

Each fault is built from generic steps, so no code is needed:

| step | what it does | fields |
|---|---|---|
| `sql` | runs SQL on the store's database as an admin | `sql`, optional `app` (the client name Postgres records) |
| `config` | deploys a config change, as CI would | `service`, `changes`, `author`, `message`, `minutes_ago` |
| `redis` | runs one Redis command | `command` (list) |
| `toxic` | adds a Toxiproxy toxic to a network link | `proxy` (`orders_db_primary`), `toxic` |
| `kill` | kills a container | `service` |
| `http` | calls any internal URL | `method`, `url`, `json`, `params` |
| `shoppers` | changes how many simulated shoppers there are | `count` |
| `sleep` | waits between steps (up to 30 s) | `seconds` |

`undo` lists the steps that clean the fault up. Every reset runs it, so make it safe to run twice: use `DROP ... IF EXISTS` and similar.

`truth` is what the scorer compares against:
- `node`: the component that is really broken, for example `orders`, `postgres` or `fulfillment`.
- `category`: one of `bad_config_deploy`, `process_crash`, `db_lock_contention`, `db_connection_exhaustion`, `network_degradation`, `cache_memory_exhaustion`, `cpu_saturation`, `memory_leak`, `dependency_failure`, `silent_data_loss`, `corrupted_data`, `poison_message`, `other`.
- `fixes`: the actions that fix it, such as `rollback_config:orders`. Leave it empty if nothing inside the store can fix it.
- `expect`: `recover`, or `escalate` when the right answer is to call a human.

Ideas of the kind that make a good blind test: something that loses or corrupts data while every request still succeeds; something slow that only shows up under load; something that looks like one service but is really another. The services, database tables and config keys are listed in the top-level README.
