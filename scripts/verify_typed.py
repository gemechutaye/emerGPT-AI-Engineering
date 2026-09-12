"""Explicit live-provider HTTP journey; writes synthetic answers and provenance only."""
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from uuid import uuid4
import httpx

base='http://localhost:8017'
out=Path('artifacts/verification/typed-http-20260911') / datetime.now(timezone.utc).strftime('%H%M%S')
out.mkdir(parents=True,exist_ok=True)
with httpx.Client(base_url=base,headers={'Origin':base},timeout=110) as client:
    client.post('/api/v1/session').raise_for_status()
    conversation=client.post('/api/v1/conversations',json={'title':'Verified PT-006 workflow'})
    conversation.raise_for_status();cid=conversation.json()['id']
    context=client.patch(f'/api/v1/conversations/{cid}/context',json={'patient_id':'PT-006','expected_version':1})
    context.raise_for_status()
    body={'question':'What does PT-006 need before deciding on RF microneedling, and what exact price and downtime are documented?',
          'context_version':context.json()['context_version'],'idempotency_key':uuid4().hex}
    submitted=client.post(f'/api/v1/conversations/{cid}/runs',json=body)
    submitted.raise_for_status();rid=submitted.json()['id']
    duplicate=client.post(f'/api/v1/conversations/{cid}/runs',json=body)
    duplicate.raise_for_status();assert duplicate.json()['id']==rid
    for _ in range(220):
        response=client.get('/api/v1/runs/'+rid);response.raise_for_status();run=response.json()
        if run['status'] in {'completed','failed','cancelled','superseded','interrupted'}:break
        time.sleep(.5)
    (out/'run.json').write_text(json.dumps(run,indent=2)+'\n')
    assert run['status']=='completed',run.get('error')
    citations=0
    for statement in run['answer']['statements']+run['answer']['next_steps']:
        for citation in statement['citations']:
            source=client.get('/api/v1/sources/'+citation['doc_id'],params={'index_id':citation['index_id']})
            source.raise_for_status();doc=source.json()
            assert doc['text'][citation['start']:citation['end']]==citation['quote']
            citations+=1
    draft=client.post('/api/v1/drafts',json={'run_id':rid});draft.raise_for_status();d=draft.json()
    saved=client.patch('/api/v1/drafts/'+d['id'],json={'text':d['text']+'\n\nInternal review requested.', 'expected_version':d['version']})
    saved.raise_for_status()
    checked=client.post('/api/v1/drafts/'+d['id']+'/recheck');checked.raise_for_status()
    (out/'draft.json').write_text(json.dumps(checked.json(),indent=2)+'\n')
    assert checked.json()['check']['version']==saved.json()['version']
    history=client.get('/api/v1/conversations/'+cid);history.raise_for_status()
    assert history.json()['runs'][0]['id']==rid
    with httpx.Client(base_url=base,headers={'Origin':base}) as stranger:
        stranger.post('/api/v1/session').raise_for_status()
        assert stranger.get('/api/v1/runs/'+rid).status_code==404
        assert stranger.get('/api/v1/drafts/'+d['id']).status_code==404
    summary={'at':datetime.now(timezone.utc).isoformat(),'url':base,'run_id':rid,'conversation_id':cid,
        'status':run['status'],'answer_status':run['answer']['status'],'citations_verified':citations,
        'draft_version':checked.json()['version'],'recheck':checked.json()['check'],
        'checks':['real answer','same-key no duplicate run','original quote spans','history','draft save/edit/recheck','stranger isolation'],
        'classification':'real HTTP and live provider; not browser/audio acceptance','metrics':run['metrics']}
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in {'recheck','metrics'}},indent=2))
