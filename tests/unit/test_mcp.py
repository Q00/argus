import json
from pathlib import Path

from argus import mcp_server


class TestJsonRpcHandling:
    def test_initialize_reports_server_info(self):
        response = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert response is not None
        assert response["result"]["serverInfo"]["name"] == "argus-review-checklist"

    def test_tools_list_includes_attrition_tool(self):
        response = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert response is not None
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert "review_attrition" in names
        assert "review_verify_completion" in names

    def test_health_tool_needs_no_active_run(self):
        response = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "review_health", "arguments": {}},
            }
        )
        assert response is not None
        assert response["result"]["structuredContent"]["ok"] is True

    def test_notifications_produce_no_response(self):
        assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_status_without_active_run_errors_via_serve(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import io

        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "review_status", "arguments": {}},
            }
        )
        stdout = io.BytesIO()
        mcp_server.serve(stdin=io.StringIO(request + "\n"), stdout=stdout)
        response = json.loads(stdout.getvalue().decode("utf-8"))
        assert "error" in response
        assert "no active argus review" in response["error"]["message"]
