"""CLI delivery layer.

Command-line entrypoints that drive the engine — the full pipeline (`cli.run`)
and per-agent runners. Imports from `engine`; never the reverse.

    python -m cli.run --source data/EFAS0042.csv --target-dict data/target_dictionary.json
"""
