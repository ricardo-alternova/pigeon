# Rules

Pattern: a full address (`news@example.com`), an exact domain (`example.com` does not match `sub.example.com`; write `*.example.com` for subdomains, or `news@*.example.com` for one sender on any subdomain), a subject in quotes (`"Your receipt"`), or a category in words (`Consumer marketing and newsletters, e.g. news@example.com`). A category matches any sender that clearly fits the description; examples only illustrate it.
Action: `trash`, `archive`, `move <folder>`, `spam`, `unsubscribe+trash`, `draft`, `forward <address>`. Add `max N` to change this rule's cap from 10, and `own cap` to keep it out of the global cap (plain words like `special cap: 50 per run, not counted toward the global cap` mean the same). Add `after send: <action>` to a `draft` or `forward` row to act on the original once the draft is sent. `<YYYY.MM>` in a folder name means the message's month.
Scope: account handles from `accounts.md`, separated by commas, or `all`. Notes / Exceptions: when the rule must not act (`never invoices`).

| ID | Pattern | Action | Scope | Notes / Exceptions |
|---|---|---|---|---|
