"""Read saved settings through the native owner; emit only non-secret metadata."""
from dataclasses import asdict
import json
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.llm.model_connections import reasoning_selection_to_dict
from pulsara_agent.llm.model_target import default_reasoning_selection

home = require_pulsara_home()
saved = LocalSettingsStore(home / "local-settings.yaml").read()
rows = []
for item in saved.model_connections:
    row = {"connection_id": item.id.value, "model": item.target.model_id,
           "wire_api": item.target.wire_api.value, "route_id": item.target.route_id,
           "reasoning_wire_profile": item.reasoning_wire_profile.value,
           "credential_available": not item.requires_api_key or saved.model_api_key(item.id) is not None}
    if item.user_declared is not None:
        row["reasoning_contract"] = asdict(item.user_declared.reasoning)
        row["runner_default_reasoning_selection"] = reasoning_selection_to_dict(default_reasoning_selection(item.user_declared.reasoning))
        row["tool_call"] = item.user_declared.tool_call
    else:
        row["runner_default_reasoning_selection"] = "resolve from original runtime/catalog"
    rows.append(row)
print(json.dumps({"saved_home": str(home), "connections": rows}, ensure_ascii=False, indent=2))
