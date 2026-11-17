"""Real stdio MCP Apps fixture: python tests/fixtures/mcp_apps_server.py.

No API key or network is required. MCP_APPS_FIXTURE_LOG optionally records tool
names in a caller-owned temporary file so browser tests can prove no replay.
"""
import asyncio
import json
import os
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

URI = "ui://flight-fixture/search.html"
MIME = "text/html;profile=mcp-app"
server = Server("mcp-apps-flight-fixture")
revision = 0
HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{color-scheme:light dark;font:14px system-ui,sans-serif}body{margin:0;padding:18px;background:#fff;color:#222}body.dark{background:#1f2023;color:#eee}h2{font-size:18px;margin:0 0 12px}p{color:#666}body.dark p{color:#aaa}table{width:100%;border-collapse:collapse;margin:12px 0}td{padding:9px 4px;border-bottom:1px solid #8884}td:last-child{text-align:right;font-variant-numeric:tabular-nums}button{border:0;border-radius:8px;background:#5368df;color:white;padding:10px 14px;cursor:pointer;font:inherit}button:disabled{opacity:.5;cursor:wait}#status{min-height:20px;overflow-wrap:anywhere}article{max-width:760px}
</style></head><body><article><h2 id="title">Flights</h2><div id="route"></div><table aria-label="Flights"><tbody id="rows"></tbody></table><button id="refresh" disabled>Refresh</button><p id="status" role="status">Loading…</p></article><script>
const pending=new Map();let next=0,locale='en',revision=0,hasResult=false;
const labels={en:['Flights','Refresh prices','Updated','New York → Hong Kong','Loading…'],zh:['航班','刷新价格','已更新','纽约 → 香港','加载中…'],ja:['フライト','料金を更新','更新済み','ニューヨーク → 香港','読み込み中…']};
function words(){return labels[locale]||labels.en}function context(c){c=c||{};if(typeof c.locale==='string')locale=c.locale.split('-')[0];if(c.theme==='dark'||c.theme==='light')document.body.classList.toggle('dark',c.theme==='dark');const w=words();document.querySelector('#title').textContent=w[0];document.querySelector('#refresh').textContent=w[1];document.querySelector('#route').textContent=w[3];document.querySelector('table').setAttribute('aria-label',w[0]);if(hasResult)document.querySelector('#status').textContent=w[2]+' · '+revision;else document.querySelector('#status').textContent=w[4];}
function send(method,params){const id='fixture-'+(++next);parent.postMessage({jsonrpc:'2.0',id,method,params},'*');return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));}
function notify(method,params){parent.postMessage({jsonrpc:'2.0',method,params},'*')}
function render(result){if(result.isError){document.querySelector('#status').textContent=result.content?.[0]?.text||'Error';return;}const data=result.structuredContent||{};revision=data.revision||0;hasResult=true;const rows=document.querySelector('#rows');rows.replaceChildren();for(const flight of (data.flights||[]).slice(0,4)){const row=document.createElement('tr');for(const value of [flight.airline,flight.departure,'USD '+flight.price]){const td=document.createElement('td');td.textContent=value;row.append(td);}rows.append(row);}document.querySelector('#refresh').disabled=false;document.querySelector('#status').textContent=words()[2]+' · '+revision;notify('ui/notifications/size-changed',{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight});}
addEventListener('message',event=>{if(event.source!==parent)return;const message=event.data;if(!message||message.jsonrpc!=='2.0')return;if(message.id&&pending.has(message.id)){const task=pending.get(message.id);pending.delete(message.id);message.error?task.reject(new Error(message.error.message)):task.resolve(message.result);return;}if(message.method==='ui/resource-teardown'){window.fixtureTeardownCount=(window.fixtureTeardownCount||0)+1;setTimeout(()=>parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}},'*'),window.fixtureTeardownDelay||0);return;}if(message.method==='ui/notifications/tool-result')render(message.params);if(message.method==='ui/notifications/host-context-changed')context(message.params);});
document.querySelector('#refresh').onclick=async()=>{const button=document.querySelector('#refresh');button.disabled=true;try{render(await send('tools/call',{name:'refresh_flights',arguments:{previous:revision}}));}catch(error){document.querySelector('#status').textContent=error.message;}finally{button.disabled=false;}};
send('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'flight-fixture',version:'1.0.0'},appCapabilities:{availableDisplayModes:['inline','fullscreen']}}).then(result=>{context(result.hostContext||{});notify('ui/notifications/initialized',{});}).catch(error=>document.querySelector('#status').textContent=error.message);
</script></body></html>'''


def tool(name, visibility, schema, *, ui=False):
    metadata = {"visibility": visibility}
    if ui:
        metadata["resourceUri"] = URI
    return types.Tool.model_validate({"name": name, "title": "Flight search", "description": name,
        "inputSchema": schema, "_meta": {"ui": metadata}})


@server.list_tools()
async def list_tools():
    return [tool("list_flights", ["model", "app"], {"type": "object", "properties": {"date": {"type": "string"}}, "additionalProperties": False}, ui=True),
            tool("refresh_flights", ["app"], {"type": "object", "properties": {"previous": {"type": "integer"}}, "required": ["previous"], "additionalProperties": False}),
            tool("model_only", ["model"], {"type": "object", "properties": {}, "additionalProperties": False})]


@server.list_resources()
async def list_resources():
    return [types.Resource.model_validate({"uri": URI, "name": "flight-search", "mimeType": MIME,
        "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}}})]


@server.read_resource()
async def read_resource(uri):
    if str(uri) == "flight://details/1":
        return [ReadResourceContents(json.dumps({"flight": 1, "airline": "ANA"}), "application/json")]
    if str(uri) == "flight://ticket/1":
        return [ReadResourceContents(b"fixture-ticket", "application/pdf")]
    if str(uri) != URI:
        raise ValueError("Unknown UI resource")
    return [ReadResourceContents(HTML, MIME)]


@server.call_tool()
async def call_tool(name, arguments):
    global revision
    if name not in {"list_flights", "refresh_flights", "model_only"}:
        raise ValueError("Unknown tool")
    revision += 1
    record = os.environ.get("MCP_APPS_FIXTURE_LOG")
    if record:
        with Path(record).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "arguments": arguments, "revision": revision}) + "\n")
    flights = [{"id": index, "airline": ["Cathay Pacific", "United", "Air Canada", "ANA"][index % 4],
                "departure": ["12:55", "14:10", "17:45", "21:05"][index % 4],
                "price": 1020 + index * 9 + revision} for index in range(125)]
    return types.CallToolResult.model_validate({"content": [{"type": "text", "text": "125 flights available."}],
        "structuredContent": {"flights": flights, "revision": revision},
        "_meta": {"fixtureViewOnly": "not-for-model-context", "completeRows": len(flights)}})


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
