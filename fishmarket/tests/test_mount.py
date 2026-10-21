"""The existing root resource and private discovery exception stay separate."""
import asyncio
import json
from pathlib import Path
import httpx2
from mcp.server.mcpserver import MCPServer
from boring_service_kit.http import ServiceConfig
from boring_service_kit.pond import wrap_app,attach_heartbeat
from boring_fishmarket.server import App
from boring_fishmarket.helper import TransferClient
from test_market import market,URL,ORIGIN  # noqa: F401  (pytest fixture)

class Watchdog:
    def pinged(self,source=""):pass
    async def run(self):await asyncio.Event().wait()

def test_prefix_metadata_sibling_mcp_and_discovery_privacy(market,tmp_path):  # noqa: F811
    async def run():
        original,store,key,*_=market
        config=original.config
        config.watchdog=Watchdog()
        fish=App(store,config)
        sibling_file=tmp_path/"sibling.json"
        sibling_file.write_text(json.dumps({"url":ORIGIN+"/mcp",
                                           "keys":{"file":str(tmp_path/"keys.json")}}))
        sibling_config=ServiceConfig(sibling_file)
        sibling_server=MCPServer("existing",**sibling_config.auth)
        attach_heartbeat(sibling_server,None)
        @sibling_server.tool()
        def marker()->dict:return {"existing":True}
        sibling=wrap_app(sibling_server,sibling_config.url)
        async def router(scope,receive,send):
            if scope.get("path")=="/mcp":
                return await sibling(scope,receive,send)
            return await fish(scope,receive,send)
        async with fish.web.router.lifespan_context(fish.web):
            async with sibling.authenticated.router.lifespan_context(sibling.authenticated):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=router),base_url=ORIGIN) as http:
                    async with TransferClient(ORIGIN+"/mcp",key,http) as client:
                        assert await client.call("marker",{})=={"existing":True}
                    async with TransferClient(URL,key,http) as client:
                        private=await client.call("create_transfer",{"direction":"upload","max_bytes":10})
                    response=await http.get("/.well-known/oauth-protected-resource/fishmarket/mcp")
                    assert response.status_code==200
                    assert response.json()["resource"]==URL
                    body={"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{
                        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities":{},
                        "io.modelcontextprotocol/clientInfo":{"name":"unverified","version":"1"}}}}
                    response=await http.post(URL,json=body,headers={"Authorization":"",
                        "MCP-Protocol-Version":"2026-07-28","Mcp-Method":"server/discover",
                        "Accept":"application/json"})
                    assert response.status_code==200
                    result=response.json()["result"]
                    assert private["id"] not in json.dumps(result)
                    assert private["token"] not in json.dumps(result)
                    assert "ready_count" not in json.dumps(result)
                    assert "capabilities" in result
                    # Spoofing the discover header cannot authorize private tool calls.
                    body["method"]="tools/call"
                    body["params"]={"name":"list_ready","arguments":{}}
                    refused=await http.post(URL,json=body,headers={"Authorization":"",
                        "Mcp-Method":"server/discover","MCP-Protocol-Version":"2026-07-28",
                        "Accept":"application/json"})
                    assert refused.status_code!=200 or "result" not in refused.json()
    asyncio.run(run())


def test_deployment_config_matches_actual_kit(tmp_path):
    source=Path(__file__).parents[1]/"deploy/config.example.json"
    data=json.loads(source.read_text())
    (tmp_path/"introspection.secret").write_text("synthetic-test-secret")
    (tmp_path/"introspection.secret").chmod(0o600)
    file=tmp_path/"config.json";file.write_text(json.dumps(data))
    config=ServiceConfig(file)
    assert config.url=="https://pond.example/fishmarket/mcp"
    assert config.bind=="0.0.0.0" and config.port==8000


def test_fishmarket_as_its_own_issuer(tmp_path):
    """A lone fishmarket signs a metadata-document client in at its own /authorize and /approve."""
    from boring_service_kit.testing import metadata_document, sign_in
    from boring_fishmarket.storage import Store
    claude, callback = "https://claude.ai/oauth/x.json", "https://claude.ai/cb"
    (tmp_path / "oauth").mkdir(mode=0o700)
    (tmp_path / "approval").write_text("market-approval")
    (tmp_path / "config.json").write_text(json.dumps({
        "url": URL, "issuer": {"database": "oauth/oauth.db", "approval_secret_file": "approval", "subject": "robin"},
        "access": [{"subject": "robin", "role": "writer"}]}))
    config = ServiceConfig(tmp_path / "config.json")
    config.issuer.fetch = lambda client_id: metadata_document(client_id, callback)
    store = Store(tmp_path / "state", max_file=1024, quota=4096)

    async def run():
        fish = App(store, config)
        async with fish.sdk.router.lifespan_context(fish.sdk):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fish), base_url=ORIGIN) as http:
                metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
                assert metadata["client_id_metadata_document_supported"] is True
                tokens = await sign_in(http, URL, claude, callback, "market-approval")
                listed = await http.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                              "io.modelcontextprotocol/clientCapabilities": {},
                              "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}}}},
                    headers={"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json",
                             "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"})
                assert listed.status_code == 200 and listed.json()["result"]["tools"]
                assert (await fish.caller(type("R", (), {"headers": {
                    "authorization": "Bearer " + tokens["access_token"]}})())) == '["http://127.0.0.1:18861","robin"]'
    try:
        asyncio.run(run())
    finally:
        store.close()
