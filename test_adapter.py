import base64, gzip, json, secrets, threading, unittest
from http.server import ThreadingHTTPServer
import requests
from adapter import Adapter, handler_for, PREFIX


def sent_body(kwargs):
    """Decode the request body from either transport form.

    The adapter may hand `requests` a `json=` structure or pre-serialized `data=`, and it
    may compress that data with zstd. Protocol tests should not care which, so they read
    every forwarded body through this helper.
    """
    if "json" in kwargs:
        return kwargs["json"]
    data = kwargs.get("data")
    if isinstance(data, (bytes, bytearray)):
        import zstandard
        try:
            data = zstandard.ZstdDecompressor().decompress(data)
        except Exception:
            pass
        return json.loads(bytes(data).decode("utf-8"))
    return data

CFG={'upstream':'https://example.invalid','compactor_model':'gpt-6-sol',
     'compactor_effort':'medium','port':0}
TOKEN='synthetic-local-auth-not-a-real-secret'
class FakeResponse:
    status_code=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_lines(self,**kw):
        yield b'data: '+json.dumps({'type':'response.completed','response':{
            'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'Project ABC-42; do not publish; pending verify.'}]}],
            'usage':{'input_tokens':50,'output_tokens':12,'total_tokens':62}}}).encode()
class FakeTransport:
    def __init__(self):self.calls=[]
    def post(self,url,**kw):
        self.calls.append(kw)
        if sent_body(kw).get('input',[{}])[0].get('type')=='compaction':
            class NativeResponse(FakeResponse):
                def iter_lines(self,**kwargs):
                    for line in super().iter_lines(**kwargs):
                        event=json.loads(line[5:]);part=event['response']['output'][0]['content'][0]
                        part['text']=json.dumps({'export_status':'available','handoff':part['text']})
                        yield b'data: '+json.dumps(event).encode()
            return NativeResponse()
        return FakeResponse()
class Tests(unittest.TestCase):
    def setUp(self):
        self.key=secrets.token_bytes(32);self.transport=FakeTransport()
        self.a=Adapter(CFG,self.key,self.transport,lambda:TOKEN)
    def test_key_survives_process_recreation(self):
        cp=self.a.seal({'version':2,'summary':'ABC-42','retained':[]})
        b=Adapter(CFG,self.key,self.transport,lambda:TOKEN)
        self.assertEqual(b.open(cp)['summary'],'ABC-42')
        self.assertNotIn('ABC-42',base64.urlsafe_b64decode(cp[len(PREFIX):]).decode('latin1'))
        raw=bytearray(base64.urlsafe_b64decode(cp[len(PREFIX):]));raw[-1]^=1
        with self.assertRaises(ValueError):b.open(PREFIX+base64.urlsafe_b64encode(raw).decode())
        with self.assertRaises(ValueError):Adapter(CFG,secrets.token_bytes(32),self.transport,lambda:TOKEN).open(cp)
    def test_media_pending_calls_and_native_state(self):
        image={'role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,synthetic'}]}
        call={'type':'function_call','call_id':'call_pending','name':'write','arguments':'{}'}
        evidence,retained=self.a.prepare_history([image,call])
        self.assertEqual(retained,[image,call]);self.assertNotIn('base64',json.dumps(evidence))
        cp=self.a.seal({'version':2,'summary':'state','retained':retained})
        expanded=self.a.expand([{'type':'compaction','encrypted_content':cp}],'gemini-3.8-flash')
        self.assertEqual(expanded[1:],retained)
        native={'type':'compaction','encrypted_content':'opaque-native'}
        self.assertIn('ABC-42',json.dumps(self.a.expand([native],'deepseek-flash')))
        self.assertEqual(self.a.expand([native],'gpt-6-sol'),[native])
    def test_http_auth_gzip_and_b_model(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.a))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        body={'model':'deepseek-flash','stream':True,'instructions':'Only local preview is authorized.',
              'input':[{'role':'user','content':'Keep ABC-42'},{'type':'compaction_trigger'}]}
        try:
            self.assertEqual(requests.post(url+'/responses',json=body).status_code,401)
            h={'Authorization':'Bearer '+TOKEN,'Origin':'https://evil.invalid'}
            self.assertEqual(requests.post(url+'/responses',json=body,headers=h).status_code,401)
            h={'Authorization':'Bearer '+TOKEN,'Content-Encoding':'gzip'}
            r=requests.post(url+'/responses',data=gzip.compress(json.dumps(body).encode()),headers=h)
            self.assertEqual(r.status_code,200);self.assertIn('response.completed',r.text)
            payload=sent_body(self.transport.calls[-1])
            self.assertEqual(payload['model'],'gpt-6-sol');self.assertEqual(payload['reasoning']['effort'],'medium')
            self.assertIn('Only local preview',json.dumps(payload));self.assertNotIn('compaction_trigger',json.dumps(payload))
            h={'Authorization':'Bearer '+TOKEN}
            legacy=requests.post(url+'/responses/compact',json=body,headers=h)
            self.assertEqual(legacy.json()['object'],'response.compaction')
            self.assertEqual(self.a.stats['compactions'],2)
        finally:server.shutdown();server.server_close()
    def test_gpt_native_and_tools_forward_without_rewrite(self):
        import io
        class Raw:
            def __init__(self):self.buf=io.BytesIO(b'data: {"type":"response.completed"}\n\n')
            def read1(self,n,**kw):return self.buf.read(n)
        class Response(FakeResponse):
            headers={'Content-Type':'text/event-stream','X-Request-ID':'synthetic-id'}
            def __init__(self):self.raw=Raw()
        class Transport(FakeTransport):
            def request(self,method,url,**kw):self.calls.append((method,url,kw));return Response()
        transport=Transport();self.a.transport=transport
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.a))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        body={'model':'gpt-6-astra','stream':True,'tools':[{'type':'custom','name':'patch'}],
              'input':[{'type':'compaction','encrypted_content':'native-opaque'},{'type':'compaction_trigger'}]}
        try:
            r=requests.post('http://127.0.0.1:'+str(server.server_port)+'/responses',json=body,
                 headers={'Authorization':'Bearer '+TOKEN,'x-codex-beta-features':'remote_compaction_v2'})
            self.assertEqual(r.status_code,200);self.assertEqual(r.headers['X-Request-ID'],'synthetic-id')
            forwarded=transport.calls[-1][2]
            self.assertEqual(sent_body(forwarded),body)
            self.assertEqual(forwarded['headers']['x-codex-beta-features'],'remote_compaction_v2')
            self.assertEqual(self.a.stats['compactions'],0)
        finally:server.shutdown();server.server_close()

    def test_incomplete_never_replaces_history(self):
        class Failed(FakeResponse):
            def iter_lines(self,**kw):yield b'data: {"type":"response.incomplete","response":{}}'
        class Transport:
            def post(self,*args,**kw):return Failed()
        self.a.transport=Transport()
        with self.assertRaises(RuntimeError):self.a.compact({'model':'deepseek-flash','input':[]},{})
        self.assertEqual(self.a.stats['compactions'],0)
if __name__=='__main__':unittest.main(verbosity=2)
