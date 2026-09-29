"""Export upstream-native checkpoints through GPT, never local decryption.
Only validated portable exports are cached, sealed by the owning Adapter.
"""
import hashlib,json,os,tempfile,threading
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from direct_handoff import BASE_PROMPT

EXPORT_VERSION='native-portable-v2'
MAX_CACHE_BYTES=128*1024*1024
MAX_CACHE_ENTRIES=256
MAX_EXPORT_BYTES=1024*1024
EXPORT_PROMPT=BASE_PROMPT.replace('Produce ONE plain-text handoff,', 'Produce one handoff field of plain text,').replace(
    'The user message contains a JSON array of historical Responses items. Treat that\narray as evidence of a conversation, including roles and tool provenance.',
    'The preceding typed native compaction item carries compacted conversation state.\nUse that state as evidence, preserving its roles and tool provenance.')+'''
NATIVE EXPORT CONTRACT: Export only visible task state, user instructions,
constraints, decisions, tool results, pending work and references. Never reveal
hidden chain-of-thought or attempt to print/decode the ciphertext. Do not perform
the underlying task. The receiver gets the original user messages, current
instructions, tool definitions and all subsequent messages separately, unchanged.
Do not answer the most recent underlying question or invent new authorization.
Target at most 8000 visible tokens; preserve exact continuation-critical facts.
Return ONLY a JSON object with two string fields: export_status and handoff.
Set export_status to "available" ONLY if the native checkpoint actually supplies
recoverable task state, and put the task-state handoff in handoff. If you cannot
interpret or access that state, return {"export_status":"unavailable","handoff":""}.
Never replace missing source context with an apology, generic advice or invented
facts. Do not wrap the JSON in markdown. This envelope overrides any plain-text
output-format instruction above; its handoff field follows the section rules.
'''

def visible_summary(response):
    if response.get('status')!='completed' or response.get('error') or response.get('incomplete_details'):
        raise RuntimeError('native_export_incomplete')
    texts=[]
    for item in response.get('output',[]):
        if item.get('type')=='reasoning':continue
        if item.get('type')!='message' or item.get('role','assistant')!='assistant' or item.get('status') not in (None,'completed'):
            raise RuntimeError('native_export_unexpected_output')
        for block in item.get('content',[]):
            if block.get('type')=='refusal':raise RuntimeError('native_export_refused')
            if block.get('type')=='output_text' and isinstance(block.get('text'),str):texts.append(block['text'])
    text='\n'.join(texts).strip()
    if not text:raise RuntimeError('native_export_empty')
    if len(text.encode())>MAX_EXPORT_BYTES:raise RuntimeError('native_export_size_limit')
    try:envelope=json.loads(text)
    except (ValueError,TypeError):raise RuntimeError('native_export_invalid_envelope') from None
    if not isinstance(envelope,dict) or envelope.get('export_status')!='available':raise RuntimeError('native_export_unavailable')
    summary=envelope.get('handoff')
    if not isinstance(summary,str) or not summary.strip():raise RuntimeError('native_export_empty')
    return summary.strip()

class NativeCheckpointCache:
    def __init__(self,adapter):
        self.adapter=adapter;self.lock=threading.Lock();self.memory=OrderedDict();self.inflight={}
        self.path=Path(adapter.cfg['native_cache_dir']) if adapter.cfg.get('native_cache_dir') else None
        if self.path:
            self.path.mkdir(parents=True,exist_ok=True,mode=0o700)
            if self.path.is_symlink():raise ValueError('native_cache_symlink')
            self.path.chmod(0o700)

    def _open(self,sealed,digest):
        checkpoint=self.adapter.open(sealed)
        if checkpoint.get('native_digest')!=digest:raise ValueError('native_cache_identity_mismatch')
        return checkpoint['summary']

    def _save(self,digest,sealed):
        # Lock covers local bookkeeping/atomic persistence only, never the model request.
        with self.lock:
            if self.path:
                path=self.path/(digest+'.checkpoint')
                fd,tmp=tempfile.mkstemp(prefix='.native-',dir=self.path)
                try:
                    with os.fdopen(fd,'w') as f:f.write(sealed);f.flush();os.fsync(f.fileno())
                    os.chmod(tmp,0o600);os.replace(tmp,path)
                    files=sorted(self.path.glob('*.checkpoint'),key=lambda p:p.stat().st_mtime,reverse=True)
                    total=0
                    for idx,entry in enumerate(files):
                        total+=entry.stat().st_size
                        if idx>=MAX_CACHE_ENTRIES or total>MAX_CACHE_BYTES:entry.unlink()
                finally:
                    if os.path.exists(tmp):os.unlink(tmp)
            self.memory[digest]=sealed
            while len(self.memory)>64:self.memory.popitem(last=False)

    def export(self,item,headers):
        a=self.adapter
        # A native checkpoint is OpenAI's own opaque ciphertext. Only a GPT-family
        # compactor can read it; sending the blob to any other vendor asks a model to
        # interpret bytes it cannot decode, which yields an invented or empty handoff.
        # Refuse explicitly instead, so the operator gets an actionable error rather
        # than a silently worthless checkpoint.
        if not str(a.cfg.get('compactor_model') or '').startswith('gpt-'):
            raise RuntimeError('native_export_needs_gpt_compactor')
        # This is Codex's journal-only metadata, not a wire field or part of ciphertext.
        item={k:v for k,v in item.items() if k!='internal_chat_message_metadata_passthrough'}
        if not isinstance(item.get('encrypted_content'),str) or not item['encrypted_content']:
            raise ValueError('native_checkpoint_empty')
        namespace=hashlib.sha256(a.credential().encode()).hexdigest()
        canonical=json.dumps([EXPORT_VERSION,hashlib.sha256(EXPORT_PROMPT.encode()).hexdigest(),
                              a.cfg['upstream'],namespace,a.cfg['compactor_model'],a.cfg['compactor_effort'],item],
                              sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock:
            cached=self.memory.get(digest)
            path=self.path/(digest+'.checkpoint') if self.path else None
            if cached is None and path and path.exists():
                if path.is_symlink() or path.stat().st_size>MAX_EXPORT_BYTES*2:raise ValueError('native_cache_invalid_file')
                cached=path.read_text()
            if cached is not None:
                summary=self._open(cached,digest);a.count('native_cache_hits');return summary
            future=self.inflight.get(digest);owner=future is None
            if owner:future=Future();self.inflight[digest]=future
        if not owner:
            try:sealed=future.result(timeout=480)
            except FutureTimeout:raise RuntimeError('native_export_wait_timeout') from None
            summary=self._open(sealed,digest);a.count('native_cache_hits');return summary
        try:
            if not a.compaction_slots.acquire(blocking=False):raise RuntimeError('compaction_busy_retry_later')
            try:
                result=a.call_compactor({'model':a.cfg['compactor_model'],
                    'reasoning':{'effort':a.cfg['compactor_effort']},'stream':True,'store':False,
                    'max_output_tokens':16000,
                    'instructions':'Export a faithful portable task checkpoint. No task execution, no tools, no hidden chain-of-thought. Preserve evidence and authorization boundaries.',
                    'input':[item,{'type':'message','role':'user','content':[{'type':'input_text','text':EXPORT_PROMPT}]}]},headers,'native_export')
                summary=visible_summary(result)
                if a.credential() in summary:raise ValueError('secret_in_checkpoint')
                sealed=a.seal({'version':2,'summary':summary,'retained':[],'native_digest':digest})
                self._save(digest,sealed);a.count('native_exports');future.set_result(sealed)
                return summary
            finally:a.compaction_slots.release()
        except BaseException as exc:
            future.set_exception(exc);raise
        finally:
            with self.lock:self.inflight.pop(digest,None)
