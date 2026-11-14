"""Invented, offline mailbox transport."""
class MockProvider:
    def __init__(self, ids, outcome='ok', remove=True):
        self.ids = list(ids)
        self.outcome, self.remove = outcome, remove
        self.calls = []

    def perform(self, call):
        self.calls.append(call)
        if self.outcome == 'ok' and self.remove and call['id'] in self.ids:
            self.ids.remove(call['id'])
        return self.outcome

    def list_inbox_ids(self, account):
        return self.ids


class MockDrive:
    """Shared atomic name and version conditions; no external transport."""
    def __init__(self, created='2026-11-11T08:00:00+00:00'):
        self.files = {}
        self.item = None
        self.version = 0
        self.created = created
        self.calls = []

    def perform(self, call):
        self.calls.append(call.copy())
        assert call['name'] == 'pigeon-run-lock'
        if call['operation'] == 'create':
            assert call['conflict'] == 'fail'
            if self.item is not None:
                return {'status': 'conflict'}
            self.version += 1
            self.item = dict(id='item-' + str(self.version), etag='"version-' + str(self.version) + '"',
                             name=call['name'], created=self.created)
            return dict(status='created', **(self.item | {'etag': '"create-' + str(self.version) + '"'}))
        if call['operation'] == 'read':
            return dict(status='ok', **self.item) if self.item else {'status': 'not-found'}
        assert call['operation'] == 'delete' and call['if_match']
        if self.item is None:
            return {'status': 'not-found'}
        if self.item['etag'] != call['if_match']:
            return {'status': 'precondition-failed'}
        self.item = None
        return {'status': 'deleted'}

    def file_call(self, call):
        from pathlib import Path
        self.calls.append(call.copy())
        name = call['name']
        if call['operation'] == 'download':
            if name not in self.files:
                return {'status': 'absent'}
            Path(call['path']).write_bytes(self.files[name])
            return {'status': 'fetched'}
        assert call['operation'] == 'upload'
        self.files[name] = Path(call['path']).read_bytes()
        return {'status': 'done', 'size': len(self.files[name])}


class MockGateway:
    """Answer recipe batches from a hand-built listing, with 100-row pages."""
    def __init__(self, listing, config):
        self.listing, self.config = listing, config
        self.exchanges = 0

    def batch(self, calls):
        if any(c.get("path") == "SIMULATION_ONLY" for c in calls):
            raise ValueError("simulation placeholder is not a gateway path")
        self.exchanges += 1
        return [dict(ok=True, value=self.answer(c)) for c in calls]

    def answer(self, call):
        from datetime import datetime
        if 'action' in call:
            a = call['account']
            fields = self.config['accounts'][a]
            assert call['input']['accountId'] == fields['executor_account']
            inner = call['input']['input']
            assert call['path'].startswith('tools')
            if call['action'] == 'create-folder':
                assert 'body' in inner
                return {'id':'folder-example'}
            if call['action'] == 'create-draft':
                assert inner['draft_text'] == call['draft_text']
                return {'id':'draft-example'}
            assert call['action'] in {'trash', 'archive', 'move', 'spam'}
            assert inner['path'].get('id', inner['path'].get('message-id')) == call['id']
            self.listing['messages'][:] = [m for m in self.listing['messages']
                                           if (m['account'], m['id']) != (call['account'], call['id'])]
            return {}
        a = next(a for a,f in self.config['accounts'].items() if f['executor_account']==call['input']['accountId'])
        f = self.config['accounts'][a]
        inner = call['input']['input']; q = inner.get('query',{}); path = inner.get('path',{})
        tool = call['path'].split(f['executor_app']+'.')[-1] if call['path'].startswith('tools.') else call['path'].split('].', 1)[1]
        messages = [m for m in self.listing['messages'] if m['account']==a]
        folders = self.listing.get('folders',{}).get(a,[])
        if tool.endswith('meUserGetUser'):
            return dict(mail=f.get('address', 'reader@example.org'))
        if tool.endswith('getProfile'):
            return dict(messagesTotal=self.listing['accounts'][a],historyId='120',emailAddress=f.get('address','reader@example.org'))
        if tool.endswith('history.list'): return dict(history=[],historyId='120')
        if tool.endswith('labels.list'):
            return dict(labels=[dict(id=x['id'],name=x['name'],type='user') for x in folders])
        if tool.endswith('messages.get'):
            m = next(m for m in messages if m['id']==path['id'])
            h = [dict(name='From',value=m['sender']),dict(name='Subject',value=m['subject'])]
            if 'unsubscribe_url' in m: h.append(dict(name='List-Unsubscribe',value='<'+m['unsubscribe_url']+'>'))
            if m.get('unsubscribe_one_click'): h.append(dict(name='List-Unsubscribe-Post',value='List-Unsubscribe=One-Click'))
            return dict(id=m['id'],labelIds=['INBOX'],payload=dict(headers=h),internalDate=str(int(datetime.fromisoformat(m['date']).timestamp()*1000)))
        if tool.endswith('messages.list'):
            rows = [dict(id=m['id']) for m in messages if not q.get('q') or m.get('has_attachment')]
            start = int(q.get('pageToken',0)); page = rows[start:start+100]
            return dict(messages=page,**({'nextPageToken':str(start+100)} if start+100<len(rows) else {}))
        if tool.endswith('ListMessages'):
            rows = [dict(id=m['id'],subject=m['subject'],receivedDateTime=m['date'],hasAttachments=m.get('has_attachment',False),
                         **{'from':dict(emailAddress=dict(address=m['sender']))}) for m in messages]
        else:
            parent_id = path.get('mailFolder-id')
            parent = next((x['name']+'/' for x in folders if x['id']==parent_id),'')
            rows = [dict(id=x['id'],displayName=x['name'].split('/')[-1],
                         totalItemCount=self.listing['accounts'][a] if i==0 else 0,
                         childFolderCount=sum(y['name'].startswith(x['name']+'/') and y['name'].count('/')==x['name'].count('/')+1 for y in folders))
                    for i,x in enumerate(folders) if x['name'].startswith(parent) and '/' not in x['name'][len(parent):]]
        start=q.get('$skip',0); return dict(value=rows[start:start+q.get('$top',100)])


def save_plan(store, token, path, plan):
    """Make a command-generated dry plan, then apply this test's plan mutations."""
    import contextlib
    import tempfile
    import os
    from unittest.mock import patch
    import execute
    import triage
    if 'run_token' in plan:
        execute.atomic(path, plan)
        return
    output = store / 'fixture-plan.json'
    guardrails = store / 'guardrails.md'
    missing = not guardrails.exists()
    if missing:
        guardrails.write_text('Fixture policy\n')
    try:
        with tempfile.TemporaryFile(mode='w+', dir=store) as sink, patch.dict(os.environ, PIGEON_HOME=str(store)), contextlib.redirect_stdout(sink):
            triage.main(['plan', '--token', token, '--inbox', str(store/'inbox.json'),
                         '--plan-out', str(output), '--dry-run'])
        generated = execute.read(output)
        plan['run_token'] = generated['run_token']
        plan.setdefault('run_id', generated['run_id'])
        execute.atomic(path, plan)
    finally:
        output.unlink(missing_ok=True)
        if missing:
            guardrails.unlink()
