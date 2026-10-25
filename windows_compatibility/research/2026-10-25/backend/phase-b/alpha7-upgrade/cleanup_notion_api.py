"""Clear only the explicitly authorized GUI test owner through verified HTTPS.

The certificate argument must come from this runtime's original ready channel,
not from an unauthenticated TLS download. No alternative OAuth owner is created.
"""

import argparse
from http.client import HTTPSConnection
import json
from pathlib import Path
import ssl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", type=Path, required=True)
    args = parser.parse_args()
    origin = "https://127.0.0.1:40363"
    server_id = "notion-oauth-alpha7-smoke"
    output = Path(__file__).resolve().parent / "alpha7-notion-api-cleanup.json"
    assert not output.exists(), "preserve original cleanup responses"
    certificate = args.certificate.read_text(encoding="ascii")
    expected_der = ssl.PEM_cert_to_DER_cert(certificate)
    context = ssl.create_default_context(cadata=certificate)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    evidence = {"origin": origin, "server_id": server_id,
                "certificate_source": str(args.certificate),
                "verified_requests": 0, "responses": []}

    def request(method, path, body=None):
        connection = HTTPSConnection("127.0.0.1", 40363, context=context, timeout=30)
        try:
            connection.connect()
            assert connection.sock.getpeercert(binary_form=True) == expected_der
            evidence["verified_requests"] += 1
            data = None if body is None else json.dumps(body).encode("utf-8")
            connection.request(method, path, body=data,
                               headers={"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read())
            assert 200 <= response.status < 300, f"HTTP {response.status}"
            return response.status, value
        finally:
            connection.close()

    def current():
        _, capabilities = request("GET", "/api/capabilities")
        matches = [item for item in capabilities["mcp"]["servers"] if item["id"] == server_id]
        assert len(matches) == 1
        item = matches[0]
        assert item["enabled"] is False and item["config"]["auth"]["type"] == "oauth"
        assert item["config"]["transport"]["endpoint"] == "https://mcp.notion.com/mcp"
        return item

    path = "/api/capabilities/mcp/" + server_id
    try:
        current()
        status, value = request("GET", path + "/authorization")
        evidence["responses"].append({"method": "GET", "action": "authorization", "http": status, "result": value})
        if value["state"] in {"connecting", "awaiting_user"}:
            status, value = request("POST", path + "/authorization/cancel", {})
            evidence["responses"].append({"method": "POST", "action": "cancel", "http": status, "result": value})
        status, value = request("DELETE", path + "/authorization", {})
        evidence["responses"].append({"method": "DELETE", "action": "logout", "http": status, "result": value})
        assert value["state"] == "idle"
        identity = current()["current_identity"]
        status, value = request("DELETE", path, {"expected_identity": identity})
        # Do not export capability config objects; record the original settlement.
        operation = value["operation"]
        evidence["responses"].append({"method": "DELETE", "action": "remove", "http": status,
                                      "operation": operation, "adoption": value["adoption"]})
        assert operation["success"] and operation["status"] == "REMOVED"
        assert operation["cleanup_attention"] is False
        _, capabilities = request("GET", "/api/capabilities")
        evidence["config_absent_in_current_api"] = all(item["id"] != server_id for item in capabilities["mcp"]["servers"])
        assert evidence["config_absent_in_current_api"]
        evidence["remote_revocation_requested"] = False
        evidence["passed"] = True
    finally:
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=True))


if __name__ == "__main__":
    main()
