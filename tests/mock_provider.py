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
