# Changelog

## v1 (2026-10-21)

First build, tested end to end against a mock Monday.com GraphQL API, mock Slack webhooks and the error alert workflow (65 checks).

### Lead intake

- A webhook for website forms, protected by a shared secret in the `X-Webhook-Secret` header, or `?key=` in the URL for form tools that cannot set headers. JSON and normal form posts both work.
- Emails are lowercased and phone numbers turned into E.164. Common field names from form tools are accepted, including separate first and last name fields.
- Leads with no usable email or phone get a 422 with the reason.
- One search request with two `items_page_by_column_values` lookups, by email and by phone. Email is searched lowercase and as typed, because Monday matches the column's whole display text. Phone is searched as digits only, without the home country code, so numbers typed in by hand as `2025550101` match too. Monday's phone search is a "contains" match, so every hit is checked exactly before it counts.
- A known lead gets an update with the new inquiry (HTML escaped) and a refresh of the last inquiry date, the source and any empty email, phone or company field. The empty fields are filled in a separate change, so if Monday refuses one of them, the inquiry and the refresh are still saved. The owner, the status, the name and the first message are never touched.
- A new lead is created with status New, the mapped columns and the next owner in the rotation, in the group you choose. If Monday refuses only the phone number or the email (Monday checks phone numbers with Google's libphonenumber), the item is created again without that column, the value goes into the message, and the owner is asked to check it.
- Round robin over `OWNERS_JSON`, skipping anyone marked away. The pointer only moves once Monday has created the item, so a failed save does not skip anyone.
- The same submission delivered twice within 24 hours is skipped.
- Every mutation sends Monday's `Idempotency-Key` header, built from the submission. n8n's retry after a lost answer and a second delivery that arrives while the first is still running both get Monday's first answer, so there is one item. After Monday refuses a lead, the next try uses a new key, in case Monday keeps the refusal under the old one.
- The owner gets a Slack message with a link to the item, in their own channel or as a mention in the team channel. Everything the visitor typed is escaped, so a form cannot ping `@channel`.
- If Monday fails, the form gets a 503 with `Retry-After` and the alert carries the whole lead.
- The error alert workflow must be active (n8n 2.x skips an inactive one). The test runs it and checks that every failed run raised exactly one alert.

### SLA check

- Runs every 30 minutes, plus a Run Now webhook protected by the `X-Run-Secret` header.
- Reads every item in status New, 100 at a time, following Monday's cursor.
- After `SLA_HOURS` the owner gets one nudge; after twice that, the manager gets one escalation. One message per person, listing all of their leads.
- Leads with no owner, an owner who is not in `OWNERS_JSON` or an owner who is away are listed in the team channel.
- What was sent is remembered in static data, and a lead that leaves New is forgotten.

### Found while testing

- **A thrown error answered the form with an empty 200.** When a Code node fails before the webhook has answered, n8n replies 200 with no body, so the form thinks the lead was saved. Config problems are now caught in the first Code node and answered with a 503 and an alert, and the Code nodes before the answer never throw.
- **n8n's retry resent SLA messages that had already gone out.** With several items in one HTTP node, n8n only looks at the first item to decide on a retry. When the first Slack message failed, the whole node ran again and the other owners got the same nudge three times; when a later one failed, it was not retried at all. The SLA messages are now sent one per loop pass, so each one is retried on its own and nothing is sent twice.
- **Error messages were cut.** n8n splits a message thrown in a Code node at ": " and keeps only the last part, so "Monday refused the SLA query: InvalidColumnIdException: ..." became just the last sentence. The thrown messages are now written without that pattern.

### Found in review

- **Real Monday would have refused most of the original test leads.** Monday validates phone numbers with libphonenumber, and numbers like +1 555 201 0101 are not valid there (555 is not an area code). The workflow sent any 10 digit number, so a typo in the phone field made Monday refuse the whole item and the lead was never saved; the form kept getting 503. The mock now validates with libphonenumber, the test data uses the fictional 202 555 01XX range, and a refused phone or email no longer loses the lead.
- **Two deliveries of one new lead at the same moment made two items.** The `Idempotency-Key` included the n8n execution ID, so a form that timed out and redelivered while the first run was still working got a second key and a second item. The key is now built from the submission, and the mock answers a concurrent duplicate with Monday's 409 and then the replay, as documented.
- **The error alert workflow was never run in the test,** so none of the "you get an alert" claims were proven. The test now imports, activates and attaches it, and checks the alert text for every failure path.
- **The SLA test only covered two pages,** which does not show that the page loop keeps the items of earlier pages. It now reads three.
- `group_id` is no longer sent as `null` when `MONDAY_GROUP_ID` is empty, and the alert for an unsaved lead now shows only the lead's own fields instead of the workflow's internal ones.
