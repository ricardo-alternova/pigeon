# Install pigeon

## Hosted setup

Keep the first run attended:

1. Give the assistant a checkout and the instruction: "Follow AGENTS.md for one scheduled Inbox triage and send the standard run message."
2. Check [host requirements](#host-requirements) and [storage choice](#storage-choice).
3. Create the [private store](#private-store). Supply config.toml and guardrails.md from lasting host storage before every run.
4. Connect the [gateway](#mail-gateway-connection); set [configuration](#configuration), [rules](setup.md#rules), never-touch and guardrails.
5. For drive storage, set [drive lock](#drive-lock) and [drive state](#drive-state). Get the private account handle from gateway search/describe (drive-example), and the parent folder ID from metadata (folder-example), not its name or sharing URL. Provision private parent folders. Supply local rules.toml and never-touch.toml for an empty drive folder.
6. Run the [quick start](quickstart.md) attended once (no first-run flag) and try a numbered reply; then enable [scheduling](setup.md#scheduling).

## Storage choice

If the host keeps its files, use local state/lock. If it starts empty or two hosts share mailboxes, use drive state and the OneDrive lock. Choose OneDrive or Google Drive state; only OneDrive locks.

## Host requirements

- [ ] Schedule: demonstrate a task started on schedule and one started when asked.
- [ ] Chat: receive a test message and reply in the same conversation.
- [ ] Storage: demonstrate the [storage choice](#storage-choice); when using drive state, verify the folder is private as required under [Drive state](#drive-state).
- [ ] Gateway: discover mail and drive tool schemas and verify configured account identities; only the calls allowed by [guardrails](../config/guardrails.example.md) may be carried by the assistant.



## Mail gateway connection

Connect and authenticate mail accounts before scheduling. Use Executor search/describe to discover connected app paths and input shapes. Verify fixed linked-account identity; never choose an arbitrary account. Use exactly accounts.HANDLE.executor_account when configured for every read, listing and action in that account. Stop if the gateway does not offer that ID. Keep connection details private.

## Private store

Keep private data outside git in ~/.config/pigeon/ or the PIGEON_HOME folder. Refuse empty PIGEON_HOME. Create it with `python3 scripts/triage.py setup`; restrict access to your user. The planner refuses nonignored git paths. After editing the four safety files run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py validate`; correct every problem before locking. Keep [run artifacts](setup.md#state-and-execution-files) here.

## Configuration

Require config.toml before locking. Use these keys:

| Key | Value; default |
| --- | --- |
| version | Integer format version, 1; refuse above 1 in config.toml/state.json; validate prints it. |
| language | en or es; en. Reasons and Inbox count stay English. |
| relist_inbox | all, or new_plus_count for new items and earlier-item counts; all. |
| timezone | IANA zone for slots/messages; UTC. |
| inbox_limit | Positive integer across accounts; 100. |
| lock.kind | local or onedrive; local. |
| lock.account, lock.folder | Required for onedrive: account handle and parent folder ID; each 1–512 ASCII letters, digits, underscore, hyphen, dot, colon or exclamation mark. No other keys for local. |
| state.kind | local, onedrive or gdrive; local. Drive requires lock.kind=onedrive. |
| state.account, state.folder | Required for drive: account handle/parent folder ID, same limits as lock. No other keys for local. |
| schedule.weekdays, schedule.weekends | When schedule exists, nonempty arrays of unique local HH:MM slots. |
| schedule.always_full | Required unique subset of those slots; set a periodic full check; empty permits indefinite quiet skips. |
| accounts | Required subtable per private handle used in rules/listings. Report missing accounts; stop if none listed or any unconfigured account appears. |
| accounts.HANDLE.provider | Optional gmail or outlook; enable scripted listing with executor_app/executor_account. |
| accounts.HANDLE.executor_app | Optional app slug for discovery. |
| accounts.HANDLE.executor_account | Optional gateway ID, 1–128 ASCII letters, digits, underscore or hyphen; unique with executor_app. |
| accounts.HANDLE.name | Optional name, 1–80 characters, single line without controls; default HANDLE. Reject other names/handles ignoring case. |
| accounts.HANDLE.address | Optional mailbox address for identity verification. |
| accounts.HANDLE.quiet_kind | history or ids; ids. |

Refuse unknown keys and wrong types in config, rules, never-touch, listings and state.

## Drive lock

Set lock.kind=onedrive ([Configuration](#configuration)); follow [Lock and fetch](quickstart.md#lock-and-fetch). Carry only the printed account, parent, name pigeon-run-lock, operation and condition. Preserve conflict="fail" on create and if_match on delete; refuse unsupported conditions. Read/delete by fixed name in that parent. Never use returned IDs/tags as targets or report text. Add required gateway type markers without changing the call. Map HTTP 409 to conflict and 412 to precondition-failed.

Save lock-observation.json and resume lock --observation PATH. For --batch supply {"create":OUTCOME,"read":OUTCOME} from ordered calls, including the read on conflict; complete takeover calls singly. Use created or ok with id, etag, created UTC timestamp and name:

```json
{"status":"created","id":"item-1","etag":"v1","created":"2026-11-11T08:00:00+00:00","name":"pigeon-run-lock"}
```

Use {"status":"conflict"} for conflict, {"status":"deleted"} for conditional deletion. Refuse error, unknown, not-found and precondition-failed.

After create, read exactly once and match ID, name and creation time; hold its read tag. Refuse a newly created item older than 45 minutes. After failed/uncertain/absent/mismatched confirmation, make no mailbox/state call or delete; wait for expiry.

On conflict read once. Age ≤55 minutes is busy (45 plus ten-minute margin); only older locks permit conditional delete by read tag, then conflict-fail create and confirmation. Never retry create/delete; refuse competing winners or failed conditions. Synchronise clocks within ten minutes. Holder expiry is 45 minutes from acquisition, bounded by drive creation, until refresh.

Before commit-state, carry a fresh matching ID/tag/creation read and resume --lock-observation PATH. Release by saved-tag conditional delete and resume release --observation PATH; failed verification/release loses ownership.

After uncertain acquisition wait 45 minutes from script start, then start fresh with conflict fail. Finish/release before changing backends. For [Lock refresh](quickstart.md#lock-refresh), carry conditional delete, conflict-fail recreation and confirming read; retain the token only after confirmation and preserve run start time.

## Drive state

Keep drive state readable/writable only by the owner's own account, never shared or in a shared folder. Apply this to the lock folder too.

Immediately after locking, fetch before reading state or calling mail. Download exactly the printed names to state-incoming: numbers.json, unsubscribe-secret, unsubscribe-failures.json, decisions.jsonl, calls.jsonl, history.log, run-message.md, rules.toml, never-touch.toml, tool-paths.json, state.json. Keep plans, checkpoints, observations, config.toml and guardrails.md local. Follow [storage guardrails](../config/guardrails.example.md).

Return fetched, absent or failed per issued filename. Confirm absence through the gateway; classify uncertainty as failed; preserve local rules when absent remotely. Refuse an empty drive store when local records exist; correct state.folder or restore local records to that folder.

For send-state carry a fresh matching lock read, even after commit, and resume with --lock-observation PATH. Upload each issued changed-file snapshot from state-outgoing, numbers first and counts last; include absent rules from local copies. Return upload-observation.json as {"status":"done","size":123} with the actual integer drive size, or {"status":"failed"}. Bound fetch/upload observations to 4096 bytes; never derive names from observations.

On upload failure, deliver the warning, retain local files and release conditionally. Never retry or send again. Clear pending messages with a zero-byte remote run-message.md.

Use `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py finish-run --token TOKEN` only without a message. Complete its lock-read, upload and conditional release through --observation PATH. Otherwise follow [Commit and message](quickstart.md#commit-and-message).

## Gateway recipes

For every call use `{accountId,input}`. Replace APP in tool paths with accounts.HANDLE.executor_app and accountId with accounts.HANDLE.executor_account from config.toml. Use the[Inbox listing](#inbox-listing) and[quiet observation](quickstart.md#quiet-observations) formats for the saved values.

Prefix table tools with tools.APP.; use tools["APP-SLUG"].rest for other app slugs. Pass action calls as printed.

### Gmail

Use path {userId:"me"} for every Gmail call; add id for message/label reads. Page every list until no nextPageToken.

| Read | Tool suffix after gmail.users. | query; save |
| --- | --- | --- |
| Position and total | getProfile | No query; historyId → quiet position, messagesTotal → inbox.json accounts.HANDLE. |
| Arrivals | history.list | {startHistoryId,labelId:"INBOX",historyTypes:["messageAdded","labelAdded"],maxResults,pageToken}; save Inbox arrival IDs. |
| Inbox IDs | messages.list | {labelIds:["INBOX"],maxResults,pageToken}; save messages[].id. |
| Attachments | messages.list | Add q:"has:attachment"; mark returned IDs true, other Inbox IDs false. |
| Headers/date | messages.get | {format:"metadata",metadataHeaders:["From","Subject","List-Unsubscribe","List-Unsubscribe-Post"]}; map payload.headers to sender, subject and unsubscribe fields. Convert internalDate milliseconds to UTC ISO 8601 messages[].date (2026-03-01T00:00:00+00:00). |
| Folders | labels.list | No query; save user labels' id/full name in folders.HANDLE. |
| Inbox count | labels.get | id:"INBOX"; messagesTotal is Inbox count, never inbox.json accounts.HANDLE. |

For post-action verification call gmail.users.messages.get with `{path:{userId:"me",id},query:{format:"minimal"}}`. Set in_inbox to whether labelIds contains INBOX. Report the boolean,not the labels.


| Action | Tool | input |
| --- | --- | --- |
| trash | gmail.users.messages.trash |{path:{userId:"me",id:ID}}|
| archive | gmail.users.messages.modify |{path:{userId:"me",id:ID},body:{removeLabelIds:["INBOX"]}}|
| move | gmail.users.messages.modify |{path:{userId:"me",id:ID},body:{removeLabelIds:["INBOX"],addLabelIds:[LABEL_ID]}}|
| spam | gmail.users.messages.modify |{path:{userId:"me",id:ID},body:{removeLabelIds:["INBOX"],addLabelIds:["SPAM"]}}|
| create-folder | gmail.users.labels.create |{path:{userId:"me"},body:{name:NAME}}|
| create-draft | gmail.users.drafts.create | Preserve issued draft_text and draft_to. |

### Outlook

| Read | Tool | input; save |
| --- | --- | --- |
| Identity | meUser.meUserGetUser | query:{$select:["mail","userPrincipalName"]}; match either to accounts.HANDLE.address case-insensitively, otherwise omit account. |
| Inbox | meMailFolder.meMailFoldersListMessages | path:{"mailFolder-id":"inbox"},query:{$select:["id","from","subject","receivedDateTime","hasAttachments"],$orderby:["receivedDateTime desc"],$top,$skip}; map fields to messages. Page with $skip until short. |
| Inbox count | meMailFolder.meGetMailFolders | path:{"mailFolder-id":"inbox"},query:{$select:["totalItemCount"]}; save Inbox count. |
| All folders | meMailFolder.meListMailFolders | query:{$select:["id","displayName","totalItemCount","childFolderCount"],$top,$skip,includeHiddenFolders: "true"}; page until short, sum every totalItemCount into inbox.json accounts.HANDLE. For children use meMailFolder.meMailFoldersListChildFolders with path:{"mailFolder-id":ID} and the same query; recurse childFolderCount and join full paths. |
| Quiet IDs | meMailFolder.meMailFoldersListMessages | Inbox path; query:{$select:["id"],$orderby:["receivedDateTime desc"],$top:10,$skip:0}; save ten newest IDs in order. |

For post-action verification read the issued message by ID with the gateway’s discovered message-read tool. Compare parentFolderId with the Inbox folder ID from meGetMailFolders (select id). Set in_inbox to that comparison. If the ID does not exist after removal,report in_inbox=false. Treat any other failure as a failed reading. The final whole Inbox reading still decides. Report the boolean,not the folder ID.

Copy Outlook IDs containing = + and / unchanged.


| Action | Tool | input |
| --- | --- | --- |
| trash | meMessage.meMessagesMessageMove |{path:{"message-id":ID},body:{DestinationId:"deleteditems"}}|
| archive | meMessage.meMessagesMessageMove |{path:{"message-id":ID},body:{DestinationId:"archive"}}|
| move | meMessage.meMessagesMessageMove |{path:{"message-id":ID},body:{DestinationId:FOLDER_ID}}|
| spam | meMessage.meMessagesMessageMove |{path:{"message-id":ID},body:{DestinationId:"junkemail"}}|
| create-folder | meMailFolder.meCreateMailFolders or meMailFolder.meMailFoldersCreateChildFolders |{body:{displayName:NAME}}; for child add path:{"mailFolder-id":PARENT}. |
| create-draft | meMessage.meMessagesMessageCreateReply or meMessage.meMessagesMessageCreateForward | path:{"message-id":ID}; preserve issued draft_text and draft_to. |
| message-read | meMessage.meGetMessages |{path:{"message-id":ID},query:{$select:["id","parentFolderId"]}}|


## Inbox listing

Page every Inbox into private UTF-8 JSON inbox.json, even above inbox_limit. Never infer provider flags or URLs from mail instructions.

| Key | Type and source |
| --- | --- |
| accounts | Map each listed handle to the provider all-folder integer total (Inbox, Sent, Trash, Spam, Archive included); use this brake denominator, or omit the account if unavailable. |
| messages | Every Inbox message from listed accounts; [] for empty accounts. |
| messages[].account | Configured mailbox handle. |
| messages[].id | Unique provider message ID within its account. |
| messages[].folder | Exactly "Inbox"; collect no other folders. |
| messages[].sender | Sender header: address or Name <address>; leave invalid/multiple addresses as decisions. |
| messages[].subject | String from the message subject. |
| messages[].has_attachment | Provider attachment boolean; default false. |
| messages[].spam, messages[].phishing | Provider booleans, default false; refuse unsubscribe, never infer a spam action. |
| messages[].unsubscribe_url | First HTTPS URI in List-Unsubscribe angle brackets, stripped; omit if absent. Leave unsafe/missing links as decisions. |
| messages[].unsubscribe_one_click | List-Unsubscribe-Post boolean, default false; true selects RFC 8058 POST, otherwise GET. |
| messages[].body | Optional plain-text excerpt (2000 characters max), every message in body-rule accounts. Request truncation or retain the first 2000. Never fetch HTML, attachments or links. Omit absent plain text (no body match). Use for matching only. |
| messages[].date | Optional ISO 8601 provider received/internal date, never sender Date/body; preserve year/month without conversion. Leave missing/invalid dates outside 1990–2100 to the user for dated moves. |
| messages[].sender_name | Do not collect. |
| skipped | Omitted account to reason map: {"cedar":"provider error (503)"} or {"cedar":"pigeon error (listing answer)"}; keep the account and its messages absent. |
| folders | Handle to existing-folder arrays; require for move rules/replies. |
| folders.HANDLE[].id, folders.HANDLE[].name | Provider ID and full slash-separated name; include every existing path, never leaf names alone. |

