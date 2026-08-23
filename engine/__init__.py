"""datamap engine — the domain core.

Agents, LangGraph orchestration, the DuckDB staging layer, gating model, and
configuration. The engine has NO knowledge of HTTP, the API, or any UI.

Dependency rule (enforced by convention): delivery layers (`api/`, `cli/`)
import from `engine`; `engine` imports from neither. If the engine ever needs
to reach into `api` or `cli`, the layering has leaked.
"""
