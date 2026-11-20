# Executor calls for pigeon

pigeon reaches mail and calendars only through Executor (executor.sh), over its MCP server. The MCP tools you use are `search`, `describe`, `execute`, and `resume`. Every mail and calendar call below runs inside one `execute` call (a TypeScript sandbox).

Replace `<accountId>`, `<email>`, and the other `<...>` values with the user's values from `local/accounts.md`.

## Sandbox basics

- Call form: `tools['<app>'].<api path>({ accountId, input:{ path, query, body } })`. Path parameters (`userId`, `id`, `'message-id'`, `'mailFolder-id'`, `calendarId`) go in `path`; filters and paging (`q`, `maxResults`, `$filter`, `$top`) go in `query`; payloads go in `body`.
- Apps: `gmail-api`, `outlook-mail-api`, `google-calendar-api`, `outlook-calendar-api`.
- A tool can return `{ ok:false, error }`. Always check before using the result. Unwrap like this:

```ts
const ok = async (p) => { const r = await p; if (r && r.ok === false) throw new Error(JSON.stringify(r.error)); return r && r.data !== undefined ? r.data : r; };
```

- The sandbox has no `fetch`, `Buffer`, `atob`, `btoa`, or `TextEncoder`. Do not enumerate `tools`.
- If you are unsure of a path or its input shape, check it with `describe` or `search` before calling it.
- Reads and writes go in separate `execute` calls. First read everything and decide, then act on explicit IDs.
- Return compact JSON from each read call (one object per account with only the fields you need), not raw API responses.

## Find the user's accounts

Run `search` with a query like `gmail getProfile` or `outlook mail list messages`. The results list the connected accounts for each app with their `accountId` and the account's email label. Save each `accountId` in `local/accounts.md`.

If an account the user wants is missing, ask them to connect it in Executor (Gmail, Outlook mail, Google Calendar, Outlook Calendar), then run `search` again.

## Check identity on every run

Gmail: the address must match `local/accounts.md`.

```ts
const p = await ok(tools['gmail-api'].gmail.users.getProfile({ accountId:'<accountId>', input:{ path:{ userId:'me' } } }));
// p.emailAddress === '<email>'
```

Outlook: a successful Inbox read on the listed `accountId` confirms the account.

```ts
await ok(tools['outlook-mail-api'].meMailFolder.meGetMailFolders({ accountId:'<accountId>', input:{ path:{ 'mailFolder-id':'inbox' } } }));
```

Wrong address, `AccountSelectionInvalid`, or `OAuthReconnectRequired`: report `<handle> ?`, skip that account, and run `search` to see the current accounts. Tell the user to reconnect it in Executor.

## Read

### Gmail Inbox

```ts
const A = '<accountId>', G = tools['gmail-api'].gmail.users;
let ids = [], pageToken;
do {
  const r = await ok(G.messages.list({ accountId:A, input:{ path:{ userId:'me' }, query:{ q:'in:inbox', maxResults:100, pageToken } } }));
  ids.push(...(r.messages ?? []).map(m => m.id));
  pageToken = r.nextPageToken;
} while (pageToken);
const msgs = [];
for (const id of ids) {
  const m = await ok(G.messages.get({ accountId:A, input:{ path:{ userId:'me', id }, query:{ format:'metadata',
    metadataHeaders:['From','To','Cc','Subject','Date','Message-ID','References','List-Unsubscribe','List-Unsubscribe-Post'] } } }));
  const h = Object.fromEntries((m.payload?.headers ?? []).map(x => [x.name, x.value]));
  msgs.push({ id, threadId:m.threadId, from:h.From, to:h.To, cc:h.Cc, subject:h.Subject, date:h.Date, unsub:h['List-Unsubscribe'], unsubPost:h['List-Unsubscribe-Post'], preview:m.snippet });
}
return msgs;
```

`in:inbox` includes the Promotions, Social, and Updates tabs. Use `format:'full'` only when you need the body.

### Outlook Inbox

```ts
const r = await ok(tools['outlook-mail-api'].meMessage.meListMessages({ accountId:'<accountId>', input:{ query:{
  $top:50, $skip:0, $filter:"parentFolderId eq 'inbox'",
  $select:['id','conversationId','subject','from','toRecipients','ccRecipients','receivedDateTime','isRead','hasAttachments','bodyPreview','webLink'],
  $orderby:['receivedDateTime desc'] } } }));
```

Repeat with `$skip` 50, 100, ... while `@odata.nextLink` is present. The Inbox includes Focused and Other.

### Body, headers, thread

- Gmail body: `G.messages.get` with `format:'full'`.
- Outlook body and headers: `meMessage.meGetMessages` with `path:{ 'message-id':id }` and `$select:['body','internetMessageHeaders','webLink']`.
- Gmail thread: `G.threads.get` with `path:{ userId:'me', id:threadId }` and `format:'metadata'`. Read only.
- Outlook thread: `meListMessages` with `$filter:"conversationId eq '<id>'"`.

### Drafts

- Gmail: `G.drafts.list` with `path:{ userId:'me' }`, then `G.drafts.get`.
- Outlook: `meListMessages` with `$filter:'isDraft eq true'` and `$select:['id','subject','toRecipients','ccRecipients','body','createdDateTime','webLink']`.

### Read-only search

- Gmail: `G.messages.list` with `q:'from:<domain>'`.
- Outlook: `meListMessages` with `$search:'"from:<domain>"'` (no `$orderby` with `$search`).

### Folders and labels

- Outlook: `meMailFolder.meListMailFolders` with `$top:250`, then `meMailFolder.meMailFoldersListChildFolders` with `path:{ 'mailFolder-id':id }` for each level. Match on `displayName`.
- Gmail: `G.labels.list`. A nested label `Clients/Acme` matches `Acme`.
- Zero or several matches: do not move. Report it. Resolve each folder once per run and reuse the ID.

## Quick check (no new mail?)

Run once per run, read only. `S` is the `accounts` object from `local/state.json`.

```ts
const S = /* accounts from local/state.json */;
const GA = { '<handle>':'<accountId>' };   // Gmail accounts
const OA = { '<handle>':'<accountId>' };   // Outlook accounts
const G = tools['gmail-api'].gmail.users, O = tools['outlook-mail-api'];
const hash = s => { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0; return h.toString(16); };
const now = {}, changed = [];
for (const [h, a] of Object.entries(GA)) { try {
  const p = await ok(G.getProfile({ accountId:a, input:{ path:{ userId:'me' } } }));
  let n = -1;
  if (S[h]?.historyId) { try {
    const hl = await ok(G.history.list({ accountId:a, input:{ path:{ userId:'me' }, query:{ startHistoryId:S[h].historyId, labelId:'INBOX', historyTypes:['messageAdded','labelAdded'], maxResults:50 } } }));
    n = (hl.history || []).length; } catch (e) { n = -1; } }
  const dr = await ok(G.drafts.list({ accountId:a, input:{ path:{ userId:'me' }, query:{ maxResults:50 } } }));
  const d = (dr.drafts || []).length;
  now[h] = { historyId:p.historyId, drafts:d };
  if (n !== 0 || d !== S[h]?.drafts) changed.push(h);
} catch (e) { now[h] = { err:String(e).slice(0,120) }; changed.push(h + '?'); } }
for (const [h, a] of Object.entries(OA)) { try {
  const l = await ok(O.meMessage.meListMessages({ accountId:a, input:{ query:{ $filter:"parentFolderId eq 'inbox'", $top:100, $select:['id','receivedDateTime'], $orderby:['receivedDateTime desc'] } } }));
  const v = l.value || [];
  const df = await ok(O.meMailFolder.meGetMailFolders({ accountId:a, input:{ path:{ 'mailFolder-id':'drafts' }, query:{ $select:['totalItemCount'] } } }));
  now[h] = { n:v.length, ids:hash(v.map(x => x.id.slice(-24)).join(',')), drafts:df.totalItemCount };
  if (now[h].ids !== S[h]?.ids || now[h].drafts !== S[h]?.drafts) changed.push(h);
} catch (e) { now[h] = { err:String(e).slice(0,120) }; changed.push(h + '?'); } }
return { changed, now };
```

- `changed` empty: no new mail and no new drafts.
- Anything in `changed`, or a `?` (failed or expired cursor): full run.
- At the end of a full run, run it again after acting and save `now` to `local/state.json`, so this run's own moves do not trigger the next run.
- Outlook does not take `$deltatoken` in Executor (422). The Inbox ID hash plus the draft count is the cursor.

## Write

| Action | Gmail (`tools['gmail-api'].gmail.users`) | Outlook (`tools['outlook-mail-api'].meMessage`) |
|---|---|---|
| trash | `messages.trash`, `path:{ userId:'me', id }` | `meMessagesMessageMove`, `path:{ 'message-id':id }`, `body:{ DestinationId:'deleteditems' }` |
| archive | `messages.modify`, `body:{ removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:'archive'` |
| move to folder | `messages.modify`, `body:{ addLabelIds:[labelId], removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:<folderId>` |
| spam | `messages.modify`, `body:{ addLabelIds:['SPAM'], removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:'junkemail'` |
| several at once | `messages.batchModify`, `body:{ ids:[...], addLabelIds, removeLabelIds }`, only with explicit IDs read and evaluated in this run | one move per ID |

- Outlook move returns the message with a **new ID**. Do not reuse the old one.
- After acting, re-read the Inbox and confirm each ID left it.
- Never call: `messages.delete`, `messages.batchDelete`, `threads.trash`, `threads.modify`, `meDeleteMessages`, any label create, update, or delete, any mail folder create, update, or delete.

### Unsubscribe

Executor has no unsubscribe call. Read `List-Unsubscribe` and `List-Unsubscribe-Post` (Gmail metadata; Outlook `internetMessageHeaders`). Make the HTTPS request yourself, outside the Executor sandbox, with your own HTTP tool or browser: a one-click POST (form body `List-Unsubscribe=One-Click`) when `List-Unsubscribe-Post` says `One-Click`, otherwise a GET of the URL. Never send a `mailto:` unsubscribe.

## Drafts and sending

### Outlook

```ts
const M = tools['outlook-mail-api'].meMessage;
// reply draft to the last message of the thread
await ok(M.meMessagesMessageCreateReply({ accountId:'<accountId>', input:{ path:{ 'message-id':'<lastId>' }, body:{ Comment:'<text>' } } }));
// forward draft (attachments travel with it)
await ok(M.meMessagesMessageCreateForward({ accountId:'<accountId>', input:{ path:{ 'message-id':'<id>' },
  body:{ ToRecipients:[{ emailAddress:{ address:'<to>' } }], Comment:'<text>' } } }));
```

Both return the draft with its `id` and `webLink`. Edit a draft with `meUpdateMessages`.

### Gmail

`drafts.create` takes the RFC 822 message encoded as base64url. The sandbox has no `btoa`, so encode in plain JS:

```ts
function b64url(s) {
  const bytes = [];
  for (const ch of s) { const c = ch.codePointAt(0);
    if (c < 0x80) bytes.push(c);
    else if (c < 0x800) bytes.push(0xC0|c>>6, 0x80|c&63);
    else if (c < 0x10000) bytes.push(0xE0|c>>12, 0x80|c>>6&63, 0x80|c&63);
    else bytes.push(0xF0|c>>18, 0x80|c>>12&63, 0x80|c>>6&63, 0x80|c&63); }
  const T = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i+1] ?? 0) << 8) | (bytes[i+2] ?? 0);
    out += T[n>>18&63] + T[n>>12&63] + (i+1 < bytes.length ? T[n>>6&63] : '') + (i+2 < bytes.length ? T[n&63] : '');
  }
  return out;
}
const mime = [
  `From: <email>`, `To: <to>`, `Cc: <cc>`,
  `Subject: Re: <subject>`, `In-Reply-To: <lastMessageId>`, `References: <references> <lastMessageId>`,
  'MIME-Version: 1.0', 'Content-Type: text/plain; charset=UTF-8', 'Content-Transfer-Encoding: 8bit', '', '<body>'
].join('\r\n');
await ok(tools['gmail-api'].gmail.users.drafts.create({ accountId:'<accountId>', input:{ path:{ userId:'me' }, body:{ message:{ threadId:'<threadId>', raw:b64url(mime) } } } }));
```

Leave out the `Cc:` line when there is no cc. A non-ASCII subject is written as `=?UTF-8?B?<standard base64>?=`. A Gmail forward that must carry the original attachment needs a multipart MIME built from `messages.attachments.get`. Until that works for the user, create the forward draft with text only and say in the report that the attachment is not attached.

Edit link for the report: Outlook `webLink`; Gmail `https://mail.google.com/mail/u/<email>/#drafts`.

### Send (only after the user approved that exact draft by number)

- Gmail: `drafts.send`, `path:{ userId:'me' }`, `body:{ id:'<draftId>' }`.
- Outlook: `meMessagesMessageSend`, `path:{ 'message-id':'<draftId>' }`.

## Calendar (read only)

### Outlook calendar

```ts
await ok(tools['outlook-calendar-api'].meEvent.meListCalendarView({ accountId:'<accountId>', input:{ query:{
  startDateTime:'<ISO start>', endDateTime:'<ISO end>',
  $select:['id','subject','start','end','isAllDay','location','organizer','isOrganizer','responseStatus','showAs','isCancelled','webLink'],
  $orderby:['start/dateTime'], $top:100 } } }));
```

`calendarView` expands recurring events. Times come back in UTC: convert them to the user's timezone. Fallback: `meCalendar.meCalendarListEvents` with `$filter:"start/dateTime ge '<start>' and start/dateTime lt '<end>'"` (it does not expand recurring series; do not pass `startDateTime` / `endDateTime` to it). Unanswered invitation: `responseStatus.response` is `none` or `notResponded` and `isOrganizer` is false.

### Google Calendar

```ts
await ok(tools['google-calendar-api'].calendar.events.list({ accountId:'<accountId>', input:{ path:{ calendarId:'primary' },
  query:{ timeMin:'<ISO start>', timeMax:'<ISO end>', singleEvents:true, orderBy:'startTime', timeZone:'<timezone>' } } }));
```

Unanswered invitation: an attendee with `self:true` and `responseStatus:'needsAction'`.

### RSVP (only after a numbered reply)

Outlook `meEvent` accept, decline, tentativelyAccept; Google `events.patch` on the user's own attendee response. Look up the exact path with `search` before the first use. Never create, move, or delete events.

## Approval pauses (`resume`)

A write can come back paused with an `executionId`, waiting for approval.

- Pause on a trash, archive, move, spam, or draft creation that this run decided within `guardrails.md`: `resume` with `accept`, without `persist` (that call only).
- Pause on a send that the user approved by number in this conversation: `resume` with `accept`, without `persist`.
- Pause on anything else (a send nobody approved, a permanent delete, a folder or label change, anything this run did not ask for): `resume` with `decline`, and report it.
- If a pause cannot be resolved, report it. Never look for another way to do what was declined.

## Failures

- `execute` does not respond at all: report `Executor not responding`, act on no mail, write the history line, release the lock.
- One account fails (auth, 401/403, timeout, wrong identity): retry once, then show `<handle> ?` with the reason and continue with the others.
- A calendar fails: show `<handle> cal ? (<reason>)` and continue.
