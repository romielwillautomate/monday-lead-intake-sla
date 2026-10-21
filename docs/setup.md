# Setup: Monday.com, Slack, your form and n8n

This guide sets up the real services: the Monday board, the API token, the Slack webhooks, n8n and your website form. Plan on about 30 minutes.

## 1. Prepare the board

Use an existing leads board or create one. The workflow needs these columns (the names can be anything, the types matter):

| Column | Type | Needed | Notes |
|---|---|---|---|
| Name | the item name | yes | Every board has it. The lead's name goes here |
| Email | Email | yes | Used to find returning leads |
| Phone | Phone | yes | Used to find returning leads |
| Status | Status | yes | Needs a label called `New` (or whatever you set in `MONDAY_STATUS_NEW_LABEL`). Anything else counts as picked up, for example Contacted, Qualified or Lost |
| Owner | People | yes | The person who handles the lead |
| Message | Long text | yes | The lead's first message. Longer than 2,000 characters is cut to fit |
| Company | Text | no | |
| Source | Text | no | Which form the lead used. Updated on every new inquiry |
| Last inquiry | Date | no | The last time the lead got in touch. Updated on every new inquiry |

The board's template Status column comes with labels like "Working on it", "Stuck" and "Done". Add a `New` label (and rename the others to fit your sales process) before you go live. If the label is missing, Monday refuses every new lead and you get an alert saying so.

New leads go into the group you set in `MONDAY_GROUP_ID`, or the board's top group if you leave it empty.

## 2. Get the API token

1. In Monday, click your profile picture (top right) and choose **Developers**.
2. Open **API token** and click **Show**. Copy it into `MONDAY_API_TOKEN`.

The token acts as you: it can do what you can do in the Monday screen, and updates it writes show you as the author. Many teams create a separate user for integrations (for example "Website Leads") and use that user's token. Whoever it is needs edit rights on the board. If you ever regenerate the token, the old one stops working at once, so update n8n straight away.

## 3. Find the IDs

**Board ID:** open the board. The number in the address is the ID: `https://yourteam.monday.com/boards/1234567890`.

**Column and group IDs:** turn on Developer mode. Click your profile picture, choose **monday.labs**, search for *Developer mode* and click **Activate**. After a refresh, each column's menu shows its ID, and so does each group's. Column IDs look like `email_mkv1a2b3`, not like the column title.

You can also read them all at once in the API playground (profile picture → Developers → **API playground**):

```graphql
query {
  boards(ids: 1234567890) {
    groups { id title }
    columns { id title type }
  }
}
```

**User IDs for `OWNERS_JSON`:** in the same playground:

```graphql
query {
  users(limit: 100) { id name email }
}
```

Make sure each owner can open the board, so they can work the leads they are given.

## 4. Slack

1. Go to [api.slack.com/apps](https://api.slack.com/apps), click **Create New App** → **From scratch**, name it (for example "Lead alerts") and pick your workspace.
2. Open **Incoming Webhooks**, switch it on and click **Add New Webhook to Workspace**. Pick the sales team channel and copy the URL into `SLACK_WEBHOOK_URL`. The error alert workflow posts to the same channel.
3. Optional: add a second webhook for a managers channel and put it in `MANAGER_SLACK_WEBHOOK_URL`. Without it, escalations go to the team channel.
4. Optional: if an owner wants their alerts in their own channel, add a webhook for that channel and put it in their `slackWebhookUrl`.

**Member IDs** make the mentions work (`<@U0123ABCD>` pings the person). In Slack, open the person's profile, click the three dots (**More**) and choose **Copy member ID**. It starts with `U`. Put the owners' IDs in `OWNERS_JSON` and the manager's in `MANAGER_SLACK_ID`.

## 5. Prepare n8n

1. Add the variables from the README (or from `.env.example`) to your n8n environment and restart n8n. Self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.
2. Import `workflow/monday-lead-intake-sla.json` and `workflow/error-alert-slack.json`.
3. Activate the error alert workflow. It must be active: n8n 2.x skips an error workflow that is switched off, and no alert arrives.
4. Open the lead workflow's settings (the three dots at the top right, then **Settings**) and choose the error alert workflow as its **Error Workflow**. Save.
5. Activate the lead workflow. The SLA check starts running every 30 minutes on its own.

The workflow has two production URLs:

| URL | What it is for |
|---|---|
| `https://YOUR-N8N-DOMAIN/webhook/monday-lead-intake` | Your form posts leads here |
| `https://YOUR-N8N-DOMAIN/webhook/monday-lead-sla-run` | Runs the SLA check on demand |

## 6. Connect your form

Send each submission as a POST to the intake URL, as JSON or as a normal form post (`application/x-www-form-urlencoded`). These fields are read; anything else is ignored:

| Field | Also accepted as | Notes |
|---|---|---|
| `name` | `full_name`, `fullName`, or `first_name` / `firstName` with `last_name` / `lastName` | |
| `email` | `Email`, `email_address`, `emailAddress` | An email or a phone is required |
| `phone` | `Phone`, `phone_number`, `phoneNumber`, `tel` | Any format. Numbers without a country code use `DEFAULT_COUNTRY`. Monday checks every number with Google's libphonenumber; a number it refuses is kept in the message and the owner is asked to check it |
| `company` | `Company`, `company_name`, `business` | |
| `message` | `Message`, `comments`, `details`, `inquiry` | |
| `source` | `form_name`, `formName`, `utm_source` | Default "Website form" |
| `submissionId` | `submission_id`, `entry_id`, `entryId`, `event_id` | Optional. If your form tool sends one, repeat deliveries are recognised by it |

Send the secret in the `X-Webhook-Secret` header. If your form tool cannot set headers, add it to the URL instead: `.../webhook/monday-lead-intake?key=YOUR_LEAD_WEBHOOK_SECRET`. The header is better, because URLs end up in logs.

Post from a server, never from JavaScript in the visitor's browser. Anything in the page can be read by anyone, including the secret. Form tools with a webhook feature, Zapier or Make, or your own site's backend all post from a server.

What the form gets back:

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true, "status": "created", "itemId": "..."}` | New item on the board |
| 200 | `{"ok": true, "status": "updated", "itemId": "..."}` | Known lead, update added to the existing item |
| 200 | `{"ok": true, "status": "duplicate", "itemId": "..."}` | This submission was already saved |
| 401 | `{"ok": false, "error": "unauthorized"}` | Wrong or missing secret |
| 422 | `{"ok": false, "error": "invalid_lead", "details": [...]}` | No usable email or phone. Do not retry |
| 503 | `{"ok": false, "error": "not_saved", "retry": true}` | Not saved yet. Retry after the `Retry-After` header (60 seconds) |

If your form tool can retry on errors, turn that on. A retry never creates a second item.

## 7. Try it

Send a test lead:

```bash
curl -X POST https://YOUR-N8N-DOMAIN/webhook/monday-lead-intake \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_LEAD_WEBHOOK_SECRET" \
  -d '{"name": "Jamie Rivera", "email": "jamie.rivera@example.com", "phone": "(202) 555-0101", "company": "Rivera Roofing", "message": "Need a quote", "source": "Website contact form", "submissionId": "test-001"}'
```

You should get `"status": "created"`, a new item in status New with an owner, and a Slack message. Send the same command again and you get `"status": "duplicate"`. Change the `submissionId` and the message and send it once more: no new item, but an update on the existing one.

To run the SLA check without waiting for the schedule:

```bash
curl -X POST https://YOUR-N8N-DOMAIN/webhook/monday-lead-sla-run -H "X-Run-Secret: YOUR_RUN_NOW_SECRET"
```

To see a reminder without waiting two hours, set `SLA_HOURS` to `0.05` (3 minutes) for a moment, restart n8n, create a lead, wait a few minutes and run the check. Set it back afterwards.

## Troubleshooting

Open the workflow's **Executions** list in n8n, or read the alert in Slack:

| You see | Likely cause |
|---|---|
| 401 for real form posts | The secret in the form does not match `LEAD_WEBHOOK_SECRET`, or the form cannot send the header (use `?key=`) |
| 503 and an alert starting "Config problem" | The alert names the variable that is missing or wrong |
| Alert with `InvalidColumnIdException` | One of the `MONDAY_COL_` values is not a column ID on this board |
| Alert with `ColumnValueException` and "label does not exist" | The board has no `New` status label, or `MONDAY_STATUS_NEW_LABEL` is spelled differently |
| Alert with `ColumnValueException` on the people column | A `mondayUserId` in `OWNERS_JSON` is wrong or belongs to a user who left the account |
| Alert with "Not Authenticated" or a 401 from Monday | `MONDAY_API_TOKEN` is wrong or was regenerated |
| Alert with `DAILY_LIMIT_EXCEEDED` | The account used up its daily API calls (1,000 on Free, Basic and Standard, 10,000 on Pro). It resets at midnight UTC |
| Slack alert failed with `no_service` | The Slack webhook was removed. Create a new one and update the variable |
| No SLA reminders | Leads are not in the exact status label `New`, they are younger than `SLA_HOURS`, or the reminder was already sent |
| A run failed but no alert arrived | The error alert workflow is not active, or it is not chosen as the Error Workflow in the lead workflow's settings |
| The owner's message says "Monday did not accept the phone number" | The visitor made a typo, or the number belongs to a country that shares a calling code with `DEFAULT_COUNTRY` (for example a Canadian number with `US`). Check it with the lead and type it into the Phone column |
| A returning lead became a new item | The email on the old item has a different display text, or the phone was saved in another country's format. Fix the old item and merge the two |
