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
