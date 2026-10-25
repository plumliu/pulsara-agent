"""Capture the installed verifier call sites without opening PostgreSQL."""
import inspect
from pathlib import Path

from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory
from pulsara_agent.storage.migrations.verifier import PostgresSchemaVerifier
from pulsara_agent.storage.migrations.runner import (
    _verify_vector_capability, _verify_search_capability,
    _verify_product_relations, _verify_runtime_grants,
)

objects = [PostgresRuntimeConnectionFactory.verify_deep, PostgresSchemaVerifier._verify,
           _verify_vector_capability, _verify_search_capability,
           _verify_product_relations, _verify_runtime_grants]
text = "\n\n".join(f"{inspect.getfile(obj)}:{inspect.getsourcelines(obj)[1]}\n{inspect.getsource(obj)}" for obj in objects)
target = Path(__file__).resolve().parent / "installed-verifier-call-sites.txt"
assert not target.exists()
target.write_text(text, encoding="utf-8")
print("Installed verifier call sites captured; no PostgreSQL connection opened.")
