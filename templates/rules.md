# Rules

Pattern: a full address (`news@example.com`), an exact domain (`example.com` does not match `sub.example.com`; write `*.example.com` for subdomains), or a subject in quotes (`"Your receipt"`).
Action: `trash`, `archive`, `move <folder>`, `spam`, `unsubscribe+trash`, `draft`, `forward <address>`. Add `max N` to raise this rule's cap from 10. `<YYYY.MM>` in a folder name means the message's month.
Scope: account handles from `accounts.md`, separated by commas, or `all`. Notes / Exceptions: when the rule must not act (`never invoices`).

| ID | Pattern | Action | Scope | Notes / Exceptions |
|---|---|---|---|---|
