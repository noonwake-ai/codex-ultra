"""Loopback Responses adapter; B handoff, encrypted self-contained checkpoints.
No request/response content is logged. Native exports are cached encrypted only.
"""
import argparse, base64, gzip, hashlib, hmac, io, json, math, os, secrets, sqlite3
import subprocess, threading, time, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import requests
import zstandard as zstd
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import tool_image_bridge
from direct_handoff import Strategy
from native_checkpoint import NativeCheckpointCache
from tool_image_bridge import NormalizationError, normalize_request

# These four values are only defaults; a config file may override each of them.
PREFIX = 'cu1:'
AAD = b'code-ultra-checkpoint-v1'
MAX_BYTES = 64 * 1024 * 1024
HOP = {'host','content-length','transfer-encoding','connection','keep-alive',
       'proxy-authenticate','proxy-authorization','te','trailer','upgrade','content-encoding'}
SERVICE = 'code-ultra'
ACCOUNT = 'checkpoint-key-v1'
EFFORTS = frozenset(('none','low','medium','high','xhigh','max','ultra'))
KNOWN_INPUT_TYPES = frozenset((
    'message', 'reasoning', 'function_call', 'function_call_output',
    'custom_tool_call', 'custom_tool_call_output', 'compaction',
    'compaction_trigger',
))
TOOL_RESULT_TYPES = frozenset(('function_call_output', 'custom_tool_call_output'))
MEDIA_PART_TYPES = frozenset(('input_image', 'input_file', 'input_audio', 'input_video'))


def keychain_key(service=SERVICE, account=ACCOUNT, create=False):
    # One exact item lookup per process, no discovery, no decrypted keychain dump.
    r = subprocess.run(['/usr/bin/security','find-generic-password','-s',service,
                        '-a',account,'-w'], capture_output=True, timeout=30)
    if r.returncode == 0:
        key = base64.urlsafe_b64decode(r.stdout.strip())
        if len(key) != 32: raise ValueError('invalid_checkpoint_key')
        return key
    if not create or r.returncode != 44:
        raise RuntimeError('checkpoint_keychain_unavailable')
    key = secrets.token_bytes(32)
    r = subprocess.run(['/usr/bin/security','add-generic-password','-s',service,
                        '-a',account,'-w',base64.urlsafe_b64encode(key).decode()],
                       capture_output=True, timeout=30)
    if r.returncode: raise RuntimeError('checkpoint_keychain_create_failed')
    return key


def read_api_key(cfg):
    """Resolve the gateway credential without ever copying or logging it.

    Two supported routes, in order:

    1. an environment variable, for users who keep the key outside every config
       file. The variable name is `credential_env` in the service config.
    2. the CC Switch database, for users who already manage providers there.
       Only the one current record is read, and only its key field is used.
    """
    name = cfg.get('credential_env') or 'CODE_ULTRA_API_KEY'
    env_key = os.environ.get(name)
    if isinstance(env_key, str) and len(env_key) >= 10:
        return env_key
    db = cfg.get('cc_db'); provider = cfg.get('credential_provider_id')
    if not db or not provider:
        raise RuntimeError('credential_not_configured')
    con = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True, timeout=5)
    try:
        row = con.execute("SELECT settings_config FROM providers WHERE id=? AND app_type='codex'",
                          (provider,)).fetchone()
        if not row: raise RuntimeError('credential_provider_missing')
        key = json.loads(row[0])['auth']['OPENAI_API_KEY']
        if not isinstance(key,str) or len(key)<10: raise RuntimeError('credential_invalid')
        return key
    finally: con.close()


def user(text):
    return {'type':'message','role':'user','content':[{'type':'input_text','text':text}]}


class Adapter:
    def __init__(self, cfg, key, transport=requests, credential=None):
        self.cfg, self.transport = cfg, transport
        self.prefix = cfg.get('checkpoint_prefix') or PREFIX
        self.aad = (cfg.get('checkpoint_aad') or AAD.decode()).encode()
        self.cipher = AESGCM(key)
        self.media_models = tuple(m for m in (cfg.get('media_models') or ()) if isinstance(m,str))
        tool_image_bridge.configure(self.media_models)
        self.credential = credential or (lambda:read_api_key(cfg))
        self.lock = threading.Lock()
        self.compaction_slots = threading.BoundedSemaphore(2)
        self.stats = {'requests':0,'compactions':0,'errors':0,'native_exports':0,'native_cache_hits':0,
                      'footprint_observations':0,'footprint_errors':0,'started_at':int(time.time())}
        self.strategy = Strategy()
        u = urlsplit(cfg['upstream'])
        if u.scheme != 'https' or u.username or u.password or u.hostname in ('localhost','127.0.0.1'):
            raise ValueError('upstream_must_be_external_https')
        if not isinstance(cfg.get('compactor_model'),str) or not cfg['compactor_model'].strip():
            raise ValueError('compactor_model_required')
        if cfg.get('compactor_effort') not in EFFORTS:
            raise ValueError('compactor_effort_not_supported')
        self.native_cache=NativeCheckpointCache(self)

    def count(self, key):
        with self.lock: self.stats[key] += 1

    @staticmethod
    def _json_bytes(value):
        """Measure serialized size without retaining request content."""
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def input_footprint(cls, items):
        """Return numeric/enumerated input shape only; never text, IDs, or hashes."""
        profile = {
            'kind': 'array' if isinstance(items, list) else 'other',
            'items': 0,
            'json_bytes': cls._json_bytes(items),
            'types': {},
            'tool_output_items': 0,
            'tool_output_json_bytes': 0,
            'reasoning_items': 0,
            'reasoning_json_bytes': 0,
            'media_items': 0,
            'media_parts': 0,
            'media_json_bytes': 0,
        }
        if not isinstance(items, list):
            return profile
        profile['items'] = len(items)
        for item in items:
            item_bytes = cls._json_bytes(item)
            kind = item.get('type') if isinstance(item, dict) else None
            if kind not in KNOWN_INPUT_TYPES:
                kind = 'other'
            bucket = profile['types'].setdefault(kind, {'items': 0, 'json_bytes': 0})
            bucket['items'] += 1
            bucket['json_bytes'] += item_bytes
            if kind in TOOL_RESULT_TYPES:
                profile['tool_output_items'] += 1
                profile['tool_output_json_bytes'] += item_bytes
            if kind == 'reasoning':
                profile['reasoning_items'] += 1
                profile['reasoning_json_bytes'] += item_bytes
            if not isinstance(item, dict):
                continue
            item_has_media = False
            for field in ('content', 'output'):
                parts = item.get(field)
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict) or part.get('type') not in MEDIA_PART_TYPES:
                        continue
                    profile['media_parts'] += 1
                    profile['media_json_bytes'] += cls._json_bytes(part)
                    item_has_media = True
            if item_has_media:
                profile['media_items'] += 1
        return profile

    @classmethod
    def request_footprint(cls, route, body, raw_items, expanded_items):
        """Build a safe request-size breakdown for the local health endpoint."""
        body = body if isinstance(body, dict) else {}
        raw_body = dict(body)
        raw_body['input'] = raw_items
        expanded_body = dict(body)
        expanded_body['input'] = expanded_items
        tools = body.get('tools')
        guardian = body.get('guardian_history')
        model = body.get('model')
        return {
            'route': route if route in ('forward', 'compact') else 'other',
            'model_family': 'gpt' if isinstance(model, str) and model.startswith('gpt-') else 'third_party',
            'raw_request_json_bytes': cls._json_bytes(raw_body),
            'expanded_request_json_bytes': cls._json_bytes(expanded_body),
            'instructions_present': isinstance(body.get('instructions'), str),
            'instructions_json_bytes': cls._json_bytes(body.get('instructions')) if isinstance(body.get('instructions'), str) else 0,
            'tools': {
                'kind': 'array' if isinstance(tools, list) else 'other',
                'items': len(tools) if isinstance(tools, list) else 0,
                'json_bytes': cls._json_bytes(tools),
            },
            'guardian_history': {
                'present': 'guardian_history' in body,
                'input': cls.input_footprint(guardian),
            },
            'raw_input': cls.input_footprint(raw_items),
            'expanded_input': cls.input_footprint(expanded_items),
        }

    def observe_request(self, route, body, raw_items, expanded_items):
        # Telemetry must never alter the request path when a malformed future item appears.
        try:
            footprint = self.request_footprint(route, body, raw_items, expanded_items)
        except Exception:
            self.count('footprint_errors')
            return
        with self.lock:
            self.stats['footprint_observations'] += 1
            self.stats['last_request_footprint'] = footprint

    def seal(self, obj):
        nonce = secrets.token_bytes(12)
        raw = json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode()
        return self.prefix + base64.urlsafe_b64encode(nonce+self.cipher.encrypt(nonce,raw,self.aad)).decode()

    def open(self, value):
        try:
            raw = base64.urlsafe_b64decode(value[len(self.prefix):])
            obj = json.loads(self.cipher.decrypt(raw[:12],raw[12:],self.aad))
            if obj.get('version') != 2 or not isinstance(obj.get('summary'),str): raise ValueError()
            if not isinstance(obj.get('retained',[]),list): raise ValueError()
            return obj
        except Exception: raise ValueError('checkpoint_integrity_or_key_failure') from None

    def expand(self, items, model, drop_trigger=False, headers=None, *,
               preserve_reasoning=False, defer_native=False):
        if not isinstance(items,list): return items
        from collections import Counter
        def fingerprint(item):
            return hashlib.sha256(json.dumps(item,ensure_ascii=False,sort_keys=True,
                separators=(',',':')).encode()).digest()
        # Codex can keep original messages alongside a local checkpoint. Consume
        # only matching retained copies; explicit input items keep their order and
        # deliberate multiplicity. Stable IDs and all other fields participate.
        explicit=Counter(fingerprint(item) for item in items
            if isinstance(item,dict) and item.get('type')!='compaction')
        out = []
        for item in items:
            if not isinstance(item,dict): raise ValueError('invalid_input_item')
            if item.get('type')=='compaction':
                value=item.get('encrypted_content','')
                if isinstance(value,str) and value.startswith(self.prefix):
                    cp=self.open(value)
                    out.append(user('<context_checkpoint>\n'+cp['summary']+'\n</context_checkpoint>'))
                    for retained in cp.get('retained',[]):
                        key=fingerprint(retained)
                        if explicit[key]:explicit[key]-=1
                        else:out.append(retained)
                elif model.startswith('gpt-') or defer_native:
                    # A deferred native item is an unchanged group boundary until
                    # pure media validation succeeds; it is never forwarded to Gemini.
                    out.append(item)
                else:
                    out.append(user('<context_checkpoint>\n'+self.export_native(item,headers or {})+'\n</context_checkpoint>'))
            elif (item.get('type')=='reasoning' and not model.startswith('gpt-')
                    and not preserve_reasoning):
                # Hidden model reasoning is not portable task state. Keep only its public summary,
                # with original messages and tool call/results still present exactly once.
                texts=[p['text'] for p in item.get('summary',[]) if isinstance(p,dict) and p.get('type')=='summary_text' and isinstance(p.get('text'),str)]
                if texts:out.append({'type':'message','role':'assistant','content':[{'type':'output_text','text':'\n'.join(texts)}]})
            elif item.get('type')=='compaction_trigger' and drop_trigger:
                continue
            else: out.append(item)
        return out

    def prepare_forward(self, body, headers=None):
        """Prepare normal Responses input without mutating its original history.

        Only the exact Gemini target gets media adaptation. Local checkpoints
        expand first, with reasoning still typed so complete call/result groups
        remain contiguous. Native ciphertext is an equivalent group boundary to
        its eventual user checkpoint: defer its potentially paid export until
        media validation has succeeded. The second pass exports that native
        state and applies the original public-summary reasoning conversion.
        Compact requests and every other model keep the original expand path.
        """
        items=body.get('input',[])
        model=body['model']
        if model in self.media_models:
            expanded=self.expand(items,model,headers=headers,
                                 preserve_reasoning=True,defer_native=True)
            prepared=normalize_request({**body,'input':expanded},self.media_models)
            expanded=self.expand(prepared['input'],model,headers=headers)
        else:
            prepared=body
            expanded=self.expand(items,model,headers=headers)
        result={**prepared,'input':expanded}
        self.observe_request('forward',body,items,expanded)
        return result

    @staticmethod
    def prepare_history(items):
        # Keep media bytes out of the textual evidence, and retain complete tool
        # call/result pairs so the receiver can use each original media result.
        media_types={'input_image','input_file','input_audio','input_video'}
        call_types={'function_call','custom_tool_call'}
        result_types={'function_call_output','custom_tool_call_output'}
        results={i.get('call_id') for i in items if i.get('type') in result_types}
        calls={}
        for index,item in enumerate(items):
            if item.get('type') in call_types and item.get('call_id') is not None:
                calls.setdefault(item['call_id'],[]).append(index)
        evidence=[];retained_indices=set()
        for index,item in enumerate(items):
            clone=item;has_media=False
            fields=('content','output') if item.get('type') in result_types else ('content',)
            for field in fields:
                parts=item.get(field)
                if not isinstance(parts,list):continue
                if not any(isinstance(p,dict) and p.get('type') in media_types for p in parts):continue
                if clone is item:clone=dict(item)
                clone[field]=[{'type':part['type'],
                    'placeholder':'[Original media block retained verbatim outside summary]'}
                    if isinstance(part,dict) and part.get('type') in media_types else part for part in parts]
                has_media=True
            if has_media:
                retained_indices.add(index)
                if item.get('type') in result_types and item.get('call_id') is not None:
                    retained_indices.update(calls.get(item['call_id'],()))
            if item.get('type') in call_types and item.get('call_id') not in results:
                retained_indices.add(index)
            evidence.append(clone)
        return evidence,[item for index,item in enumerate(items) if index in retained_indices]

    def export_native(self,item,headers):
        return self.native_cache.export(item,headers)

    def call_compactor(self,payload,headers,label='compaction'):
        payload['stream']=True
        payload['service_tier']=self.cfg.get('service_tier','priority')
        token=self.credential()
        h={k:v for k,v in headers.items() if k.lower() in
           ('user-agent','session_id','conversation_id','originator','x-codex-beta-features')}
        h.update({'Authorization':'Bearer '+token,'Content-Type':'application/json',
                  'X-Client-Request-Id':str(uuid.uuid4())})
        started=time.monotonic();terminal=None;outputs=[]

        def record_terminal(event_type,response):
            # Diagnostics contain only fixed enums and numeric measurements, never
            # upstream text, response/request IDs, input content or credential values.
            response=response if isinstance(response,dict) else {}
            def enum(value,allowed):
                return None if value is None else value if isinstance(value,str) and value in allowed else 'other'
            def number(value):
                if isinstance(value,bool) or not isinstance(value,(int,float)) or value<0:return None
                if isinstance(value,float) and not math.isfinite(value):return None
                return value
            usage=response.get('usage');usage=usage if isinstance(usage,dict) else {}
            numeric_usage={}
            for name in ('input_tokens','output_tokens','total_tokens'):
                value=number(usage.get(name))
                if value is not None:numeric_usage[name]=value
            for group,name in (('input_tokens_details','cached_tokens'),('output_tokens_details','reasoning_tokens')):
                details=usage.get(group)
                value=number(details.get(name)) if isinstance(details,dict) else None
                if value is not None:numeric_usage[name]=value
            visible_chars=0
            result_output=response.get('output') or outputs
            if isinstance(result_output,list):
                for item in result_output:
                    if not isinstance(item,dict) or item.get('type')!='message':continue
                    content=item.get('content',[])
                    if not isinstance(content,list):continue
                    for part in content:
                        if isinstance(part,dict) and part.get('type')=='output_text' and isinstance(part.get('text'),str):
                            visible_chars+=len(part['text'])
            incomplete=response.get('incomplete_details');error=response.get('error')
            diagnostic={'requested_model':self.cfg['compactor_model'],
                'requested_effort':self.cfg['compactor_effort'],
                'response_model':enum(response.get('model'),{self.cfg['compactor_model']}),
                'response_tier':enum(response.get('service_tier'),{'auto','default','flex','scale','priority'}),
                'event_type':event_type,
                'status':enum(response.get('status'),{'queued','in_progress','completed','incomplete','failed','cancelled'}),
                'incomplete_reason':enum(incomplete.get('reason') if isinstance(incomplete,dict) else None,
                    {'max_output_tokens','content_filter'}),
                'error_code':enum(error.get('code') if isinstance(error,dict) else None,
                    {'server_error','rate_limit_exceeded','invalid_prompt','context_length_exceeded',
                     'insufficient_quota','invalid_request_error'}),
                'usage':numeric_usage,'visible_chars':visible_chars,
                'seconds':round(max(0,time.monotonic()-started),3),
                'input_chars':len(json.dumps(payload.get('input',[]),ensure_ascii=False,separators=(',',':'))),
                'max_output_tokens':number(payload.get('max_output_tokens'))}
            with self.lock:self.stats['last_'+label]=diagnostic

        with self.transport.post(self.cfg['upstream'].rstrip('/')+'/responses',
                json=payload,headers=h,stream=True,timeout=(20,240)) as response:
            if response.status_code!=200:raise RuntimeError('compactor_http_'+str(response.status_code))
            for line in response.iter_lines(chunk_size=1):
                if time.monotonic()-started>480:raise TimeoutError('compactor_deadline')
                if not line.startswith(b'data:'):continue
                data=line[5:].strip()
                if data==b'[DONE]':continue
                event=json.loads(data)
                if event.get('type')=='response.output_item.done':outputs.append(event['item'])
                if event.get('type') in ('response.completed','response.failed','response.incomplete'):
                    terminal=event.get('response')
                    record_terminal(event['type'],terminal)
                    if (event['type']!='response.completed' or not isinstance(terminal,dict)
                            or terminal.get('status')!='completed' or terminal.get('error') is not None
                            or terminal.get('incomplete_details') is not None):
                        raise RuntimeError('compactor_incomplete')
                    # A validated terminal completes the request; TCP close is not
                    # required and a later read failure must not undo success.
                    break
                if event.get('type')=='error':
                    record_terminal('error',{'error':event.get('error') or {'code':event.get('code')}})
                    raise RuntimeError('compactor_error')
        if terminal is None:
            record_terminal('stream.ended',{})
            raise RuntimeError('compactor_no_completed_response')
        return {**terminal,'output':terminal.get('output') or outputs,
                'seconds':round(time.monotonic()-started,3)}

    def compact(self, body, headers):
        raw_items=body.get('input',[])
        items=self.expand(raw_items,body.get('model',''),True,headers)
        self.observe_request('compact',body,raw_items,items)
        if not self.compaction_slots.acquire(blocking=False): raise RuntimeError('compaction_busy_retry_later')
        try:
            if not isinstance(items,list): raise ValueError('compaction_requires_history_array')
            evidence,retained=self.prepare_history(items)
            if body.get('instructions'):
                evidence=[{'role':'developer','content':body['instructions']}] + evidence
            adapter=self
            class Client:
                def call(self,payload,label):return adapter.call_compactor(payload,headers)
            result=self.strategy.compact(evidence,Client(),{
                'model':self.cfg['compactor_model'],'effort':self.cfg['compactor_effort'],
                'budget_tokens':8000,'max_output_tokens':16000,'scenario':'local_production'})
            token=self.credential()
            if token in result['summary']:raise ValueError('secret_in_checkpoint')
            item={'type':'compaction','id':'cmp_'+uuid.uuid4().hex,
                  'encrypted_content':self.seal({'version':2,'summary':result['summary'],'retained':retained})}
            with self.lock:
                self.stats['last_compaction_plan']={key:result['metadata'].get(key) for key in
                    ('strategy_version','input_token_budget','source_request_tokens_estimate',
                     'max_request_tokens_estimate','max_total_tokens_estimate','calls','map_chunks',
                     'reduce_levels','split_source_items','source_fragments','history_item_count',
                     'history_json_chars','summary_chars','seconds','usage','stages')}
                self.stats['last_compaction_plan']['retained_items']=len(retained)
            self.count('compactions')
            return item,result['metadata'].get('usage') or {'input_tokens':0,'output_tokens':0,'total_tokens':0}
        finally:self.compaction_slots.release()


def handler_for(adapter):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0'
        server_version='CodeUltraAdapter/1'
        def log_message(self,*args): pass
        def send_response(self,*args,**kwargs):
            self.response_started=True
            return super().send_response(*args,**kwargs)
        def setup(self):
            super().setup();self.connection.settimeout(600)
        def reply(self,status,obj):
            data=json.dumps(obj).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(data)
        def allowed(self):
            # Reject browser-originated calls; require exact current existing Codex credential.
            if self.headers.get('Origin'):return False
            return hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+adapter.credential())
        def do_GET(self):
            if self.path=='/health':
                return self.reply(200,{'status':'ok','model':adapter.cfg['compactor_model'],
                    'effort':adapter.cfg['compactor_effort'],'strategy':'B_direct_handoff',**adapter.stats})
            try:
                if not self.allowed():return self.reply(401,{'error':{'code':'unauthorized'}})
                if urlsplit(self.path).path not in ('/models','/v1/models'):return self.reply(404,{'error':{'code':'unsupported_path'}})
                return self.forward('GET',None)
            except Exception:return self.reply(502,{'error':{'code':'upstream_unavailable'}})
        def do_POST(self):
            started=False; phase='authentication'; encoding='unknown'
            adapter.count('requests')
            try:
                if not self.allowed():return self.reply(401,{'error':{'code':'unauthorized'}})
                path=urlsplit(self.path).path
                if path not in ('/responses','/v1/responses','/responses/compact','/v1/responses/compact'):
                    return self.reply(404,{'error':{'code':'unsupported_path'}})
                phase='decode'
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=MAX_BYTES:raise ValueError('request_size_limit')
                raw=self.rfile.read(n)
                encoding=(self.headers.get('Content-Encoding') or 'identity').lower().strip()
                if encoding=='gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:raw=gz.read(MAX_BYTES+1)
                elif encoding=='zstd':
                    with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as zr:raw=zr.read(MAX_BYTES+1)
                elif encoding not in ('identity',''):raise ValueError('unsupported_encoding')
                if len(raw)>MAX_BYTES:raise ValueError('request_size_limit')
                phase='parse'
                body=json.loads(raw)
                if not isinstance(body,dict) or not isinstance(body.get('model'),str):raise ValueError('model_required')
                items=body.get('input',[])
                compact=path.endswith('/compact') or (isinstance(items,list) and any(
                    isinstance(i,dict) and i.get('type')=='compaction_trigger' for i in items))
                if compact and not body['model'].startswith('gpt-'):
                    phase='compaction'
                    item,usage=adapter.compact(body,dict(self.headers))
                    rid='resp_'+uuid.uuid4().hex
                    response={'id':rid,'object':'response','status':'completed','model':body['model'],
                              'output':[item],'usage':usage}
                    if path.endswith('/compact') or not body.get('stream',False):
                        if path.endswith('/compact'):response['object']='response.compaction'
                        return self.reply(200,response)
                    self.send_response(200);started=True
                    self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache')
                    self.end_headers()
                    events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                        {'type':'response.output_item.added','output_index':0,'item':item},
                        {'type':'response.output_item.done','output_index':0,'item':item},
                        {'type':'response.completed','response':response}]
                    for i,e in enumerate(events):
                        e['sequence_number']=i
                        self.wfile.write(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode());self.wfile.flush()
                else:
                    phase='forward'
                    body=adapter.prepare_forward(body,dict(self.headers))
                    started=True; self.forward('POST',body)
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception as exc:
                adapter.count('errors')
                # Never expose raw upstream bodies, credentials or request contents.
                code=(exc.code if isinstance(exc,NormalizationError) else
                      str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__)
                allowed_codes={'checkpoint_integrity_or_key_failure','opaque_native_checkpoint_requires_source_history',
                    'compactor_input_budget_exceeded','compactor_empty_handoff','compactor_unexpected_output',
                    'compactor_intermediate_too_large','compactor_reduction_did_not_converge',
                    'compactor_source_metadata_too_large','compactor_input_budget_too_small',
                    'compactor_input_budget_out_of_range','compaction_busy_retry_later','compactor_incomplete','compactor_no_completed_response','request_size_limit','unsupported_encoding','native_export_incomplete','native_export_empty',
                    'native_export_refused','native_export_unexpected_output','native_cache_identity_mismatch','native_checkpoint_empty',
                    'native_export_invalid_envelope','native_export_unavailable','native_export_wait_timeout','native_export_size_limit','native_cache_invalid_file',
                    'ambiguous_tool_image_group','invalid_tool_image','unknown_image_detail','unknown_tool_content'}
                if code not in allowed_codes:code='adapter_request_failed'
                with adapter.lock:
                    adapter.stats['last_error']={'phase':phase,'code':code,'exception_type':type(exc).__name__,
                        'encoding':encoding if encoding in ('identity','gzip','zstd') else 'other','at':int(time.time())}
                status=(422 if isinstance(exc,NormalizationError) else
                        413 if code=='request_size_limit' else 415 if code=='unsupported_encoding' else
                        400 if phase in ('decode','parse') else 502)
                if not getattr(self,'response_started',False):
                    self.reply(status,{'error':{'code':code,'message':'Compression/forwarding failed ('+code+'); original history must be retained.'}})
        def forward(self,method,body):
            # Preserve beta, turn metadata, request identity, tools and SSE response headers.
            h={k:v for k,v in self.headers.items() if k.lower() not in HOP}
            h['Accept-Encoding']='identity'
            if body is not None:h['Content-Type']='application/json'
            url=adapter.cfg['upstream'].rstrip('/')+self.path
            with adapter.transport.request(method,url,json=body,headers=h,stream=True,timeout=(20,480)) as r:
                self.send_response(r.status_code)
                for k,v in r.headers.items():
                    if k.lower() not in HOP and k.lower() not in ('server','date'):self.send_header(k,v)
                self.end_headers()
                # read1 drains available bytes rather than accumulating an SSE-sized block.
                while True:
                    chunk=r.raw.read1(65536,decode_content=True)
                    if not chunk:break
                    self.wfile.write(chunk);self.wfile.flush()
    return Handler


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--init-key',action='store_true')
    a=p.parse_args();cfg=json.loads(Path(a.config).read_text())
    cfg.setdefault('native_cache_dir',str(Path(a.config).resolve().parent/'native-checkpoint-cache'))
    service=cfg.get('keychain_service') or SERVICE;account=cfg.get('keychain_account') or ACCOUNT
    try:
        key=keychain_key(service,account,create=a.init_key)
    except Exception:
        if a.init_key:raise
        # Do not let launchd repeatedly prompt for an unavailable Keychain item.
        print('Checkpoint key unavailable; service paused. Explicit restart required.',flush=True)
        threading.Event().wait()
        return
    if a.init_key:print('Checkpoint key ready in macOS Keychain');return
    adapter=Adapter(cfg,key)
    server=ThreadingHTTPServer(('127.0.0.1',cfg['port']),handler_for(adapter))
    server.daemon_threads=True
    print('Local compaction adapter ready',flush=True)
    server.serve_forever()
if __name__=='__main__':main()
