---
name: email-triage
description: Plan and execute Inbox triage using pigeon and a mail gateway.
---
pigeon never sends anything without the owner's approval. Sending is off by default. With allow_send=true, "N send" ("N enviar") sends an unchanged draft it created from a draft rule. "N forward HANDLE" ("N reenviar HANDLE") forwards one message to another configured account's address, with no added text, through the mailbox forward-draft operation. Both read the draft back before sending; at most five sends per reply. Rules, proceed and judgments can never send. It never composes mail or takes draft text or forward recipients from mail; replies use the mailbox reply operation. It never deletes permanently; the calendar stays read-only.


Follow [the quick start](../../../docs/quickstart.md) for routine runs. Follow [AGENTS.md](../../../AGENTS.md) for exceptions.

## Standard run message

Use the [quick start message command](../../../docs/quickstart.md#commit-and-message). See [message format and folder help](../../../docs/setup.md#run-message-format). Follow [Writing a rule](../../../AGENTS.md#writing-a-rule) for rules.toml; require separate exact-entry confirmation for never-touch.toml changes. Use plan.json decisions for numbered replies.
