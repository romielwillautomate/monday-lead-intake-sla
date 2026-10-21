# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 65 of 65 checks pass: every scenario, the text of the Slack alert for each failure path, two checks on every Monday request (schema and headers), one check that exactly the failed runs raised an alert, and two checks on the workflow file

## How it was tested

No real Monday.com account or Slack workspace was used. The test script:

1. starts a mock Monday.com GraphQL API and mock Slack incoming webhooks on the test machine,
2. imports a temporary copy of the error alert workflow and three temporary copies of the lead workflow (the normal one, one where everyone is away, one with a broken `OWNERS_JSON`), with the Monday host pointed at the mock and test values for every variable,
3. activates all of them and makes the alert workflow the **Error Workflow** of the three copies, the same way the README tells you to set it up,
4. sends form posts and Run Now calls like a real form and a real user would, including two copies of one submission at the same moment,
5. waits for each n8n run to finish, then checks every request the workflow made, what the form got back, the node each run ended on and the Slack alert the error workflow posted for it,
6. deletes the temporary copies.

The schedule trigger in the temporary copy is set to fire only on 1 January at 03:00, so it cannot interfere; the test drives the same SLA logic through Run Now, and a separate check confirms that the real file runs it every 30 minutes.

### What the mock Monday API enforces

The mock is a real GraphQL server (graphql-core) with a schema subset written from Monday's API reference: `items_page_by_column_values`, `next_items_page`, `create_item`, `change_multiple_column_values`, `create_update` and the column value types used here. So it rejects what Monday rejects:

- a missing or wrong `Authorization` token (401), a body that is not JSON or has no `query` (400), an invalid `API-Version`, variables sent as a string instead of an object,
- GraphQL syntax errors, unknown fields or arguments, wrong variable types and unused variables (answered with HTTP 200 and an `errors` list, like Monday),
- `column_values` that is not a JSON string, an unknown column ID (`InvalidColumnIdException`), and a value in the wrong format for its column type, such as an email without `text`, a phone without `countryShortName`, a status label that does not exist, or a user who is not in the account (`ColumnValueException`, HTTP 200, with `error_data.column_type` such as `PhoneColumn`, the way Monday names it),
- a phone number that Google's libphonenumber does not accept for its country. Monday's phone column docs name libphonenumber as the check, and the mock uses the same library (the `phonenumbers` package), so `+1 555 201 0123` is refused just as Monday refuses it,
- an item name over 255 characters, an unknown board, group or item, a `limit` over 500, and `columns` together with `cursor`.

It also behaves like Monday where it matters for this workflow:

- a request with several fields returns partial data: the fields that worked plus an `errors` entry with the `path` of the one that failed,
- email search matches the column's whole display text exactly,
- phone search takes one number in digits only and returns every item that *contains* it (a value with `+` or dashes matches nothing),
- status search matches the label text,
- results come in pages with an opaque cursor for `next_items_page`,
- a mutation with an `Idempotency-Key` that was already used gets the first answer replayed with `Idempotency-Replayed: true`, and a second request with the same key while the first is still running gets `409 IDEMPOTENCY_CONFLICT` with `Retry-After`, as in Monday's idempotency guide,
- answers with HTTP 200 are kept under their key, refusals included. Monday's guide does not say whether it keeps refusals, so the mock takes the stricter case; 429 and 5xx answers are not kept, because the request never ran,
- the complexity budget error is a 429 with the body from Monday's migration guide (`COMPLEXITY_BUDGET_EXHAUSTED`, `retry_in_seconds`) and a `Retry-After` header.

Faults are injected on purpose for some scenarios: a 429, a 500 three times in a row, a `ColumnValueException` with HTTP 200, an `InvalidColumnIdException` on the SLA query, a create that succeeds on Monday's side but whose answer is replaced by a 504 (once, and three times in a row), and a create that takes 1.5 seconds so a second delivery arrives while it runs. The run made 76 Monday requests. Apart from the injected faults, every one had to pass the schema and format checks, and the "Every Monday request" checks confirm none was refused and every one had the right headers.

The mock Slack webhooks accept only `application/json` with a non-empty `text`, answer `ok`, and answer `404 no_service` for a webhook that is gone, like Slack's incoming webhooks. The error alert workflow posts to its own mock webhook, so the test can tell alerts apart from the team's messages and match each alert to its run through the execution link at the end of the alert.

## Found while building

Testing turned up three n8n behaviours that changed the design. None of them shows up in a quick manual test.

| What testing showed | Evidence | Change |
|---|---|---|
| When a Code node throws before the webhook has answered, n8n answers the form with **200 and an empty body**, so the form thinks the lead was saved | A probe workflow: the failing run returned `200` with no body, the run itself ended in error | Config problems are caught in "Normalize Lead" and routed to a 503; the Code nodes before the answer never throw. The "OWNERS_JSON is broken" check proves it |
| With several items in one HTTP node and `retryOnFail`, n8n only checks the **first** item to decide on a retry. If the first Slack message fails, the whole node runs again and the messages that worked are sent again; if a later one fails, it is not retried | With Sam's webhook first in line and broken, the Slack calls were `sam 404, team 200, sam 404, team 200, sam 404, team 200`: Alex got the same nudge three times. With Sam second, Sam's nudge was tried only once | SLA messages are sent one per loop pass ("Each Alert"). The final run shows `sam 404, sam 404, sam 404, team 200` |
| n8n splits a message thrown in a Code node at ": " and keeps only the last part | "Monday refused the SLA query: InvalidColumnIdException: This column ID doesn't exist for the board" arrived as "This column ID doesn't exist for the board [line 8]" | Thrown messages are written without ": ". The alert check on the SLA query proves the whole reason now arrives |

## Found in review

A second pass over the first build compared every call with Monday's docs and tried to break the workflow. These changed the design:

| What was wrong | Evidence | Change |
|---|---|---|
| **Real Monday would have refused every old test lead with a phone number, and any real lead with a mistyped one.** Monday checks phone numbers with libphonenumber. The workflow accepted any 10 digit US number, and the first build's mock accepted it too, so a typo or a made-up area code (all the old test numbers used area code 555) made Monday refuse the whole `create_item`. The lead was never saved and the form got 503 on every retry | `phonenumbers.is_valid_number` is `False` for `+15552010101`; with the mock checking like Monday, the old test leads with a phone could not be saved | Test numbers now use the fictional 202 555 01XX range. When Monday refuses only the phone or the email, "Check Create" sends the item again without it and keeps the value in the message, and the owner is asked to check it. For a returning lead the contact fields go in their own `change_multiple_column_values`, so a refused phone does not block the inquiry or the refresh |
| **Two deliveries of one new lead at the same moment made two items.** The `Idempotency-Key` contained the n8n execution ID, so a form that timed out and redelivered while the first run was still working sent a different key. Neither run had found the lead yet, so both created one | Monday's idempotency guide says webhook driven mutations should derive the key from the event | The key is built from the submission (`lead-<fingerprint>-a<attempt>-create`). The concurrent check shows `200, 409, 200 replay` for the three create calls and one item. `attempt` only goes up after Monday refused the lead with an answer ("Note Failed Try"), so a refusal kept under the old key cannot block the next try |
| **The error alert workflow was never run in the test,** so "you get an alert" was not proven anywhere, and n8n 2.x skips an inactive error workflow | The first test did not import `error-alert-slack.json` | The test imports and activates it, sets it as the Error Workflow, checks the text of the alert for each failure path, and checks that exactly the failed runs raised an alert. README and setup guide now say to activate it |
| **The SLA paging was only tested with two pages,** which cannot show that the loop keeps the items of earlier pages | Page 2 reads the list from the first loop pass either way | The SLA check now reads three pages (100, 100, 21) and the overdue leads sit on the last one |
| `group_id: null` was sent when `MONDAY_GROUP_ID` was empty, and the alert for an unsaved lead carried internal fields | Request dump | `group_id` is only sent when set; the alert shows the lead's own fields |

One mock change was needed for stable runs: in one run a Slack request through Docker's host bridge never reached the mock (n8n saw a 15 second timeout on a kept-alive connection), which made the "retried 3 times" check flaky. The mock now closes the connection after each answer, and the following full runs all passed.

## Results

```
Lead webhook, no secret header
  PASS  refused with 401, nothing sent to Monday or Slack  (401, ended at Stop: Bad Secret)
  PASS  the error workflow posts an alert naming the header  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Bad Secret*)

Lead webhook, wrong secret
  PASS  refused with 401, nothing sent to Monday or Slack  (401, ended at Stop: Bad Secret)
  PASS  the error workflow posts an alert naming the header  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Bad Secret*)

Lead with no email and no phone
  PASS  refused with 422 and the reason, Monday not called  (422 {'ok': False, 'error': 'invalid_lead', 'details': ['an email or a phone number is required']})

Lead with a bad email and a bad phone
  PASS  refused with 422 naming both fields  (422 ['email is not a valid address', 'phone is not a full phone number'])

New lead
  PASS  answered 200 created with the new item ID  (200 {'ok': True, 'status': 'created', 'itemId': '9000000005'})
  PASS  one search by email (lowercase and as typed) and by phone (digits only, no country code)  ({"emails": ["jamie.rivera@example.com", "Jamie.Rivera@Example.com"], "phones": ["2025550101"]})
  PASS  create_item with column values as one JSON string, status New, owner Alex, in the configured group  (column_values: {"status":{"label":"New"},"email_mkv1a2b3":{"email":"jamie.rivera@example.com","text":"jam...)
  PASS  every Monday call has the token, API-Version 2026-10 and a JSON body; the create has an Idempotency-Key built from the submission  (2 calls, key lead-1zqb7e2jv2g-a0-create)
  PASS  one Slack message in the team channel that mentions Alex and links the item  (<@U0ALEX> New lead for you: *Jamie Rivera*)

Same submission delivered twice
  PASS  answered 200 duplicate, no Monday call, no Slack message  (200 {'ok': True, 'status': 'duplicate', 'itemId': '9000000005'})

Returning lead (same email, new question)
  PASS  no new item: one update on the existing item, HTML escaped  (<p><strong>New inquiry from the website:</strong> Can you also check the gutters? &lt;b&gt;Urgent&lt;/b&gt; &a)
  PASS  refreshes only last inquiry and source; owner, status and contact details untouched  (refreshed ['date_mkv1l2m3', 'text_mkv1j0k1'])
  PASS  the owner (Alex) hears about it in Slack  (<@U0ALEX> Returning lead: *Jamie Rivera* sent a new inquiry (matched by email).)

Round robin
  PASS  second lead goes to Sam on his own webhook, third skips Pat (away) and goes to Alex  (owners ['Alex Rivera', 'Sam Lee', 'Alex Rivera'])

Returning lead (phone saved as 2025550177)
  PASS  matched by phone, the empty email is filled in, no new item, Sam told  ({'ok': True, 'status': 'updated', 'itemId': '9000000002'}, filled ['email_mkv1a2b3'])

Similar phone number, different person
  PASS  Monday returns the German number as a phone hit, the workflow rejects it and creates a new item  (1 phone hit, {'ok': True, 'status': 'created', 'itemId': '9000000008'})

Visitor text with Slack mentions
  PASS  escaped, so it cannot ping the channel, a user or post a fake link  (<@U0ALEX> New lead for you: *&lt;!channel&gt; Eve Mallory*)

Phone number Monday refuses (libphonenumber)
  PASS  Monday refuses the item; it is created again without the phone, which goes into the message  (2 create calls, 200 {'ok': True, 'status': 'created', 'itemId': '9000000011'})
  PASS  the owner is told to check the number; no alert, the lead is saved  (Monday did not accept the phone number (555) 201-0123, so the lead was saved without it. You find it in the message; please check with the lead.)

Returning lead with a phone Monday refuses
  PASS  Monday answers with partial data: update and refresh saved, the phone refused; answered 200 updated, Sam warned  ({'ok': True, 'status': 'updated', 'itemId': '9000000004'}, errors on [['fill']])

Monday complexity budget exhausted once (429)
  PASS  retried, then saved normally  (search attempts [429, 200])

Monday down (500)
  PASS  retried 3 times, the form gets 503 with Retry-After, the run fails with the lead in the message  (503, ended at Stop: Lead Not Saved)
  PASS  the Slack alert carries the whole lead, so it can be entered by hand  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Lead Not Saved*)

Monday back, the form retries the same submission
  PASS  saved once (the failed try was not remembered as done)  (200 {'ok': True, 'status': 'created', 'itemId': '9000000013'})

Monday answers 200 with a ColumnValueException
  PASS  not retried (it would fail again), the form gets 503, no Slack message  (ended at Stop: Lead Not Saved)
  PASS  the Slack alert quotes Monday's error  (Lead NOT saved to Monday, so the form was told to retry (503). Reason: Monday did not crea)

The form retries after the fix
  PASS  saved once with a new Idempotency-Key (Monday may have kept the refusal under the old one), same owner as the failed try  (keys -a0-create then -a1-create, owner Alex Rivera)

Monday creates the item but the answer is lost (504)
  PASS  the retry sends the same Idempotency-Key, monday replays it, one item  (2 create calls, 1 item(s))

Monday saves the item but every answer is lost
  PASS  the form gets 503; the only item is the one Monday made on the first try  (503, 3 create calls)
  PASS  the form's retry finds that item and logs the inquiry on it: still one item  (200 {'ok': True, 'status': 'updated', 'itemId': '9000000016'})

Same new lead delivered twice at the same moment
  PASS  one item: the second create gets 409 while the first runs, then Monday's replay  (create calls [(200, ''), (409, ''), (200, 'replay')])
  PASS  both Slack messages (one per delivery) point the same owner to the same item  (2 Slack messages)

Slack refuses the owner alert
  PASS  the lead is still saved and answered 200; Slack retried 3 times; run flagged  (ended at Stop: Notify Failed)
  PASS  the Slack alert names the item and says the SLA check still nudges  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Notify Failed*)

Form tool that cannot set headers, wrong ?key=
  PASS  refused with 401  (401)

Form tool that cannot set headers, right ?key=
  PASS  urlencoded post accepted; first and last name joined, comments kept as the message  (200 {'ok': True, 'status': 'created', 'itemId': '9000000019'})

Round robin over all new leads
  PASS  Alex, Sam, Alex, Sam ... (Pat is away and never picked, failed tries skip nobody)  (Alex, Sam, Alex, Sam, Alex, Sam, Alex, Sam, Alex, Sam, Alex, Sam, Alex)

Run Now, wrong secret
  PASS  refused with 401, Monday not called  (401, ended at Stop: Bad Run Secret)
  PASS  the error workflow posts an alert  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Bad Run Secret*)

SLA check
  PASS  Run Now answers 202, then reads every New item page by page (3 pages, each cursor passed on)  (['NewLeads', 'NextLeads', 'NextLeads'], items per page [100, 100, 21])
  PASS  Alex is nudged once for Avery (2.5 h, on the last page), nothing for Drew (1 h) or Emery (not New)  (<@U0ALEX> This lead is still New after more than 2 h. Please reach out: | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000220|Avery Stone>, waiting 2.5 h)
  PASS  Sam is nudged on his own webhook for Blake (3 h)  (<@U0SAM> This lead is still New after more than 2 h. Please reach out: | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000221|Blake Hunter>, waiting 3.0 h)
  PASS  the manager gets one escalation for Casey (5 h, past twice the SLA), naming the owner  (<@U0MANAGER1> *SLA missed:* 1 lead is still New after more than 4 h. | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000222|Casey Fox>, owner <@U0ALEX>, waiting 5.0 h)
  PASS  leads with no owner, an owner outside OWNERS_JSON or an owner who is away go to the team channel  (*3 leads need an owner.* Still New after more than 2 h, and the owner is missing, not in OWNERS_JSON or away: | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000225|Finley Moss>, owner nobody, waiting 2.5 h | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000226|Gray Lin>, owner Dana Cole, waiting 2.5 h | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000227|Harper Quinn>, owner Pat Kim (away), waiting 2.5 h)

SLA check again right away
  PASS  no reminder is repeated  (0 Slack messages)

SLA check after Avery passes twice the SLA
  PASS  one escalation for Avery only, nothing else  (<@U0MANAGER1> *SLA missed:* 1 lead is still New after more than 4 h. | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000220|Avery Stone>, owner <@U0ALEX>, waiting 4.5 h)

SLA check once more
  PASS  the escalation is not repeated either  (0 Slack messages)

SLA check while Sam's Slack webhook is gone
  PASS  Sam's nudge is retried 3 times, Alex is nudged exactly once, run flagged  (Slack calls [('sam', 404), ('sam', 404), ('sam', 404), ('team', 200)], ended at Stop: SLA Alert Failed)
  PASS  the Slack alert says one reminder failed and will be sent again  (Slack refused 1 SLA alert(s). They are sent again on the next run. 404 - "no_ser)

SLA check after Slack recovers
  PASS  only Sam's missed nudge is sent; Alex is not nudged twice  (<@U0SAM> This lead is still New after more than 2 h. Please reach out: | - <https://acme-demo.monday.com/boards/5550001111/pulses/9000000228|Jules Kerr>, waiting 2.5 h)

SLA memory
  PASS  a lead that left New is forgotten; the others are remembered with the time of each reminder  (7 remembered)

SLA check, Monday answers 200 with an error
  PASS  the run stops with the error, no reminders  (ended at Collect Page: Monday refused the SLA query. InvalidColumnIdException (This)
  PASS  the Slack alert keeps the whole reason (no text lost at ": ")  (Monday refused the SLA query. InvalidColumnIdException (This column ID doesn't exist for t)

SLA check, Monday down (500)
  PASS  retried 3 times, the run stops, no reminders  (ended at Stop: SLA Query Failed)
  PASS  the Slack alert says no reminders were sent  (:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: SLA Query Failed*)

Every Monday request
  PASS  passed the mock schema and format checks (no unknown fields, wrong types or bad variables)  (76 requests)
  PASS  sent the token, API-Version 2026-10 and JSON; every mutation and only mutations sent an Idempotency-Key built from the submission  (0 with wrong headers)

Error alert workflow
  PASS  every failed run raised exactly one Slack alert, and no run that worked or was refused on purpose (422, duplicate) raised one  (12 alerts for 12 failed runs out of 39 runs)

Everyone in OWNERS_JSON is away
  PASS  the lead is created without an owner and the team channel is asked to assign it  (*New lead with no owner:* *Lane Brooks*. Everyone in OWNERS_JSON is ma)

OWNERS_JSON is broken
  PASS  the form gets 503 (not an empty 200), Monday is not called  (503, ended at Stop: Lead Not Saved)
  PASS  the Slack alert says which variable is wrong and carries the lead  (Lead NOT saved to Monday, so the form was told to retry (503). Reason: Config problem: OWN)

Workflow file
  PASS  the schedule runs every 30 minutes and feeds the same SLA logic as Run Now
  PASS  no secrets in the file, every API call retries and has an error output  (7 HTTP nodes)

65/65 checks passed
```

## What the workflow sent (from the test run)

**Monday: find lead** (one request, two lookups). The email is sent lowercase and as typed; the phone as digits without the home country code:

```graphql
query FindLead($board: ID!, $cols: [String!], $emailCol: String!, $emails: [String]!, $phoneCol: String!, $phones: [String]!) {
  by_email: items_page_by_column_values(
    board_id: $board
    limit: 25
    columns: [{ column_id: $emailCol, column_values: $emails }]
  ) {
    items {
      id
      name
      url
      created_at
      column_values(ids: $cols) {
        id
        text
        value
      }
    }
  }
  by_phone: items_page_by_column_values(
    board_id: $board
    limit: 25
    columns: [{ column_id: $phoneCol, column_values: $phones }]
  ) {
    items {
      id
      name
      url
      created_at
      column_values(ids: $cols) {
        id
        text
        value
      }
    }
  }
}
```

```json
{"board": "5550001111", "cols": ["email_mkv1a2b3", "phone_mkv1c4d5", "status", "person", "text_mkv1g8h9"], "emailCol": "email_mkv1a2b3", "emails": ["jamie.rivera@example.com", "Jamie.Rivera@Example.com"], "phoneCol": "phone_mkv1c4d5", "phones": ["2025550101"]}
```

**Monday: create lead** (headers `Authorization`, `API-Version: 2026-10`, `Idempotency-Key: lead-1zqb7e2jv2g-a0-create`):

```graphql
mutation CreateLead($board: ID!, $group: String!, $name: String!, $values: JSON!) {
  create_item(
    board_id: $board
    group_id: $group
    item_name: $name
    column_values: $values
  ) {
    id
    name
    url
  }
}
```

`$values` is one JSON string:

```json
{"status": {"label": "New"}, "email_mkv1a2b3": {"email": "jamie.rivera@example.com", "text": "jamie.rivera@example.com"}, "phone_mkv1c4d5": {"phone": "+12025550101", "countryShortName": "US"}, "person": {"personsAndTeams": [{"id": 41000001, "kind": "person"}]}, "long_text_mkv1e6f7": {"text": "Need a quote for a new roof.\nThe house is two floors."}, "text_mkv1g8h9": "Rivera Roofing", "text_mkv1j0k1": "Website contact form", "date_mkv1l2m3": {"date": "2026-10-20", "time": "20:06:06"}}
```

**Monday: create lead again without the phone.** Monday refused `+15552010123` with a `ColumnValueException` on the `PhoneColumn`; the second try (`Idempotency-Key: lead-1p3qzg8d6oh-a0-create-without-phone`) sent these `$values`:

```json
{"status": {"label": "New"}, "email_mkv1a2b3": {"email": "ari.novak@example.com", "text": "ari.novak@example.com"}, "person": {"personsAndTeams": [{"id": 41000002, "kind": "person"}]}, "long_text_mkv1e6f7": {"text": "Need a new water heater\n\nPhone (Monday did not accept it): (555) 201-0123"}, "text_mkv1j0k1": "Website contact form", "date_mkv1l2m3": {"date": "2026-10-20", "time": "20:06:09"}}
```

**Monday: update lead.** The same person comes back with a new question and a phone number the item does not have yet. Monday refused the number, so `fill` failed while `update` and `refresh` were saved. The request, then the mock's answer (`mock_reason` is a debug field only the mock adds; Monday sends the same generic message for every column):

```graphql
mutation UpdateLead($item: ID!, $body: String!, $board: ID!, $refresh: JSON!, $fill: JSON!) {
  update: create_update(item_id: $item, body: $body) {
    id
  }
  refresh: change_multiple_column_values(
    board_id: $board
    item_id: $item
    column_values: $refresh
  ) {
    id
  }
  fill: change_multiple_column_values(
    board_id: $board
    item_id: $item
    column_values: $fill
  ) {
    id
  }
}
```

```json
{"item": "9000000004", "body": "<p><strong>New inquiry from the website:</strong> Can we move the visit to Friday?</p><p>Name: Jordan Blake<br>Email: jordan.blake@example.com<br>Phone: +15552010144<br>Source: Booking form<br>Received: 2026-10-20 20:06 UTC</p>", "board": "5550001111", "refresh": "{\"date_mkv1l2m3\":{\"date\":\"2026-10-20\",\"time\":\"20:06:10\"},\"text_mkv1j0k1\":\"Booking form\"}", "fill": "{\"phone_mkv1c4d5\":{\"phone\":\"+15552010144\",\"countryShortName\":\"US\"}}"}
```

```json
{
  "data": {
    "update": {
      "id": "7000000003"
    },
    "refresh": {
      "id": "9000000004"
    },
    "fill": null
  },
  "errors": [
    {
      "message": "invalid value, please check our API documentation for the correct data structure for this column. https://developer.monday.com/api-reference/docs/change-column-values",
      "locations": [
        {
          "line": 1,
          "column": 258
        }
      ],
      "path": [
        "fill"
      ],
      "extensions": {
        "code": "ColumnValueException",
        "status_code": 200,
        "error_data": {
          "column_value": "{\"phone\"=>\"+15552010144\", \"countryShortName\"=>\"US\"}",
          "column_type": "PhoneColumn",
          "mock_reason": "libphonenumber says the number is not valid for the country"
        }
      }
    }
  ],
  "extensions": {
    "request_id": "54b4979c-903a-49a6-bb6b-9721e0915039"
  }
}
```

**Monday: the same new lead delivered twice at the same moment** (create calls in the order Monday got them):

```
200  the first create, item made
409  IDEMPOTENCY_CONFLICT (the first create is still running), n8n retries 2 seconds later
200  replayed from the first create (Idempotency-Replayed: true), same item
```

**Monday: SLA query**, then `next_items_page` with the cursor for each further page:

```graphql
query NewLeads($board: ID!, $statusCol: String!, $labels: [String]!, $cols: [String!], $limit: Int!) {
  items_page_by_column_values(
    board_id: $board
    limit: $limit
    columns: [{ column_id: $statusCol, column_values: $labels }]
  ) {
    cursor
    items {
      id
      name
      url
      created_at
      column_values(ids: $cols) {
        id
        text
        value
      }
    }
  }
}
```

```json
{"board": "5550001111", "statusCol": "status", "labels": ["New"], "cols": ["status", "person"], "limit": 100}
```

**Slack: new lead** (team channel, mention):

```
<@U0ALEX> New lead for you: *Jamie Rivera*
Company: Rivera Roofing
Email: jamie.rivera@example.com
Phone: +12025550101
Source: Website contact form
Message: Need a quote for a new roof.
The house is two floors.
<https://acme-demo.monday.com/boards/5550001111/pulses/9000000005|Open in Monday>
```

**Slack: new lead whose phone Monday refused** (Sam's own webhook):

```
<@U0SAM> New lead for you: *Ari Novak*
Email: ari.novak@example.com
Phone: +15552010123
Source: Website contact form
Message: Need a new water heater
Monday did not accept the phone number (555) 201-0123, so the lead was saved without it. You find it in the message; please check with the lead.
<https://acme-demo.monday.com/boards/5550001111/pulses/9000000011|Open in Monday>
```

**Slack: nudge, escalation and the team list** from the first SLA check:

```
<@U0ALEX> This lead is still New after more than 2 h. Please reach out:
- <https://acme-demo.monday.com/boards/5550001111/pulses/9000000220|Avery Stone>, waiting 2.5 h
```

```
<@U0MANAGER1> *SLA missed:* 1 lead is still New after more than 4 h.
- <https://acme-demo.monday.com/boards/5550001111/pulses/9000000222|Casey Fox>, owner <@U0ALEX>, waiting 5.0 h
```

```
*3 leads need an owner.* Still New after more than 2 h, and the owner is missing, not in OWNERS_JSON or away:
- <https://acme-demo.monday.com/boards/5550001111/pulses/9000000225|Finley Moss>, owner nobody, waiting 2.5 h
- <https://acme-demo.monday.com/boards/5550001111/pulses/9000000226|Gray Lin>, owner Dana Cole, waiting 2.5 h
- <https://acme-demo.monday.com/boards/5550001111/pulses/9000000227|Harper Quinn>, owner Pat Kim (away), waiting 2.5 h
```

**Alerts from the error alert workflow** (raw Slack text as the mock received it):

```
:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Lead Not Saved*
Lead NOT saved to Monday, so the form was told to retry (503). Reason: 500 - "{\"errors\": [{\"message\": \"Internal server error\", \"extensions\": {\"code\": \"INTERNAL_SERVER_ERROR\", \"status_code\": 500}}]}" | Lead: {"name":"Rowan Ellis","email":"rowan.ellis@example.com","phone":"202 555 0105","source":"Website contact form","submissionId":"sub-2001","message":"Window replacement"}
http://localhost:5678/workflow/QU8WLkFrCI5ZTD8w/executions/6452
```

```
:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Stop: Lead Not Saved*
Lead NOT saved to Monday, so the form was told to retry (503). Reason: Monday did not create the item: ColumnValueException: invalid value, please check our API documentation for the correct data structure for this column. https://developer.monday.com/api-reference/docs/change-column-values | Lead: {"name":"Sage Morris","email":"sage.morris@example.com","source":"Website contact form","submissionId":"sub-3001","message":"Deck stain"}
http://localhost:5678/workflow/QU8WLkFrCI5ZTD8w/executions/6461
```

```
:rotating_light: *TEST monday-lead-intake-sla (temporary)* failed at *Collect Page*
Monday refused the SLA query. InvalidColumnIdException (This column ID doesn't exist for the board). No reminders were sent; the next run tries again. [line 9]
http://localhost:5678/workflow/QU8WLkFrCI5ZTD8w/executions/6529
```

All names, emails, phone numbers, IDs and Slack member IDs above are made up.
