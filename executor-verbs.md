# Executor calls for pigeon

pigeon reaches mail and calendars only through Executor (executor.sh), over its MCP server. The MCP tools you use are `search`, `describe`, `execute`, and `resume`. Every mail and calendar call below runs inside an `execute` call (a JavaScript sandbox).

Replace `<accountId>`, `<email>`, and the other `<...>` values with the user's values from `local/accounts.md`.

## Sandbox basics

- Call form: `tools['<app>'].<api path>({ accountId, input:{ path, query, body } })`. Path parameters (`userId`, `id`, `'message-id'`, `'mailFolder-id'`, `calendarId`) go in `path`; filters and paging (`q`, `maxResults`, `$filter`, `$top`) go in `query`; payloads go in `body`.
- Apps: `gmail-api`, `outlook-mail-api`, `google-calendar-api`, `outlook-calendar-api`.
- On success a tool returns the raw API payload (no wrapper). On failure it throws an `Error` (for example `InputInvalid` 422, `AccountSelectionInvalid`, `OAuthReconnectRequired`). Wrap each account's calls in `try`/`catch` so one failure does not stop the others. The helper below gives every failure a short, readable message for the report:

```js
const ok = async (p) => { try { return await p; } catch (e) { throw new Error(String(e && e.message || e).slice(0, 200)); } };
```

- The sandbox has no `fetch`, `Buffer`, `atob`, `btoa`, or `TextEncoder`. Do not enumerate `tools`.
- If you are unsure of a path or its input shape, check it with `describe` or `search` before calling it.
- Reads and writes go in separate `execute` calls. First read everything and decide, then act on explicit IDs.
- Return compact JSON from each read call (one object per account with only the fields you need), not raw API responses.

## Find the user's accounts

Run `search` with a query like `gmail getProfile` or `outlook mail list messages`. The results list the connected accounts for each app with their `accountId`. Gmail and Google Calendar accounts show their email address. Outlook mail and Outlook Calendar accounts show only generic names (`personalOutlookMailApi`, `personalOutlookMailApi2`, ...) with no address, so you must run the identity call below on each Outlook `accountId` to learn which address it is. Save each `accountId` with its verified address in `local/accounts.md`.

If an account the user wants is missing, ask them to connect it in Executor (Gmail, Outlook mail, Google Calendar, Outlook Calendar), then run `search` again.

## Check identity on every run

Gmail: the address must match `local/accounts.md`.

```js
const p = await ok(tools['gmail-api'].gmail.users.getProfile({ accountId:'<accountId>', input:{ path:{ userId:'me' } } }));
// p.emailAddress === '<email>'
```

Outlook: the address must match `local/accounts.md`.

```js
const u = await ok(tools['outlook-mail-api'].meUser.meUserGetUser({ accountId:'<accountId>', input:{ query:{ $select:['mail','userPrincipalName'] } } }));
// (u.mail || u.userPrincipalName).toLowerCase() === '<email>'
```

Some Outlook accounts ignore `$select` here and return the full user profile. That is harmless: read only `mail` and `userPrincipalName`, and never put the other fields in the report or in `local/`.

Outlook Calendar has the same call: `tools['outlook-calendar-api'].meUser.meUserGetUser`.

Wrong address, `AccountSelectionInvalid`, or `OAuthReconnectRequired`: report `<handle> ?`, skip that account, and run `search` to see the current accounts. Tell the user to reconnect it in Executor.

## Read

### Result size

Each message in these read results is about 0.4 KB (previews are cut to 120 characters, and no links, unsubscribe URLs, or thread IDs are returned). One result therefore holds about 10 messages in 4 KB. Both Inbox reads take `SKIP` and `MAX`: run them with `MAX = 10`, then again with `SKIP` set to the returned `next`, one `execute` call per page, until `next` is `null`. Merge the pages into `local/inbox.json`.

### Gmail Inbox

```js
const A = '<accountId>', G = tools['gmail-api'].gmail.users, SKIP = 0, MAX = 10;
let ids = [], pageToken;
do {
  const list = await ok(G.messages.list({ accountId:A, input:{ path:{ userId:'me' }, query:{ q:'in:inbox', maxResults:100, pageToken } } }));
  ids.push(...(list.messages ?? []).map(m => m.id));
  pageToken = list.nextPageToken;
} while (pageToken);
const msgs = [];
for (const id of ids.slice(SKIP, SKIP + MAX)) {
  const m = await ok(G.messages.get({ accountId:A, input:{ path:{ userId:'me', id }, query:{ format:'metadata',
    metadataHeaders:['From','To','Cc','Subject','Date','List-Unsubscribe','List-Unsubscribe-Post'] } } }));
  const h = Object.fromEntries((m.payload?.headers ?? []).map(x => [x.name, x.value]));
  msgs.push({ id, from:h.From, to:h.To, cc:h.Cc, subject:h.Subject, date:h.Date,
    unsub:/https:/i.test(h['List-Unsubscribe'] || '') ? 'https' : (h['List-Unsubscribe'] ? 'mailto' : null), preview:(m.snippet || '').slice(0, 120) });
}
return { total:ids.length, next:SKIP + MAX < ids.length ? SKIP + MAX : null, msgs };
```

Gmail IDs are short, so `id` is the full ID used to act. `in:inbox` includes the Promotions, Social, and Updates tabs. `unsub` only says which kind of unsubscribe header the message has (`https`, `mailto`, or `null`); the URL itself is fetched on demand.

### Outlook Inbox

Outlook IDs are about 150 characters, so the read returns only a short key `k` (the last 20 characters of the ID). The write call resolves each `k` back to the full ID (see "Write").

```js
const A = '<accountId>', M = tools['outlook-mail-api'].meMessage, SKIP = 0, MAX = 10;
const page = await ok(M.meListMessages({ accountId:A, input:{ query:{
  $top:MAX, $skip:SKIP, $filter:"parentFolderId eq 'inbox'",
  $select:['id','subject','from','toRecipients','ccRecipients','receivedDateTime','bodyPreview'],
  $orderby:['receivedDateTime desc'] } } }));
const msgs = (page.value || []).map(m => ({ k:m.id.slice(-20), from:m.from?.emailAddress?.address,
  to:(m.toRecipients || []).map(x => x.emailAddress?.address), cc:(m.ccRecipients || []).map(x => x.emailAddress?.address),
  subject:m.subject, date:m.receivedDateTime, preview:(m.bodyPreview || '').slice(0, 120) }));
return { next:page['@odata.nextLink'] ? SKIP + MAX : null, msgs };
```

The Inbox includes Focused and Other. Outlook unsubscribe headers are not in this read: fetch them on demand with "Unsubscribe link for one message" (under "Write") only for messages a rule will unsubscribe.

### Body, headers, thread

- Gmail body: `G.messages.get` with `format:'full'`.
- Outlook body and headers: `meMessage.meGetMessages` with `path:{ 'message-id':id }` and `$select:['body','internetMessageHeaders']`. Return only the part you need (a few lines of text, or one header), never the whole body.
- Gmail thread: `G.threads.get` with `path:{ userId:'me', id:threadId }` and `format:'metadata'`. Read only.
- Outlook thread: read the message's `conversationId` with `meGetMessages`, then `meListMessages` with `$filter:"conversationId eq '<id>'"`.

### Drafts

- Gmail: `G.drafts.list` with `path:{ userId:'me' }`, then `G.drafts.get`.
- Outlook: the quick check already counts all drafts (`d`). This read only checks which drafts in `pigeonDrafts` still exist, and returns nothing about the user's own drafts. Without `$top` the list stops at 10; drafts can sit in any folder, not only Drafts.

```js
const A = '<accountId>', M = tools['outlook-mail-api'].meMessage;
const PD = [/* full draft IDs from pigeonDrafts for this account */];
const seen = new Set();
let skip = 0, page;
do {
  page = await ok(M.meListMessages({ accountId:A, input:{ query:{ $top:50, $skip:skip, $filter:'isDraft eq true', $select:['id'] } } }));
  for (const m of (page.value || [])) if (PD.includes(m.id)) seen.add(m.id);
  skip += 50;
} while (page['@odata.nextLink']);
return { present:PD.filter(id => seen.has(id)).map(id => id.slice(-20)), gone:PD.filter(id => !seen.has(id)).map(id => id.slice(-20)) };
```

Read a pigeon draft's full body with `meGetMessages` only when you show it in the report. Drafts the user made are never listed and never touched.

### Read-only search

- Gmail: `G.messages.list` with `q:'from:<domain>'`.
- Outlook: `meListMessages` with `$search:'"from:<domain>"'` (no `$orderby` with `$search`).

### Folders and labels

- Outlook: `meMailFolder.meListMailFolders` with `$top:250`, then `meMailFolder.meMailFoldersListChildFolders` with `path:{ 'mailFolder-id':id }` for each level. Match on `displayName`.
- Gmail: `G.labels.list`. A nested label `Clients/Acme` matches `Acme`.
- Zero or several matches: do not move. Report it. Resolve each folder once per run and reuse the ID.

## Quick check (no new mail?)

Run once per run, read only. `S` is the `accounts` object from `local/state.json`. For each account it saves `{ n, d, total }` plus a cursor (`historyId` for Gmail, `ids` for Outlook): the Inbox message count `n` (for the 0-messages brake in `guardrails.md`) and the count `d` of all unsent drafts in the mailbox, in any folder (a change signal only; the report lists only drafts pigeon created), and the whole-mailbox count `total` (for the 20% brake).

```js
const S = /* accounts from local/state.json */;
const GA = { '<handle>':'<accountId>' };   // Gmail accounts
const OA = { '<handle>':'<accountId>' };   // Outlook accounts
const G = tools['gmail-api'].gmail.users, O = tools['outlook-mail-api'];
const hash = s => { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0; return h.toString(16); };
const now = {}, changed = [];
for (const [h, a] of Object.entries(GA)) { try {
  const p = await ok(G.getProfile({ accountId:a, input:{ path:{ userId:'me' } } }));
  const inbox = await ok(G.labels.get({ accountId:a, input:{ path:{ userId:'me', id:'INBOX' } } }));
  let added = -1;
  if (S[h]?.historyId) { try {
    const hl = await ok(G.history.list({ accountId:a, input:{ path:{ userId:'me' }, query:{ startHistoryId:S[h].historyId, labelId:'INBOX', historyTypes:['messageAdded','labelAdded'], maxResults:50 } } }));
    added = (hl.history || []).length; } catch (e) { added = -1; } }
  let d = 0, pageToken;
  do { const dr = await ok(G.drafts.list({ accountId:a, input:{ path:{ userId:'me' }, query:{ maxResults:100, pageToken } } }));
    d += (dr.drafts || []).length; pageToken = dr.nextPageToken; } while (pageToken);
  now[h] = { historyId:p.historyId, n:inbox.messagesTotal, d, total:p.messagesTotal };
  if (added !== 0 || now[h].n !== S[h]?.n || d !== S[h]?.d) changed.push(h);
} catch (e) { now[h] = { err:String(e.message).slice(0,120) }; changed.push(h + '?'); } }
const pages = async (a, query) => { const out = []; let skip = 0, page;
  do { page = await ok(O.meMessage.meListMessages({ accountId:a, input:{ query:{ ...query, $top:100, $skip:skip } } }));
    out.push(...(page.value || [])); skip += 100; } while (page['@odata.nextLink']);
  return out; };
for (const [h, a] of Object.entries(OA)) { try {
  const v = await pages(a, { $filter:"parentFolderId eq 'inbox'", $select:['id'], $orderby:['receivedDateTime desc'] });
  const dr = await pages(a, { $filter:'isDraft eq true', $select:['id'] });
  const all = await ok(O.meMessage.meListMessages({ accountId:a, input:{ query:{ $count:true, $top:1, $select:['id'] } } }));
  now[h] = { ids:hash(v.map(x => x.id.slice(-24)).join(',')), n:v.length, d:dr.length, total:all['@odata.count'] };
  if (now[h].ids !== S[h]?.ids || now[h].d !== S[h]?.d) changed.push(h);
} catch (e) { now[h] = { err:String(e.message).slice(0,120) }; changed.push(h + '?'); } }
return { changed, now };
```

- `changed` empty: no new mail and no draft changes.
- Anything in `changed`, or a `?` (failed or expired cursor): full run.
- At the end of a full run, run it again after acting and save `now` to `local/state.json` as `accounts`, so this run's own moves do not trigger the next run.
- Outlook does not take `$deltatoken` in Executor (422). The Inbox ID hash plus the draft count is the cursor.
- `total` is the whole mailbox count for both providers (Gmail `messagesTotal` from `getProfile`, Outlook `@odata.count`), used by the 20% brake.
- The block returns only counts and hashes, so it stays small. Do not add message lists to it.

## Write

| Action | Gmail (`tools['gmail-api'].gmail.users`) | Outlook (`tools['outlook-mail-api'].meMessage`) |
|---|---|---|
| trash | `messages.trash`, `path:{ userId:'me', id }` | `meMessagesMessageMove`, `path:{ 'message-id':id }`, `body:{ DestinationId:'deleteditems' }` |
| archive | `messages.modify`, `body:{ removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:'archive'` |
| move to folder | `messages.modify`, `body:{ addLabelIds:[labelId], removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:<folderId>` |
| spam | `messages.modify`, `body:{ addLabelIds:['SPAM'], removeLabelIds:['INBOX'] }` | `meMessagesMessageMove`, `DestinationId:'junkemail'` |
| several at once | `messages.batchModify`, `body:{ ids:[...], addLabelIds, removeLabelIds }`, only with explicit IDs read and evaluated in this run | one move per ID |

- Outlook: resolve each short key `k` to its full ID inside the same write call, then act:

```js
const A = '<accountId>', M = tools['outlook-mail-api'].meMessage;
const K = [/* short keys k from local/inbox.json */];
const full = [];
let skip = 0, page;
do {
  page = await ok(M.meListMessages({ accountId:A, input:{ query:{ $top:100, $skip:skip, $filter:"parentFolderId eq 'inbox'", $select:['id'] } } }));
  full.push(...(page.value || []).map(m => m.id));
  skip += 100;
} while (page['@odata.nextLink']);
const resolved = {};
for (const k of K) { const hits = full.filter(id => id.endsWith(k)); resolved[k] = hits.length === 1 ? hits[0] : null; }
// act only on keys whose resolved[k] is not null; a null key left the Inbox or is ambiguous: report it as not done
```

- Outlook move returns the message with a **new ID**. Do not reuse the old one.
- After acting, re-read the Inbox and confirm each ID left it.
- Never call: `messages.delete`, `messages.batchDelete`, `threads.trash`, `threads.modify`, `meDeleteMessages`, any label create, update, or delete, any mail folder create, update, or delete.

### Unsubscribe

Executor has no unsubscribe call. Make the HTTPS request yourself, outside the Executor sandbox, with your own HTTP tool or browser.

1. **Header with an HTTPS URL** (`List-Unsubscribe`): send a one-click POST (form body `List-Unsubscribe=One-Click`) when `List-Unsubscribe-Post` says `One-Click`, otherwise a GET of the URL.
2. **Header with only `mailto:`:** do not send it. Log `mailto only`.
3. **No header:** look in that one message's body for an HTTPS link whose URL or link text says unsubscribe or opt out, in any language (`unsubscribe`, `opt out`, `opt-out`, `darse de baja`, `desuscribirse`, `cancelar suscripción`). If there is one, open it with a single GET. If there is none, log `no link`.
4. If the page asks for a login, a captcha, personal details, or more than one confirm click, stop and log `failed: <reason>`.

In every case, do the rest of the rule's action (trash or move) in the same run. Never unsubscribe from phishing or spam.

#### Unsubscribe link for one message

Run this only for messages a rule will unsubscribe. It returns only the unsubscribe header, the one-click flag, and at most one body link.

```js
const WORDS = /unsubscribe|opt[\s-]?out|darse de baja|desuscrib|cancelar (la )?suscripci/i;
const firstLink = (html) => { const re = /<a\b[^>]*href=["'](https:[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi; let m;
  while ((m = re.exec(html))) if (WORDS.test(m[1]) || WORDS.test(m[2].replace(/<[^>]+>/g, ''))) return m[1].replace(/&amp;/g, '&');
  return null; };
// Outlook: <fullId> is the full ID, resolved from the short key k as in "Write"
const om = await ok(tools['outlook-mail-api'].meMessage.meGetMessages({ accountId:'<accountId>', input:{ path:{ 'message-id':'<fullId>' }, query:{ $select:['body','internetMessageHeaders'] } } }));
const hd = n => ((om.internetMessageHeaders || []).find(x => x.name.toLowerCase() === n) || {}).value || null;
const outlook = { unsub:hd('list-unsubscribe'), oneClick:/One-Click/i.test(hd('list-unsubscribe-post') || ''), bodyLink:firstLink(om.body?.content || '') };
// Gmail: the body parts are base64url; the sandbox has no atob, so decode in plain JS
const b64urlToText = (s) => { const T = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'; const bytes = []; let buf = 0, bits = 0;
  for (const ch of s.replace(/[=\s]/g, '').replace(/\+/g, '-').replace(/\//g, '_')) { const v = T.indexOf(ch); if (v < 0) continue;
    buf = (buf << 6) | v; bits += 6; if (bits >= 8) { bits -= 8; bytes.push((buf >> bits) & 255); } }
  let out = '', i = 0;
  while (i < bytes.length) { const c = bytes[i++];
    if (c < 0x80) out += String.fromCharCode(c);
    else if (c < 0xE0) out += String.fromCharCode(((c & 31) << 6) | (bytes[i++] & 63));
    else if (c < 0xF0) out += String.fromCharCode(((c & 15) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63));
    else { const cp = ((c & 7) << 18) | ((bytes[i++] & 63) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63); out += String.fromCodePoint(cp); } }
  return out; };
const gm = await ok(tools['gmail-api'].gmail.users.messages.get({ accountId:'<accountId>', input:{ path:{ userId:'me', id:'<id>' }, query:{ format:'full' } } }));
const gh = n => ((gm.payload?.headers || []).find(x => x.name.toLowerCase() === n) || {}).value || null;
const parts = []; const walk = p => { if (!p) return; if (p.mimeType === 'text/html' && p.body?.data) parts.push(b64urlToText(p.body.data)); (p.parts || []).forEach(walk); };
walk(gm.payload);
const gmail = { unsub:gh('list-unsubscribe'), oneClick:/One-Click/i.test(gh('list-unsubscribe-post') || ''), bodyLink:firstLink(parts.join('\n')) };
// For an Outlook message run only the Outlook lines and return { outlook }; for a Gmail message run only the Gmail lines and return { gmail }.
```

## Drafts and sending

### Outlook

```js
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

```js
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

```js
await ok(tools['outlook-calendar-api'].meEvent.meListCalendarView({ accountId:'<accountId>', input:{ query:{
  startDateTime:'<ISO start>', endDateTime:'<ISO end>',
  $select:['id','subject','start','end','isAllDay','location','organizer','isOrganizer','responseStatus','showAs','isCancelled','webLink'],
  $orderby:['start/dateTime'], $top:100 } } }));
```

`calendarView` expands recurring events. Times come back in UTC: convert them to the user's timezone. Fallback: `meCalendar.meCalendarListEvents` with `$filter:"start/dateTime ge '<start>' and start/dateTime lt '<end>'"` (it does not expand recurring series; do not pass `startDateTime` / `endDateTime` to it). Unanswered invitation: `responseStatus.response` is `none` or `notResponded` and `isOrganizer` is false.

### Google Calendar

```js
await ok(tools['google-calendar-api'].calendar.events.list({ accountId:'<accountId>', input:{ path:{ calendarId:'primary' },
  query:{ timeMin:'<ISO start>', timeMax:'<ISO end>', singleEvents:true, orderBy:'startTime', timeZone:'<timezone>' } } }));
```

Unanswered invitation: an attendee with `self:true` and `responseStatus:'needsAction'`.

### RSVP (only after a numbered reply)

Outlook `meEvent` accept, decline, tentativelyAccept; Google `events.patch` on the user's own attendee response. Look up the exact path with `search` before the first use. Never create, move, or delete events.

## Approval pauses (`resume`)

A write can come back paused with a `requestId` (`apr_...` or `elc_...`), waiting for approval. Answer it with `resume`, passing that `requestId` and a `response`. Accept only that one request; never accept a standing or persistent permission.

- Pause on a trash, archive, move, spam, or draft creation that this run decided within `guardrails.md`: `resume` with `{ action:'accept' }`.
- Pause on a send that the user approved by number in this conversation: `resume` with `{ action:'accept' }`.
- Pause on anything else (a send nobody approved, a permanent delete, a folder or label change, anything this run did not ask for): `resume` with `{ action:'decline' }`, and report it.
- If a pause cannot be resolved, report it. Never look for another way to do what was declined.

## Failures

- `execute` does not respond at all: report `Executor not responding`, act on no mail, write the history line, release the lock.
- One account fails (auth, 401/403, timeout, wrong identity): retry once, then show `<handle> ?` with the reason and continue with the others.
- A calendar fails: show `<handle> cal ? (<reason>)` and continue.
