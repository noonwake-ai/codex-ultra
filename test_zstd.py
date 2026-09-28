import json, threading, unittest
import requests, zstandard as zstd
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import adapter as adapter_module
from adapter import Adapter, handler_for
class Up(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  n=int(self.headers['Content-Length']);b=json.loads(self.rfile.read(n));self.server.body=b
  out={'id':'r','status':'completed','model':b['model'],'output':[]}
  data=('data: '+json.dumps({'type':'response.completed','response':out})+'\n\n').encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(data)
class Tests(unittest.TestCase):
 def test_zstd_decodes_codex_desktop_wire(self):
  upstream=ThreadingHTTPServer(('127.0.0.1',0),Up);threading.Thread(target=upstream.serve_forever,daemon=True).start()
  cfg={'upstream':'http://127.0.0.1:%s'%upstream.server_port,'compactor_model':'gpt-6-sol','compactor_effort':'medium','port':0}
  # Adapter's safety guard is intentionally HTTPS-only; use a locally constructed instance for protocol test.
  a=object.__new__(Adapter)
  a.cfg=cfg;a.credential=lambda:'synthetic-token';a.transport=requests;a.lock=threading.Lock();a.compaction_slots=threading.BoundedSemaphore(2)
  a.stats={'requests':0,'compactions':0,'errors':0,'footprint_observations':0,'footprint_errors':0,'started_at':0}
  a.prefix=adapter_module.PREFIX;a.aad=adapter_module.AAD;a.media_models=();a.cipher=None
  local=ThreadingHTTPServer(('127.0.0.1',0),handler_for(a));threading.Thread(target=local.serve_forever,daemon=True).start()
  body={'model':'gpt-6-astra','stream':False,'tools':[{'type':'custom','name':'patch'}],'input':[{'role':'user','content':'DIAGNOSTIC_OK'}]}
  try:
   raw=zstd.ZstdCompressor(write_content_size=False).compress(json.dumps(body).encode())
   r=requests.post('http://127.0.0.1:%s/responses'%local.server_port,data=raw,headers={'Authorization':'Bearer synthetic-token','Content-Encoding':'zstd','originator':'Codex Desktop'})
   self.assertEqual(r.status_code,200);self.assertEqual(upstream.body,body)
   bad=requests.post('http://127.0.0.1:%s/responses'%local.server_port,data=b'corrupt',headers={'Authorization':'Bearer synthetic-token','Content-Encoding':'zstd'})
   self.assertEqual(bad.status_code,400);self.assertEqual(a.stats['last_error']['phase'],'decode')
   self.assertNotIn('corrupt',json.dumps(a.stats))
  finally:local.shutdown();local.server_close();upstream.shutdown();upstream.server_close()
if __name__=='__main__':unittest.main()
