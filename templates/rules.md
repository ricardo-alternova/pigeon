# Rules

Pattern: a full address (`news@example.com`), an exact domain (`example.com` does not match `sub.example.com`; write `*.example.com` for subdomains, or `news@*.example.com` for one sender on any subdomain), or a subject in quotes (`"Your receipt"`).
Action: `trash`, `archive`, `move <folder>`, `spam`, `unsubscribe+trash`, `draft`, `forward <address>`. Add `max N` to change this rule's cap from 10, and `own cap` to keep it out of the global cap. Add `after send: <action>` to a `draft` or `forward` row to act on the original once the draft is sent. `<YYYY.MM>` in a folder name means the message's month.
Scope: account handles from `accounts.md`, separated by commas, or `all`. Notes / Exceptions: when the rule must not act (`never invoices`).

| ID | Pattern | Action | Scope | Notes / Exceptions |
|---|---|---|---|---|
