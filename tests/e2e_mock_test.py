"""End-to-end test for the Monday.com lead intake and SLA workflow against MOCK monday.com and Slack APIs.

No real monday.com account or Slack workspace is used. The script:
  1. starts a mock monday.com GraphQL API and mock Slack incoming webhooks on this machine,
  2. imports temporary copies of the workflow and of the error alert workflow into your n8n (API host pointed at
     the mock, fake keys), activates them and makes the alert workflow the error workflow of the copies,
  3. plays the scenarios below: security, validation, new leads, repeat deliveries (one after the other and at the
     same moment), returning leads matched by email and by phone, round robin with an owner away, Slack injection,
     phone numbers monday refuses, monday errors, complexity limits, lost responses, Slack outages and the SLA
     check with nudges, escalations and pagination,
  4. checks every request the workflow made and every Slack alert the error workflow sent, prints PASS or FAIL
     per check, then deletes the temporary copies.

The mock monday API is a real GraphQL server (graphql-core) with a schema subset written from monday's API
reference, so a query with an unknown field, a wrong variable type or a syntax error fails here the way it
fails on monday. It also behaves like monday where it matters: GraphQL errors come back with HTTP 200 and
partial data, column values must be a JSON string in each column's documented format, phone numbers are
checked with Google's libphonenumber (the library monday names), email search matches the whole display text,
phone search wants digits only and does a "contains" match, the complexity budget error is a 429, mutations
honour the Idempotency-Key header (replay, and 409 while the first request is still running), and results are
paginated with cursors.

Requirements: Python 3.9+, graphql-core 3.2 or later and phonenumbers (pip install graphql-core phonenumbers),
a running n8n with the public API.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python tests/e2e_mock_test.py
"""
import base64, json, os, re, sys, threading, time, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

try:
    from graphql import GraphQLError, build_schema, graphql_sync, parse
    from graphql.language import OperationDefinitionNode
    import phonenumbers
except ImportError:
    raise SystemExit('This test needs graphql-core and phonenumbers: pip install graphql-core phonenumbers')

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8785
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
SLUG = 'monday-lead-intake-sla'
ROOT_DIR = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT_DIR / 'workflow' / f'{SLUG}.json'
ERROR_WORKFLOW = ROOT_DIR / 'workflow' / 'error-alert-slack.json'

BOARD = '5550001111'
TOKEN = 'mock-monday-token'
VERSION = '2026-10'
LEAD_SECRET, RUN_SECRET = 'test-lead-secret', 'test-run-secret'
ACCOUNT = 'acme-demo'
COL = {'email': 'email_mkv1a2b3', 'phone': 'phone_mkv1c4d5', 'status': 'status', 'owner': 'person',
       'message': 'long_text_mkv1e6f7', 'company': 'text_mkv1g8h9', 'source': 'text_mkv1j0k1', 'last': 'date_mkv1l2m3'}
ALEX, SAM, PAT, DANA = 41000001, 41000002, 41000003, 41000004
USERS = {ALEX: 'Alex Rivera', SAM: 'Sam Lee', PAT: 'Pat Kim', DANA: 'Dana Cole'}
HOOK = {name: f'{MOCK}/slack/services/T0000TEST/B000{i}/{name}' for i, name in enumerate(('team', 'sam', 'manager', 'alerts'))}
OWNERS = [{'name': 'Alex Rivera', 'mondayUserId': ALEX, 'slackMemberId': 'U0ALEX'},
          {'name': 'Sam Lee', 'mondayUserId': SAM, 'slackMemberId': 'U0SAM', 'slackWebhookUrl': HOOK['sam']},
          {'name': 'Pat Kim', 'mondayUserId': PAT, 'slackMemberId': 'U0PAT', 'away': True}]
OWNER_HOOK = {ALEX: 'team', SAM: 'sam'}
ENV = {
    'MONDAY_API_TOKEN': TOKEN, 'MONDAY_API_VERSION': VERSION, 'MONDAY_BOARD_ID': BOARD, 'MONDAY_GROUP_ID': 'new_leads',
    'MONDAY_STATUS_NEW_LABEL': 'New', 'MONDAY_COL_EMAIL': COL['email'], 'MONDAY_COL_PHONE': COL['phone'],
    'MONDAY_COL_STATUS': COL['status'], 'MONDAY_COL_OWNER': COL['owner'], 'MONDAY_COL_MESSAGE': COL['message'],
    'MONDAY_COL_COMPANY': COL['company'], 'MONDAY_COL_SOURCE': COL['source'], 'MONDAY_COL_LAST_INQUIRY': COL['last'],
    'OWNERS_JSON': json.dumps(OWNERS), 'LEAD_WEBHOOK_SECRET': LEAD_SECRET, 'RUN_NOW_SECRET': RUN_SECRET,
    'SLACK_WEBHOOK_URL': HOOK['team'], 'MANAGER_SLACK_ID': 'U0MANAGER1', 'MANAGER_SLACK_WEBHOOK_URL': HOOK['manager'],
    'SLA_HOURS': '2', 'DEFAULT_COUNTRY': 'US',
}
# The error alert workflow posts to SLACK_WEBHOOK_URL too; in the test it gets its own mock hook so alerts can be told apart.
ERROR_ENV = {'SLACK_WEBHOOK_URL': HOOK['alerts']}
HOSTS = {'https://api.monday.com': MOCK}
KEY_RE = re.compile(r'lead-[0-9a-z]+-a\d+-(create|update)(-without-(phone|email)(-(phone|email))?)?')

# ----------------------------------------------------------------------------------------------------------------
# Mock monday.com: schema subset from the API reference (items_page_by_column_values, next_items_page,
# create_item, change_multiple_column_values, create_update and the column value types used here)
# ----------------------------------------------------------------------------------------------------------------
SDL = '''
scalar JSON
scalar Date
type Query {
  items_page_by_column_values(board_id: ID!, columns: [ItemsPageByColumnValuesQuery!], cursor: String, limit: Int! = 25, hierarchy_scope_config: String): ItemsResponse!
  next_items_page(cursor: String!, limit: Int! = 25): ItemsResponse!
  items(ids: [ID!], limit: Int, page: Int, newest_first: Boolean, exclude_nonactive: Boolean): [Item]
}
type Mutation {
  create_item(board_id: ID!, item_name: String!, column_values: JSON, group_id: String, create_labels_if_missing: Boolean, position_relative_method: PositionRelative, relative_to: ID): Item
  change_multiple_column_values(item_id: ID, board_id: ID!, column_values: JSON!, create_labels_if_missing: Boolean): Item
  create_update(item_id: ID, body: String!, parent_id: ID, mentions_list: [UpdateMention], original_creation_date: String): Update
}
enum PositionRelative { before_at after_at }
enum MentionType { User Team Board Project }
input UpdateMention { id: ID!, type: MentionType! }
input ItemsPageByColumnValuesQuery { column_id: String!, column_values: [String]! }
type ItemsResponse { cursor: String, items: [Item!]! }
type Group { id: ID!, title: String! }
type Board { id: ID!, name: String! }
enum State { all active archived deleted }
enum ColumnType { email phone status people text long_text date }
enum ColumnCapability { CALCULATED VISIBILITY }
type Column { id: ID!, title: String!, type: ColumnType! }
type Item {
  id: ID!, name: String!, url: String!, relative_link: String, created_at: Date, updated_at: Date, creator_id: String!,
  state: State, board: Board, group: Group, updates(limit: Int, page: Int): [Update!],
  column_values(ids: [String!], types: [ColumnType!], capabilities: [ColumnCapability!]): [ColumnValue!]!
}
interface ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column! }
type EmailValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, email: String, label: String, updated_at: Date }
type PhoneValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, phone: String, country_short_name: String, updated_at: Date }
type StatusValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, index: Int, label: String, is_done: Boolean, updated_at: Date }
type PeopleEntity { id: ID!, kind: Kind }
enum Kind { person team agent }
type PeopleValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, persons_and_teams: [PeopleEntity!], updated_at: Date }
type TextValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column! }
type LongTextValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, updated_at: Date }
type DateValue implements ColumnValue { id: ID!, text: String, type: ColumnType!, value: JSON, column: Column!, date: String, time: String, updated_at: Date }
type Update { id: ID!, body: String!, text_body: String, created_at: Date, item_id: String, creator_id: String }
'''
SCHEMA = build_schema(SDL)
SCHEMA.type_map['ColumnValue'].resolve_type = lambda obj, info, abstract: obj['__typename']

COLDEFS = {COL['email']: ('email', 'Email'), COL['phone']: ('phone', 'Phone'), COL['status']: ('status', 'Status'),
           COL['owner']: ('people', 'Owner'), COL['message']: ('long_text', 'Message'), COL['company']: ('text', 'Company'),
           COL['source']: ('text', 'Source'), COL['last']: ('date', 'Last inquiry')}
TYPENAME = {'email': 'EmailValue', 'phone': 'PhoneValue', 'status': 'StatusValue', 'people': 'PeopleValue',
            'text': 'TextValue', 'long_text': 'LongTextValue', 'date': 'DateValue'}
# How monday names the column class in ColumnValueException errors (error_data.column_type)
CLASSNAME = {'email': 'EmailColumn', 'phone': 'PhoneColumn', 'status': 'StatusColumn', 'people': 'MultiplePersonColumn',
             'text': 'TextColumn', 'long_text': 'LongTextColumn', 'date': 'DateColumn'}
LABELS = {7: 'New', 0: 'Working on it', 1: 'Contacted', 3: 'Qualified', 2: 'Lost'}
GROUPS = {'topics': 'Inbox', 'new_leads': 'New leads'}
DOCS = 'https://developer.monday.com/api-reference/docs/change-column-values'

LOCK = threading.RLock()
STATE = {'next_id': 9000000001, 'next_update': 7000000001, 'items': {}, 'cursors': {}, 'idem': {}, 'inflight': set()}
LOG = []                       # every request the workflow made: dict(service, ...)
FAIL = {'monday': [], 'slack': set()}  # monday: list of (operation, kind, times) to inject; slack: hook names that are gone
NOTES = []                     # things the mock noticed that the real API would silently get wrong
SLOW_SECONDS = 1.5             # the "slow" fault: monday takes this long to create the item


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def mon_error(message, code, status=200, **data):
    ext = {'code': code, 'status_code': status}
    if data:
        ext['error_data'] = data
    return GraphQLError(message, extensions=ext)


def ruby(value):
    """monday quotes a refused column value in Ruby hash style: {"phone"=>"+1...", "countryShortName"=>"US"}"""
    return json.dumps(value).replace('": ', '"=>')


def column_value_error(col_id, value, why):
    # monday's message is the same generic sentence for every column; "why" is only there to debug the mock.
    return mon_error(f'invalid value, please check our API documentation for the correct data structure for this column. {DOCS}',
                     'ColumnValueException', column_value=ruby(value)[:200], column_type=CLASSNAME[COLDEFS[col_id][0]], mock_reason=why)


def phone_ok(phone, country):
    """monday checks phone numbers with Google's libphonenumber: the number must be valid for the given country."""
    try:
        number = phonenumbers.parse(phone, country)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.is_valid_number_for_region(number, country)


def coerce(col_id, v, create_labels):
    """Validates one column value the way monday documents it and returns what gets stored."""
    typ = COLDEFS[col_id][0]
    stamp = iso(now_utc())
    if typ == 'email':
        if isinstance(v, str) and ' ' in v:
            v = {'email': v.split(' ', 1)[0], 'text': v.split(' ', 1)[1]}
        if not isinstance(v, dict) or not v.get('email') or 'text' not in v:
            raise column_value_error(col_id, v, 'email and text are both required')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[A-Za-z]{2,}', v['email']):
            raise column_value_error(col_id, v, 'not an email address')
        return {'email': v['email'], 'text': v['text'] or v['email'], 'changed_at': stamp}
    if typ == 'phone':
        if not isinstance(v, dict) or not v.get('phone') or not v.get('countryShortName'):
            raise column_value_error(col_id, v, 'phone and countryShortName are both required')
        if v['countryShortName'] != v['countryShortName'].upper() or not phone_ok(v['phone'], v['countryShortName']):
            raise column_value_error(col_id, v, 'libphonenumber says the number is not valid for the country')
        return {'phone': v['phone'], 'countryShortName': v['countryShortName'], 'changed_at': stamp}
    if typ == 'status':
        if isinstance(v, str):
            v = {'label': v}
        if isinstance(v, dict) and 'index' in v and int(v['index']) in LABELS:
            return {'index': int(v['index']), 'changed_at': stamp}
        if isinstance(v, dict) and 'label' in v:
            hit = [i for i, l in LABELS.items() if l == v['label']]
            if hit:
                return {'index': hit[0], 'changed_at': stamp}
            if create_labels:
                LABELS[max(LABELS) + 1] = v['label']
                return {'index': max(LABELS), 'changed_at': stamp}
        raise column_value_error(col_id, v, 'label does not exist in column')
    if typ == 'people':
        people = v.get('personsAndTeams') if isinstance(v, dict) else None
        if not isinstance(people, list) or any(not isinstance(p, dict) or not isinstance(p.get('id'), int) or p.get('kind') not in ('person', 'team') for p in people):
            raise column_value_error(col_id, v, 'personsAndTeams must be a list of {id, kind}')
        if any(p['kind'] == 'person' and p['id'] not in USERS for p in people):
            raise column_value_error(col_id, v, 'user does not exist')
        return {'personsAndTeams': people, 'changed_at': stamp}
    if typ == 'text':
        if isinstance(v, (dict, list)):
            raise column_value_error(col_id, v, 'text columns take a plain string')
        return str(v)
    if typ == 'long_text':
        if isinstance(v, str):
            v = {'text': v}
        if not isinstance(v, dict) or not isinstance(v.get('text'), str):
            raise column_value_error(col_id, v, 'long text needs a text key')
        if len(v['text']) > 2000:
            raise column_value_error(col_id, v, 'long text is limited to 2000 characters')
        return {'text': v['text'], 'changed_at': stamp}
    if typ == 'date':
        if isinstance(v, str):
            parts = v.split(' ')
            v = {'date': parts[0], **({'time': parts[1]} if len(parts) > 1 else {})}
        try:
            datetime.strptime(v['date'], '%Y-%m-%d')
            if v.get('time'):
                datetime.strptime(v['time'], '%H:%M:%S')
        except (KeyError, TypeError, ValueError):
            raise column_value_error(col_id, v, 'date must be YYYY-MM-DD and time HH:MM:SS')
        return {'date': v['date'], 'time': v.get('time'), 'changed_at': stamp}
    raise column_value_error(col_id, v, 'unknown column type')


def apply_values(item, raw, create_labels=False):
    if raw is None:
        return
    if not isinstance(raw, str):
        raise mon_error('column_values must be a JSON-formatted string', 'InvalidArgumentException')
    try:
        values = json.loads(raw)
    except ValueError:
        raise mon_error('column_values is not valid JSON', 'JsonParseException')
    if not isinstance(values, dict):
        raise mon_error('column_values must be a JSON object of column IDs and values', 'InvalidArgumentException')
    staged = {}
    for col_id, v in values.items():
        if col_id not in COLDEFS:
            raise mon_error("This column ID doesn't exist for the board", 'InvalidColumnIdException', column_id=col_id)
        staged[col_id] = None if v is None else coerce(col_id, v, create_labels)
    for col_id, v in staged.items():          # all or nothing, like monday
        if v is None:
            item['values'].pop(col_id, None)
        else:
            item['values'][col_id] = v
    item['updated_at'] = now_utc()


def column_view(col_id, raw):
    typ, title = COLDEFS[col_id]
    base = {'__typename': TYPENAME[typ], 'id': col_id, 'type': typ, 'column': {'id': col_id, 'title': title, 'type': typ},
            'text': '', 'value': None, 'updated_at': None}
    if raw is None:
        return base
    base['value'] = json.dumps(raw)
    if isinstance(raw, dict) and raw.get('changed_at'):
        base['updated_at'] = raw['changed_at']
    if typ == 'email':
        base.update(email=raw['email'], label=raw['text'], text=raw['email'] if raw['text'] == raw['email'] else f"{raw['text']} - {raw['email']}")
    elif typ == 'phone':
        base.update(phone=raw['phone'], country_short_name=raw['countryShortName'], text=raw['phone'])
    elif typ == 'status':
        base.update(index=raw['index'], label=LABELS[raw['index']], text=LABELS[raw['index']], is_done=raw['index'] == 1)
    elif typ == 'people':
        base.update(persons_and_teams=[{'id': str(p['id']), 'kind': p['kind']} for p in raw['personsAndTeams']],
                    text=', '.join(USERS.get(p['id'], f"Team {p['id']}") for p in raw['personsAndTeams']))
    elif typ == 'text':
        base['text'] = raw
    elif typ == 'long_text':
        base['text'] = raw['text']
    elif typ == 'date':
        base.update(date=raw['date'], time=raw.get('time') or '', text=raw['date'] + (' ' + raw['time'][:5] if raw.get('time') else ''))
    return base


def item_view(item):
    def column_values(info, ids=None, types=None, capabilities=None):
        cols = [c for c in COLDEFS if (ids is None or c in ids) and (types is None or COLDEFS[c][0] in types)]
        return [column_view(c, item['values'].get(c)) for c in cols]
    return {'id': item['id'], 'name': item['name'], 'url': f"https://{ACCOUNT}.monday.com/boards/{BOARD}/pulses/{item['id']}",
            'relative_link': f"/boards/{BOARD}/pulses/{item['id']}", 'created_at': iso(item['created_at']),
            'updated_at': iso(item['updated_at']), 'creator_id': '41000099', 'state': 'active',
            'board': {'id': BOARD, 'name': 'Leads'}, 'group': {'id': item['group'], 'title': GROUPS[item['group']]},
            'updates': [dict(u) for u in item['updates']], 'column_values': column_values}


def check_board(board_id):
    if str(board_id) != BOARD:
        raise mon_error('This board does not exist or you do not have permission to access it', 'InvalidBoardIdException', board_id=str(board_id))


def matches(item, col_id, wanted):
    typ = COLDEFS[col_id][0]
    raw = item['values'].get(col_id)
    for w in wanted:
        if w in (None, ''):
            if raw is None:
                return True
            continue
        if raw is None:
            continue
        if typ == 'email' and raw['text'] == w:          # the whole display text, exactly
            return True
        if typ == 'phone':
            if re.fullmatch(r'\d+', w):
                if w in re.sub(r'\D', '', raw['phone']):  # one number, digits only, "contains"
                    return True
            elif w.upper() == raw['countryShortName']:
                return True
            else:
                NOTES.append(f'phone search value {w!r} has non digit characters, monday matches nothing for it')
        if typ == 'status' and LABELS[raw['index']] == w:
            return True
        if typ == 'text' and raw.lower() == w.lower():
            return True
        if typ == 'long_text' and raw['text'] == w:
            return True
        if typ == 'people' and any(str(p['id']) == w or USERS.get(p['id']) == w for p in raw['personsAndTeams']):
            return True
        if typ == 'date' and raw['date'] == w[:10]:
            return True
    return False


def page_of(ids, limit):
    if not 1 <= limit <= 500:
        raise mon_error('limit must be between 1 and 500', 'InvalidArgumentException')
    cursor = None
    if len(ids) > limit:
        cursor = base64.b64encode(f'MSw{uuid.uuid4().hex}'.encode()).decode().rstrip('=')
        STATE['cursors'][cursor] = ids[limit:]
    return {'cursor': cursor, 'items': [item_view(STATE['items'][i]) for i in ids[:limit]]}


def r_items_page_by_column_values(info, board_id, limit=25, columns=None, cursor=None, hierarchy_scope_config=None):
    if columns and cursor:
        raise mon_error("You can't use columns and cursor in the same request", 'InvalidArgumentException')
    if cursor:
        return r_next_items_page(info, cursor, limit)
    check_board(board_id)
    for c in columns or []:
        if c['column_id'] not in COLDEFS:
            raise mon_error("This column ID doesn't exist for the board", 'InvalidColumnIdException', column_id=c['column_id'])
    ids = [i for i in sorted(STATE['items']) if all(matches(STATE['items'][i], c['column_id'], c['column_values']) for c in columns or [])]
    return page_of(ids, limit)


def r_next_items_page(info, cursor, limit=25):
    if cursor not in STATE['cursors']:
        raise mon_error('Cursor is invalid or expired', 'InvalidArgumentException')
    return page_of(STATE['cursors'].pop(cursor), limit)


def r_items(info, ids=None, **kwargs):
    return [item_view(STATE['items'][str(i)]) for i in ids or [] if str(i) in STATE['items']]


def r_create_item(info, board_id, item_name, column_values=None, group_id=None, create_labels_if_missing=False, **kwargs):
    check_board(board_id)
    if not 1 <= len(item_name) <= 255:
        raise mon_error('Item name must be between 1 and 255 characters long', 'ItemNameTooLongException')
    if group_id is not None and group_id not in GROUPS:
        raise mon_error('Group not found', 'InvalidArgumentException', group_id=group_id)
    item = new_item(item_name, group=group_id or 'topics')
    try:
        apply_values(item, column_values, create_labels_if_missing)
    except GraphQLError:
        del STATE['items'][item['id']]
        raise
    return item_view(item)


def r_change_multiple_column_values(info, board_id, column_values, item_id=None, create_labels_if_missing=False):
    check_board(board_id)
    item = STATE['items'].get(str(item_id))
    if not item:
        raise mon_error('Item not found in board', 'ResourceNotFoundException', item_id=str(item_id))
    apply_values(item, column_values, create_labels_if_missing)
    return item_view(item)


def r_create_update(info, body, item_id=None, **kwargs):
    item = STATE['items'].get(str(item_id))
    if not item:
        raise mon_error('Item not found', 'ResourceNotFoundException', item_id=str(item_id))
    if not body.strip():
        raise mon_error('Update body cannot be empty', 'InvalidArgumentException')
    update = {'id': str(STATE['next_update']), 'body': body, 'text_body': re.sub('<[^>]+>', ' ', body), 'created_at': iso(now_utc()),
              'item_id': item['id'], 'creator_id': '41000099'}
    STATE['next_update'] += 1
    item['updates'].append(update)
    return update


ROOT = {'items_page_by_column_values': r_items_page_by_column_values, 'next_items_page': r_next_items_page, 'items': r_items,
        'create_item': r_create_item, 'change_multiple_column_values': r_change_multiple_column_values, 'create_update': r_create_update}


def new_item(name, values=None, group='new_leads', created=None):
    with LOCK:
        item = {'id': str(STATE['next_id']), 'name': name, 'group': group, 'created_at': created or now_utc(),
                'updated_at': created or now_utc(), 'values': {}, 'updates': []}
        STATE['next_id'] += 1
        STATE['items'][item['id']] = item
        for col_id, v in (values or {}).items():
            item['values'][col_id] = coerce(col_id, v, False)
        return item


def board_items(**where):
    """Items on the mock board, filtered by readable fields: email, phone, name."""
    out = []
    for item in list(STATE['items'].values()):
        email = (item['values'].get(COL['email']) or {}).get('email')
        phone = (item['values'].get(COL['phone']) or {}).get('phone')
        if all({'email': email, 'phone': phone, 'name': item['name']}[k] == v for k, v in where.items()):
            out.append(item)
    return out


def status_of(item):
    raw = item['values'].get(COL['status'])
    return LABELS[raw['index']] if raw else ''


def owner_of(item):
    raw = item['values'].get(COL['owner'])
    return [p['id'] for p in raw['personsAndTeams']] if raw else []


# ----------------------------------------------------------------------------------------------------------------
# HTTP layer of the mocks
# ----------------------------------------------------------------------------------------------------------------
COMPLEXITY_429 = {'errors': [{'message': 'Complexity budget exhausted', 'extensions': {
    'code': 'COMPLEXITY_BUDGET_EXHAUSTED', 'complexity': 6182, 'complexity_budget_left': 100,
    'complexity_budget_limit': 5000000, 'retry_in_seconds': 60, 'status_code': 429}}]}
CONFLICT_409 = {'errors': [{'message': 'A request with this idempotency key is currently being processed',
                            'extensions': {'code': 'IDEMPOTENCY_CONFLICT'}}]}
VERSIONS = {'2025-04', '2025-07', '2025-10', '2026-01', '2026-04', '2026-07', '2026-10', '2027-01'}


def take_fault(op):
    for f in FAIL['monday']:
        if f['op'] == op and f['times'] > 0:
            f['times'] -= 1
            return f['kind']
    return None


class Mock(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, obj, ctype=None, headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype or ('text/html' if isinstance(obj, str) else 'application/json; charset=utf-8'))
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        # One request per connection: an idle kept-alive socket through Docker's host bridge once swallowed a request
        # (n8n saw a 15 s timeout, the mock never saw the request), which made a retry check flaky.
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)
        return code, obj

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split('?')[0].rstrip('/')
        if path.startswith('/slack/'):
            return self.slack(path, headers, raw)
        if path == '/v2':
            return self.monday(headers, raw)
        LOG.append({'service': 'other', 'path': path})
        self._send(404, {'error': 'not mocked'})

    def slack(self, path, headers, raw):
        name = path.rsplit('/', 1)[-1]
        entry = {'service': 'slack', 'hook': name, 'headers': headers, 'raw': raw.decode('utf-8', 'replace'), 'at': time.time()}
        LOG.append(entry)
        if f'{MOCK}{path}' not in HOOK.values() or name in FAIL['slack']:
            entry['status'] = 404
            return self._send(404, 'no_service')
        if not headers.get('content-type', '').startswith('application/json'):
            entry['status'] = 400
            return self._send(400, 'invalid_payload')
        try:
            body = json.loads(raw)
        except ValueError:
            entry['status'] = 400
            return self._send(400, 'invalid_payload')
        entry['body'] = body
        if not isinstance(body, dict) or not str(body.get('text') or '').strip():
            entry['status'] = 400
            return self._send(400, 'no_text')
        entry['status'] = 200
        entry['text'] = body['text']
        self._send(200, 'ok')

    def monday(self, headers, raw):
        entry = {'service': 'monday', 'headers': headers, 'raw': raw.decode('utf-8', 'replace'), 'op': None, 'body': None, 'at': time.time()}
        LOG.append(entry)

        def reply(code, obj, extra=None):
            entry['status'], entry['response'] = code, obj
            return self._send(code, obj, headers=extra)

        if headers.get('authorization') != TOKEN:
            entry['rejected'] = 'auth'
            return reply(401, {'errors': [{'message': 'Not Authenticated', 'extensions': {'code': 'UNAUTHENTICATED', 'status_code': 401}}]})
        if not headers.get('content-type', '').startswith('application/json'):
            entry['rejected'] = 'content-type'
            return reply(400, {'errors': [{'message': 'Bad request: send a JSON body with the application/json content type', 'extensions': {'code': 'BAD_REQUEST', 'status_code': 400}}]})
        try:
            body = json.loads(raw)
        except ValueError:
            entry['rejected'] = 'json'
            return reply(400, {'errors': [{'message': 'Issues interpreting the provided JSON', 'extensions': {'code': 'JsonParseException', 'status_code': 400}}]})
        entry['body'] = body
        if not isinstance(body, dict) or not isinstance(body.get('query'), str):
            entry['rejected'] = 'no query'
            return reply(400, {'errors': [{'message': 'Bad request: pass the query string with the query key', 'extensions': {'code': 'BAD_REQUEST', 'status_code': 400}}]})
        rid = {'request_id': str(uuid.uuid4())}
        version = headers.get('api-version')
        if version is not None and version not in VERSIONS:
            entry['rejected'] = 'version'
            return reply(200, {'errors': [{'message': f'Invalid API version {version!r}', 'extensions': {'code': 'InvalidVersionException', 'status_code': 200}}], 'extensions': rid})
        variables = body.get('variables')
        if variables is not None and not isinstance(variables, dict):
            entry['rejected'] = 'variables'
            return reply(200, {'errors': [{'message': 'Variables must be sent as a JSON object, not a string', 'extensions': {'code': 'InvalidArgumentException', 'status_code': 200}}], 'extensions': rid})
        try:
            doc = parse(body['query'])
        except GraphQLError as e:
            entry['rejected'] = 'syntax'
            return reply(200, {'errors': [{'message': f'Parse error on {e.message}', 'locations': [l._asdict() for l in e.locations or []],
                                           'extensions': {'code': 'PARSE_ERROR', 'status_code': 200}}], 'extensions': rid})
        ops = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
        op = ops[0] if len(ops) == 1 else None
        entry['op'] = op.name.value if op and op.name else None
        entry['kind'] = op.operation.value if op else None
        # The Idempotency-Key header only counts on mutations (monday ignores it on queries).
        key = headers.get('idempotency-key') if entry['kind'] == 'mutation' else None

        with LOCK:
            if key and key in STATE['inflight']:     # the first request with this key is still running
                entry['conflict'] = True
                return reply(409, CONFLICT_409, {'Retry-After': '1'})
            fault = take_fault(entry['op'])
            if fault == 'complexity':                # rejected before it ran, so nothing is kept for the key
                return reply(429, COMPLEXITY_429, {'Retry-After': '60'})
            if fault == 'server':
                return reply(500, {'errors': [{'message': 'Internal server error', 'extensions': {'code': 'INTERNAL_SERVER_ERROR', 'status_code': 500}}]})
            if key and key in STATE['idem']:         # a retry: the first answer again, nothing done twice
                entry['replayed'] = True
                if fault == 'drop':
                    entry['dropped'] = True
                    return reply(504, '<html><body><h1>504 Gateway Time-out</h1></body></html>')
                return reply(200, STATE['idem'][key], {'Idempotency-Replayed': 'true'})
            if key:
                STATE['inflight'].add(key)
        if fault == 'slow':
            time.sleep(SLOW_SECONDS)                 # outside the lock, so a request that arrives meanwhile is answered
        with LOCK:
            try:
                if fault == 'column_value':
                    out = {'data': {'create_item': None}, 'errors': [{
                        'message': f'invalid value, please check our API documentation for the correct data structure for this column. {DOCS}',
                        'locations': [{'line': 1, 'column': 80}], 'path': ['create_item'],
                        'extensions': {'code': 'ColumnValueException', 'status_code': 200,
                                       'error_data': {'column_value': '{"label"=>"New"}', 'column_type': 'StatusColumn'}}}], 'extensions': rid}
                elif fault == 'bad_column':
                    out = {'errors': [{'message': "This column ID doesn't exist for the board", 'locations': [{'line': 1, 'column': 120}],
                                       'path': ['items_page_by_column_values'], 'extensions': {'code': 'InvalidColumnIdException', 'status_code': 200,
                                                                                               'error_data': {'column_id': COL['status']}}}], 'extensions': rid}
                else:
                    result = graphql_sync(SCHEMA, body['query'], root_value=ROOT, variable_values=variables, operation_name=body.get('operationName'))
                    out = {}
                    if result.data is not None:
                        out['data'] = result.data
                    if result.errors:
                        out['errors'] = [e.formatted for e in result.errors]
                        for e in out['errors']:
                            e.setdefault('extensions', {}).setdefault('code', 'GRAPHQL_VALIDATION_FAILED' if result.data is None else 'error')
                        if not any((e.get('extensions') or {}).get('status_code') for e in out['errors']):
                            entry['rejected'] = 'graphql: ' + '; '.join(e['message'] for e in out['errors'])
                    out['extensions'] = rid
                if key:
                    # The docs do not say whether an answer with errors is kept; the mock keeps every 200 answer,
                    # the stricter case: the same key would get the same refusal back for 30 minutes.
                    STATE['idem'][key] = out
            finally:
                if key:
                    STATE['inflight'].discard(key)
            if fault == 'drop':     # monday did the work, but the answer never reaches n8n
                entry['dropped'] = True
                return reply(504, '<html><body><h1>504 Gateway Time-out</h1></body></html>')
        return reply(200, out)


# ----------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def js_string(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def test_copy(source, env, paths):
    """A workflow file with $env values replaced by test values and the monday host pointed at the mock."""
    wf = json.loads(source.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: js_string(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for n in wf['nodes']:
        n['parameters'] = fix(n.get('parameters', {}))
        if n['name'] in paths:
            n['parameters']['path'] = paths[n['name']]
            n['webhookId'] = str(uuid.uuid4())
        if n['type'] == 'n8n-nodes-base.scheduleTrigger':
            # The test drives the SLA check through Run Now; the copy's schedule only fires on 1 January at 03:00.
            n['parameters']['rule'] = {'interval': [{'field': 'cronExpression', 'expression': '0 0 3 1 1 *'}]}
    return wf


def alerts_in_log():
    """Slack alerts from the error workflow, as {execution id: text}. Each alert ends with the failed run's link."""
    out = {}
    for e in list(LOG):
        if e['service'] == 'slack' and e.get('hook') == 'alerts' and e.get('text'):
            m = re.search(r'/executions/(\d+)\s*$', e['text'])
            if m:
                out[m.group(1)] = e['text']
    return out


class Runner:
    def __init__(self, wid, paths):
        self.wid, self.paths, self.sent, self.mark, self.results = wid, paths, 0, 0, []
        self.flagged = set()       # runs that must fail and raise an alert
        self.all_runs = set()

    def wait(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            data = api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])
            done = [e for e in data if e.get('stoppedAt')]
            if len(done) >= self.sent:
                for e in done:
                    self.all_runs.add(str(e['id']))
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def _send(self, which, payload, headers=None, query='', form=False):
        data = urlencode(payload).encode() if form else json.dumps(payload).encode()
        ctype = 'application/x-www-form-urlencoded' if form else 'application/json'
        req = urllib.request.Request(f"{N8N}/webhook/{self.paths[which]}{query}", method='POST', data=data,
                                     headers={'Content-Type': ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                status, text, rh = r.status, r.read().decode(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, text, rh = e.code, e.read().decode(), {k.lower(): v for k, v in e.headers.items()}
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = text
        return status, body, rh

    def post(self, which, payload, headers=None, query='', form=False):
        """Sends one webhook, waits for the run to finish. Returns (status, json body, response headers, execution)."""
        self.mark = len(LOG)
        status, body, rh = self._send(which, payload, headers, query, form)
        self.sent += 1
        ex = self.wait()
        return status, body, rh, ex

    def post_together(self, which, payloads, headers):
        """Sends several webhooks at the same moment (a form tool that redelivers while the first try is running)."""
        self.mark = len(LOG)
        answers = [None] * len(payloads)

        def go(i):
            answers[i] = self._send(which, payloads[i], headers)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(len(payloads))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.sent += len(payloads)
        self.wait()
        return answers

    def lead(self, payload, secret=LEAD_SECRET):
        return self.post('Lead Webhook', payload, {'X-Webhook-Secret': secret} if secret is not None else {})

    def run_now(self, secret=RUN_SECRET):
        return self.post('Run Now', {}, {'X-Run-Secret': secret} if secret is not None else {})

    def ended(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service, op=None, hook=None):
        """Requests since the last post. Slack alerts from the error workflow are left out unless hook='alerts'."""
        return [e for e in LOG[self.mark:] if e['service'] == service and (op is None or e.get('op') == op)
                and (e.get('hook') == hook if hook else e.get('hook') != 'alerts')]

    def alert(self, ex, timeout=30):
        """Marks this run as one that must raise an alert and waits for the error workflow's Slack message about it."""
        self.flagged.add(str(ex['id']))
        end = time.time() + timeout
        while time.time() < end:
            hit = alerts_in_log().get(str(ex['id']))
            if hit:
                return hit
            time.sleep(0.4)
        return ''

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def variables(entry):
    return (entry.get('body') or {}).get('variables') or {}


def values_of(entry, name='values'):
    raw = variables(entry).get(name)
    return json.loads(raw) if isinstance(raw, str) else None


def lead(name, email='', phone='', message='', company='', source='Website contact form', sub=None):
    p = {'name': name, 'email': email, 'phone': phone, 'company': company, 'message': message, 'source': source}
    if sub:
        p['submissionId'] = sub
    return {k: v for k, v in p.items() if v}


def seed():
    """The board before the test: leads typed in by hand and one similar looking phone number."""
    new_item('Morgan Lane', {COL['email']: {'email': 'morgan.lane@example.com', 'text': 'morgan.lane@example.com'},
                             COL['phone']: {'phone': '+12025550199', 'countryShortName': 'US'}, COL['status']: {'label': 'Working on it'},
                             COL['owner']: {'personsAndTeams': [{'id': ALEX, 'kind': 'person'}]}}, created=now_utc() - timedelta(days=9))
    new_item('Riley Stone', {COL['phone']: {'phone': '2025550177', 'countryShortName': 'US'}, COL['status']: {'label': 'Contacted'},
                             COL['owner']: {'personsAndTeams': [{'id': SAM, 'kind': 'person'}]}}, created=now_utc() - timedelta(days=5))
    new_item('Kai Becker', {COL['email']: {'email': 'kai.becker@example.com', 'text': 'kai.becker@example.com'},
                            COL['phone']: {'phone': '+492025550188', 'countryShortName': 'DE'}, COL['status']: {'label': 'Qualified'},
                            COL['owner']: {'personsAndTeams': [{'id': DANA, 'kind': 'person'}]}}, created=now_utc() - timedelta(days=3))
    new_item('Jordan Blake', {COL['email']: {'email': 'jordan.blake@example.com', 'text': 'jordan.blake@example.com'},
                              COL['status']: {'label': 'Contacted'}, COL['owner']: {'personsAndTeams': [{'id': SAM, 'kind': 'person'}]}},
             created=now_utc() - timedelta(days=2))


# ----------------------------------------------------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------------------------------------------------
CREATED = []   # (lead name, owner id or None) in the order the workflow created items
SAMPLES = {}   # messages for the test report


def created_item(r, name):
    items = [i for i in STATE['items'].values() if i['name'] == name]
    if len(items) == 1:
        CREATED.append((name, (owner_of(items[0]) or [None])[0]))
    return items


def next_owner():
    return SAM if CREATED and CREATED[-1][1] == ALEX else ALEX


def run_intake(r):
    # 1. Security
    for label, secret in (('no secret header', None), ('wrong secret', 'guess')):
        status, body, _, ex = r.lead(lead('Jamie Rivera', 'jamie.rivera@example.com'), secret=secret)
        node, _ = r.ended(ex)
        r.check(f'Lead webhook, {label}', 'refused with 401, nothing sent to Monday or Slack',
                status == 401 and body == {'ok': False, 'error': 'unauthorized'} and not r.new('monday') and not r.new('slack') and node == 'Stop: Bad Secret',
                f'{status}, ended at {node}')
        alert = r.alert(ex)
        r.check(f'Lead webhook, {label}', 'the error workflow posts an alert naming the header',
                'failed at *Stop: Bad Secret*' in alert and 'X-Webhook-Secret' in alert, alert.splitlines()[0] if alert else 'no alert')
    SAMPLES['secret_alert'] = alert

    # 2. Validation
    status, body, _, ex = r.lead({'name': 'No Contact', 'message': 'Call me'})
    r.check('Lead with no email and no phone', 'refused with 422 and the reason, Monday not called',
            status == 422 and body == {'ok': False, 'error': 'invalid_lead', 'details': ['an email or a phone number is required']} and not r.new('monday'),
            f'{status} {body}')
    status, body, _, ex = r.lead({'name': 'Typo Person', 'email': 'not-an-email', 'phone': '12345'})
    r.check('Lead with a bad email and a bad phone', 'refused with 422 naming both fields',
            status == 422 and body.get('details') == ['email is not a valid address', 'phone is not a full phone number'] and not r.new('monday'),
            f"{status} {body.get('details') if isinstance(body, dict) else body}")

    # 3. A new lead
    jamie = lead('Jamie Rivera', 'Jamie.Rivera@Example.com', '(202) 555-0101', 'Need a quote for a new roof.\nThe house is two floors.',
                 'Rivera Roofing', sub='sub-1001')
    status, body, _, ex = r.lead(jamie)
    calls, slack = r.new('monday'), r.new('slack')
    items = created_item(r, 'Jamie Rivera')
    item_id = items[0]['id'] if items else ''
    r.check('New lead', 'answered 200 created with the new item ID', status == 200 and body == {'ok': True, 'status': 'created', 'itemId': item_id}, f'{status} {body}')
    find = [c for c in calls if c['op'] == 'FindLead']
    fv = variables(find[0]) if find else {}
    r.check('New lead', 'one search by email (lowercase and as typed) and by phone (digits only, no country code)',
            len(find) == 1 and fv.get('board') == BOARD and fv.get('emailCol') == COL['email'] and fv.get('emails') == ['jamie.rivera@example.com', 'Jamie.Rivera@Example.com']
            and fv.get('phoneCol') == COL['phone'] and fv.get('phones') == ['2025550101']
            and 'by_email: items_page_by_column_values' in find[0]['body']['query'] and 'by_phone: items_page_by_column_values' in find[0]['body']['query'],
            json.dumps({k: fv.get(k) for k in ('emails', 'phones')}))
    create = [c for c in calls if c['op'] == 'CreateLead']
    cv = variables(create[0]) if create else {}
    values = values_of(create[0]) if create else None
    expected = {COL['status']: {'label': 'New'}, COL['email']: {'email': 'jamie.rivera@example.com', 'text': 'jamie.rivera@example.com'},
                COL['phone']: {'phone': '+12025550101', 'countryShortName': 'US'}, COL['owner']: {'personsAndTeams': [{'id': ALEX, 'kind': 'person'}]},
                COL['message']: {'text': 'Need a quote for a new roof.\nThe house is two floors.'}, COL['company']: 'Rivera Roofing', COL['source']: 'Website contact form'}
    r.check('New lead', 'create_item with column values as one JSON string, status New, owner Alex, in the configured group',
            len(create) == 1 and values is not None and set(values) == set(expected) | {COL['last']} and {k: values.get(k) for k in expected} == expected
            and re.fullmatch(r'\d{4}-\d\d-\d\d', values.get(COL['last'], {}).get('date', '')) and cv.get('group') == 'new_leads'
            and cv.get('name') == 'Jamie Rivera' and len(items) == 1 and status_of(items[0]) == 'New' and items[0]['group'] == 'new_leads'
            and items[0]['values'][COL['phone']]['phone'] == '+12025550101',
            'column_values: ' + (cv.get('values', '')[:90] + '...' if cv else 'none'))
    jamie_key = create[0]['headers'].get('idempotency-key', '') if create else ''
    r.check('New lead', 'every Monday call has the token, API-Version 2026-10 and a JSON body; the create has an Idempotency-Key built from the submission',
            len(calls) == 2 and all(c['headers'].get('authorization') == TOKEN and c['headers'].get('api-version') == VERSION
                                    and c['headers'].get('content-type', '').startswith('application/json') for c in calls)
            and re.fullmatch(r'lead-[0-9a-z]+-a0-create', jamie_key), f'{len(calls)} calls, key {jamie_key}')
    text = slack[0].get('text', '') if slack else ''
    r.check('New lead', 'one Slack message in the team channel that mentions Alex and links the item',
            len(slack) == 1 and slack[0]['hook'] == 'team' and text.startswith('<@U0ALEX> New lead for you: *Jamie Rivera*')
            and f'<https://{ACCOUNT}.monday.com/boards/{BOARD}/pulses/{item_id}|Open in Monday>' in text and 'Company: Rivera Roofing' in text
            and 'Phone: +12025550101' in text, text.splitlines()[0] if text else 'no message')
    SAMPLES['new_lead'] = text

    # 4. The form delivers the same submission again
    status, body, _, ex = r.lead(jamie)
    r.check('Same submission delivered twice', 'answered 200 duplicate, no Monday call, no Slack message',
            status == 200 and body == {'ok': True, 'status': 'duplicate', 'itemId': item_id} and not r.new('monday') and not r.new('slack'),
            f'{status} {body}')

    # 5. Jamie comes back with a new question (different case, different phone format, HTML in the message)
    status, body, _, ex = r.lead(lead('Jamie Rivera', 'JAMIE.RIVERA@example.com', '202.555.0101', 'Can you also check the gutters? <b>Urgent</b> & thanks',
                                      source='Pricing page form', sub='sub-1002'))
    upd = r.new('monday', 'UpdateLead')
    uv = variables(upd[0]) if upd else {}
    refresh = values_of(upd[0], 'refresh') if upd else None
    r.check('Returning lead (same email, new question)', 'no new item: one update on the existing item, HTML escaped',
            status == 200 and body == {'ok': True, 'status': 'updated', 'itemId': item_id} and not r.new('monday', 'CreateLead') and len(upd) == 1
            and uv.get('item') == item_id and len(board_items(email='jamie.rivera@example.com')) == 1
            and uv.get('body', '').startswith('<p><strong>New inquiry from the website:</strong> Can you also check the gutters? &lt;b&gt;Urgent&lt;/b&gt; &amp; thanks</p>')
            and len(STATE['items'][item_id]['updates']) == 1 and re.fullmatch(r'lead-[0-9a-z]+-a0-update', upd[0]['headers'].get('idempotency-key', '')),
            (uv.get('body') or '')[:110])
    r.check('Returning lead (same email, new question)', 'refreshes only last inquiry and source; owner, status and contact details untouched',
            refresh is not None and set(refresh) == {COL['last'], COL['source']} and refresh[COL['source']] == 'Pricing page form' and 'fill' not in uv
            and STATE['items'][item_id]['values'][COL['source']] == 'Pricing page form'
            and owner_of(STATE['items'][item_id]) == [ALEX] and status_of(STATE['items'][item_id]) == 'New',
            f'refreshed {sorted(refresh or {})}')
    slack = r.new('slack')
    r.check('Returning lead (same email, new question)', 'the owner (Alex) hears about it in Slack',
            len(slack) == 1 and slack[0]['hook'] == 'team' and slack[0]['text'].startswith('<@U0ALEX> Returning lead: *Jamie Rivera* sent a new inquiry (matched by email).'),
            slack[0]['text'].splitlines()[0] if slack else 'no message')
    SAMPLES['returning'] = slack[0]['text'] if slack else ''

    # 6. Round robin: Sam next, then Pat is away so Alex
    r.lead(lead('Taylor Brooks', 'taylor.brooks@example.com', '+1 202 555 0102', 'Bathroom remodel'))
    created_item(r, 'Taylor Brooks')
    sam_msg = r.new('slack', hook='sam')
    r.lead(lead('Morgan Diaz', 'morgan.diaz@example.com', message='Kitchen lights'))
    created_item(r, 'Morgan Diaz')
    r.check('Round robin', 'second lead goes to Sam on his own webhook, third skips Pat (away) and goes to Alex',
            CREATED[-2:] == [('Taylor Brooks', SAM), ('Morgan Diaz', ALEX)] and len(sam_msg) == 1 and sam_msg[0]['text'].startswith('<@U0SAM> New lead for you'),
            f'owners {[USERS.get(o) for _, o in CREATED]}')

    # 7. A lead typed in by hand with a national phone number and no email
    riley_id = board_items(name='Riley Stone')[0]['id']
    status, body, _, ex = r.lead(lead('Riley Stone', 'riley.stone@example.com', '+1-202-555-0177', 'Following up on my estimate'))
    upd = r.new('monday', 'UpdateLead')
    fill = values_of(upd[0], 'fill') if upd else None
    slack = r.new('slack')
    r.check('Returning lead (phone saved as 2025550177)', 'matched by phone, the empty email is filled in, no new item, Sam told',
            status == 200 and body.get('status') == 'updated' and body.get('itemId') == riley_id and not r.new('monday', 'CreateLead')
            and fill == {COL['email']: {'email': 'riley.stone@example.com', 'text': 'riley.stone@example.com'}}
            and STATE['items'][riley_id]['values'][COL['email']]['email'] == 'riley.stone@example.com'
            and len(board_items(name='Riley Stone')) == 1 and len(slack) == 1 and slack[0]['hook'] == 'sam' and '(matched by phone)' in slack[0]['text'],
            f"{body}, filled {sorted(fill or {})}")

    # 8. A different person whose number contains the same digits (German number +49 202 5550188)
    status, body, _, ex = r.lead(lead('Noah Price', 'noah.price@example.com', '(202) 555-0188', 'Fence repair'))
    find = r.new('monday', 'FindLead')
    phone_hits = len(((find[0].get('response') or {}).get('data') or {}).get('by_phone', {}).get('items', [])) if find else 0
    created_item(r, 'Noah Price')
    r.check('Similar phone number, different person', 'Monday returns the German number as a phone hit, the workflow rejects it and creates a new item',
            phone_hits == 1 and status == 200 and body.get('status') == 'created' and not r.new('monday', 'UpdateLead')
            and len(board_items(name='Noah Price')) == 1 and not STATE['items'][board_items(name='Kai Becker')[0]['id']]['updates'],
            f'{phone_hits} phone hit, {body}')

    # 9. Slack injection through the form
    r.lead(lead('<!channel> Eve Mallory', 'eve.mallory@example.com', message='hello <@U0MANAGER1> & <https://evil.example.com|click>'))
    created_item(r, '<!channel> Eve Mallory')
    slack = r.new('slack')
    text = slack[0]['text'] if slack else ''
    r.check('Visitor text with Slack mentions', 'escaped, so it cannot ping the channel, a user or post a fake link',
            text and '<!channel>' not in text and '<@U0MANAGER1>' not in text and '&lt;!channel&gt; Eve Mallory' in text
            and 'hello &lt;@U0MANAGER1&gt; &amp; &lt;https://evil.example.com|click&gt;' in text,
            text.splitlines()[0] if text else 'no message')

    # 10. A phone number that looks complete but that monday's libphonenumber check refuses (area code 555)
    owner = next_owner()
    status, body, _, ex = r.lead(lead('Ari Novak', 'ari.novak@example.com', '(555) 201-0123', 'Need a new water heater'))
    creates = r.new('monday', 'CreateLead')
    items = created_item(r, 'Ari Novak')
    first, second = (values_of(creates[0]) if creates else None), (values_of(creates[1]) if len(creates) > 1 else None)
    err = (((creates[0].get('response') or {}).get('errors') or [{}])[0].get('extensions') or {}) if creates else {}
    keys = [c['headers'].get('idempotency-key', '') for c in creates]
    r.check('Phone number Monday refuses (libphonenumber)', 'Monday refuses the item; it is created again without the phone, which goes into the message',
            len(creates) == 2 and err.get('code') == 'ColumnValueException' and (err.get('error_data') or {}).get('column_type') == 'PhoneColumn'
            and first.get(COL['phone']) == {'phone': '+15552010123', 'countryShortName': 'US'} and COL['phone'] not in second
            and second.get(COL['message']) == {'text': 'Need a new water heater\n\nPhone (Monday did not accept it): (555) 201-0123'}
            and second.get(COL['email']) == first.get(COL['email']) and second.get(COL['owner']) == first.get(COL['owner'])
            and keys[1] == keys[0] + '-without-phone' and len(items) == 1 and COL['phone'] not in items[0]['values']
            and status == 200 and body == {'ok': True, 'status': 'created', 'itemId': items[0]['id']} and owner_of(items[0]) == [owner],
            f"{len(creates)} create calls, {status} {body}")
    slack = r.new('slack')
    text = slack[0]['text'] if slack else ''
    r.check('Phone number Monday refuses (libphonenumber)', 'the owner is told to check the number; no alert, the lead is saved',
            len(slack) == 1 and slack[0]['hook'] == OWNER_HOOK[owner] and 'New lead for you: *Ari Novak*' in text
            and 'Monday did not accept the phone number (555) 201-0123, so the lead was saved without it.' in text
            and r.ended(ex)[0] == 'Slack: Notify Owner', text.splitlines()[-2] if text else 'no message')
    SAMPLES['phone_refused'] = text

    # 11. A returning lead whose new phone number monday refuses: the inquiry and the refresh are still saved
    jordan_id = board_items(name='Jordan Blake')[0]['id']
    status, body, _, ex = r.lead(lead('Jordan Blake', 'jordan.blake@example.com', '(555) 201-0144', 'Can we move the visit to Friday?', source='Booking form'))
    upd = r.new('monday', 'UpdateLead')
    resp = (upd[0].get('response') or {}) if upd else {}
    data, errors = resp.get('data') or {}, resp.get('errors') or []
    slack = r.new('slack')
    text = slack[0]['text'] if slack else ''
    r.check('Returning lead with a phone Monday refuses', 'Monday answers with partial data: update and refresh saved, the phone refused; answered 200 updated, Sam warned',
            len(upd) == 1 and data.get('update') and data.get('refresh') and data.get('fill') is None and len(errors) == 1 and errors[0].get('path') == ['fill']
            and status == 200 and body == {'ok': True, 'status': 'updated', 'itemId': jordan_id} and len(STATE['items'][jordan_id]['updates']) == 1
            and STATE['items'][jordan_id]['values'][COL['source']] == 'Booking form' and COL['phone'] not in STATE['items'][jordan_id]['values']
            and len(slack) == 1 and slack[0]['hook'] == 'sam' and 'Monday did not save the missing contact details: ColumnValueException' in text,
            f"{body}, errors on {[e.get('path') for e in errors]}")

    # 12. monday's complexity budget is exhausted once (HTTP 429)
    FAIL['monday'].append({'op': 'FindLead', 'kind': 'complexity', 'times': 1})
    status, body, _, ex = r.lead(lead('Quinn Harper', 'quinn.harper@example.com', message='New deck'))
    created_item(r, 'Quinn Harper')
    finds = r.new('monday', 'FindLead')
    r.check('Monday complexity budget exhausted once (429)', 'retried, then saved normally',
            [c['status'] for c in finds] == [429, 200] and status == 200 and body.get('status') == 'created' and len(board_items(name='Quinn Harper')) == 1,
            f"search attempts {[c['status'] for c in finds]}")

    # 13. monday is down for this lead, then the form retries
    FAIL['monday'].append({'op': 'FindLead', 'kind': 'server', 'times': 3})
    rowan = lead('Rowan Ellis', 'rowan.ellis@example.com', '202 555 0105', 'Window replacement', sub='sub-2001')
    status, body, rh, ex = r.lead(rowan)
    node, error = r.ended(ex)
    r.check('Monday down (500)', 'retried 3 times, the form gets 503 with Retry-After, the run fails with the lead in the message',
            len(r.new('monday', 'FindLead')) == 3 and status == 503 and body == {'ok': False, 'error': 'not_saved', 'retry': True}
            and rh.get('retry-after') == '60' and node == 'Stop: Lead Not Saved' and not r.new('slack'), f'{status}, ended at {node}')
    alert = r.alert(ex)
    r.check('Monday down (500)', 'the Slack alert carries the whole lead, so it can be entered by hand',
            'failed at *Stop: Lead Not Saved*' in alert and '"email":"rowan.ellis@example.com"' in alert and '"phone":"202 555 0105"' in alert
            and '"message":"Window replacement"' in alert and '"submissionId":"sub-2001"' in alert, alert.splitlines()[0] if alert else 'no alert')
    SAMPLES['down_alert'] = alert
    status, body, _, ex = r.lead(rowan)
    created_item(r, 'Rowan Ellis')
    r.check('Monday back, the form retries the same submission', 'saved once (the failed try was not remembered as done)',
            status == 200 and body.get('status') == 'created' and len(board_items(name='Rowan Ellis')) == 1, f'{status} {body}')

    # 14. monday refuses the create with a GraphQL error (HTTP 200), then the form retries
    FAIL['monday'].append({'op': 'CreateLead', 'kind': 'column_value', 'times': 1})
    sage = lead('Sage Morris', 'sage.morris@example.com', message='Deck stain', sub='sub-3001')
    status, body, _, ex = r.lead(sage)
    node, error = r.ended(ex)
    refused = r.new('monday', 'CreateLead')
    r.check('Monday answers 200 with a ColumnValueException', 'not retried (it would fail again), the form gets 503, no Slack message',
            len(refused) == 1 and status == 503 and node == 'Stop: Lead Not Saved' and 'ColumnValueException' in error
            and not board_items(name='Sage Morris') and not r.new('slack'), f'ended at {node}')
    alert = r.alert(ex)
    r.check('Monday answers 200 with a ColumnValueException', 'the Slack alert quotes Monday\'s error',
            'failed at *Stop: Lead Not Saved*' in alert and 'Monday did not create the item: ColumnValueException' in alert, alert.splitlines()[1][:90] if alert else 'no alert')
    SAMPLES['refused_alert'] = alert
    r.lead(sage)
    retry = r.new('monday', 'CreateLead')
    items = created_item(r, 'Sage Morris')
    k1 = refused[0]['headers'].get('idempotency-key', '') if refused else ''
    k2 = retry[0]['headers'].get('idempotency-key', '') if retry else ''
    owner_then = ((values_of(refused[0]) or {}).get(COL['owner']) or {}).get('personsAndTeams', [{}])[0].get('id') if refused else None
    sd = (api('GET', f'/api/v1/workflows/{r.wid}').get('staticData') or {}).get('global') or {}
    r.check('The form retries after the fix', 'saved once with a new Idempotency-Key (Monday may have kept the refusal under the old one), same owner as the failed try',
            len(items) == 1 and len(retry) == 1 and not retry[0].get('replayed') and k1.endswith('-a0-create') and k2 == k1.replace('-a0-', '-a1-')
            and CREATED[-1][1] == owner_then and not sd.get('failed'), f'keys {k1[-10:]} then {k2[-10:]}, owner {USERS.get(CREATED[-1][1])}')

    # 15. monday creates the item but the response is lost once
    FAIL['monday'].append({'op': 'CreateLead', 'kind': 'drop', 'times': 1})
    status, body, _, ex = r.lead(lead('Parker Gray', 'parker.gray@example.com', message='Solar panels'))
    creates = r.new('monday', 'CreateLead')
    items = created_item(r, 'Parker Gray')
    keys = [c['headers'].get('idempotency-key') for c in creates]
    r.check('Monday creates the item but the answer is lost (504)', 'the retry sends the same Idempotency-Key, monday replays it, one item',
            len(creates) == 2 and keys[0] and keys[0] == keys[1] and creates[0].get('dropped') and creates[1].get('replayed')
            and len(items) == 1 and status == 200 and body == {'ok': True, 'status': 'created', 'itemId': items[0]['id'] if items else None},
            f'{len(creates)} create calls, {len(items)} item(s)')

    # 16. monday creates the item but all three answers are lost; the form retries later
    FAIL['monday'].append({'op': 'CreateLead', 'kind': 'drop', 'times': 3})
    logan = lead('Logan Pierce', 'logan.pierce@example.com', message='Driveway sealing', sub='sub-5001')
    status, body, _, ex = r.lead(logan)
    creates = r.new('monday', 'CreateLead')
    r.check('Monday saves the item but every answer is lost', 'the form gets 503; the only item is the one Monday made on the first try',
            status == 503 and len(creates) == 3 and [bool(c.get('replayed')) for c in creates] == [False, True, True]
            and len(board_items(name='Logan Pierce')) == 1, f'{status}, {len(creates)} create calls')
    r.alert(ex)
    status, body, _, ex = r.lead(logan)
    logan_items = board_items(name='Logan Pierce')
    r.check('Monday saves the item but every answer is lost', 'the form\'s retry finds that item and logs the inquiry on it: still one item',
            status == 200 and len(logan_items) == 1 and body == {'ok': True, 'status': 'updated', 'itemId': logan_items[0]['id']}
            and not r.new('monday', 'CreateLead') and len(logan_items[0]['updates']) == 1, f'{status} {body}')

    # 17. The form delivers the same new lead twice at the same moment (it timed out and retried at once)
    owner = next_owner()
    FAIL['monday'].append({'op': 'CreateLead', 'kind': 'slow', 'times': 1})
    blair = lead('Blair Quinn', 'blair.quinn@example.com', '202-555-0131', 'Patio cover', sub='sub-6001')
    answers = r.post_together('Lead Webhook', [blair, blair], {'X-Webhook-Secret': LEAD_SECRET})
    creates = r.new('monday', 'CreateLead')
    items = created_item(r, 'Blair Quinn')
    keys = {c['headers'].get('idempotency-key') for c in creates}
    fresh = [c for c in creates if c.get('status') == 200 and not c.get('replayed')]
    slack = r.new('slack')
    r.check('Same new lead delivered twice at the same moment', 'one item: the second create gets 409 while the first runs, then Monday\'s replay',
            len(items) == 1 and len(fresh) == 1 and any(c.get('conflict') for c in creates) and any(c.get('replayed') for c in creates)
            and len(keys) == 1 and all(a[0] == 200 and a[1] == {'ok': True, 'status': 'created', 'itemId': items[0]['id']} for a in answers)
            and owner_of(items[0]) == [owner], f"create calls {[(c.get('status'), 'replay' if c.get('replayed') else '') for c in creates]}")
    r.check('Same new lead delivered twice at the same moment', 'both Slack messages (one per delivery) point the same owner to the same item',
            len(slack) == 2 and all(e['hook'] == OWNER_HOOK[owner] and f"/pulses/{items[0]['id']}|Open in Monday>" in e['text'] for e in slack),
            f'{len(slack)} Slack messages')

    # 18. Slack is down for the owner
    owner = next_owner()
    FAIL['slack'].add(OWNER_HOOK[owner])
    status, body, _, ex = r.lead(lead('Reese Clark', 'reese.clark@example.com', message='Gutter guards'))
    node, error = r.ended(ex)
    created_item(r, 'Reese Clark')
    FAIL['slack'].discard(OWNER_HOOK[owner])
    r.check('Slack refuses the owner alert', 'the lead is still saved and answered 200; Slack retried 3 times; run flagged',
            status == 200 and body.get('status') == 'created' and len(board_items(name='Reese Clark')) == 1
            and len(r.new('slack', hook=OWNER_HOOK[owner])) == 3 and node == 'Stop: Notify Failed' and body.get('itemId', '-') in error,
            f'ended at {node}')
    alert = r.alert(ex)
    r.check('Slack refuses the owner alert', 'the Slack alert names the item and says the SLA check still nudges',
            'failed at *Stop: Notify Failed*' in alert and f"item {body.get('itemId')}" in alert and 'The SLA check will still nudge the owner' in alert,
            alert.splitlines()[0] if alert else 'no alert')

    # 19. A plain HTML form tool: urlencoded body, first and last name fields, secret in the URL
    form = {'first_name': 'Avery', 'last_name': 'Nolan', 'email': 'avery.nolan@example.com', 'phone': '202-555-0110', 'comments': 'Quote for a fence'}
    status, body, _, ex = r.post('Lead Webhook', form, query='?key=wrong', form=True)
    r.check('Form tool that cannot set headers, wrong ?key=', 'refused with 401', status == 401 and not r.new('monday'), f'{status}')
    r.alert(ex)
    status, body, _, ex = r.post('Lead Webhook', form, query=f'?key={LEAD_SECRET}', form=True)
    items = created_item(r, 'Avery Nolan')
    r.check('Form tool that cannot set headers, right ?key=', 'urlencoded post accepted; first and last name joined, comments kept as the message',
            status == 200 and body.get('status') == 'created' and len(items) == 1
            and items[0]['values'].get(COL['message'], {}).get('text') == 'Quote for a fence'
            and items[0]['values'].get(COL['phone'], {}).get('phone') == '+12025550110', f'{status} {body}')

    expect = [ALEX if i % 2 == 0 else SAM for i in range(len(CREATED))]
    r.check('Round robin over all new leads', 'Alex, Sam, Alex, Sam ... (Pat is away and never picked, failed tries skip nobody)',
            [o for _, o in CREATED] == expect and len(CREATED) == 13,
            ', '.join(USERS.get(o, 'nobody').split()[0] for _, o in CREATED))


def sla_item(name, hours, owner=None, status='New'):
    values = {COL['status']: {'label': status}, COL['email']: {'email': f"{name.split()[0].lower()}@example.com", 'text': f"{name.split()[0].lower()}@example.com"}}
    if owner:
        values[COL['owner']] = {'personsAndTeams': [{'id': owner, 'kind': 'person'}]}
    return new_item(name, values, created=now_utc() - timedelta(hours=hours))


def by_hook(entries):
    out = {}
    for e in entries:
        out.setdefault(e['hook'], []).append(e.get('text', ''))
    return out


def run_sla(r):
    status, body, _, ex = r.run_now(secret='nope')
    node, _ = r.ended(ex)
    r.check('Run Now, wrong secret', 'refused with 401, Monday not called', status == 401 and not r.new('monday') and node == 'Stop: Bad Run Secret', f'{status}, ended at {node}')
    alert = r.alert(ex)
    r.check('Run Now, wrong secret', 'the error workflow posts an alert', 'failed at *Stop: Bad Run Secret*' in alert, alert.splitlines()[0] if alert else 'no alert')

    # 200 fresh New leads first, so every overdue lead below lands on the third page
    for i in range(200):
        sla_item(f'Fresh Lead {i:03d}', 0.2, ALEX)
    s = {key: sla_item(*args) for key, args in {
        'avery': ('Avery Stone', 2.5, ALEX), 'blake': ('Blake Hunter', 3.0, SAM), 'casey': ('Casey Fox', 5.0, ALEX),
        'drew': ('Drew Park', 1.0, ALEX), 'emery': ('Emery Cole', 3.0, ALEX, 'Working on it'), 'finley': ('Finley Moss', 2.5),
        'gray': ('Gray Lin', 2.5, DANA), 'harper': ('Harper Quinn', 2.5, PAT)}.items()}
    new_total = len([i for i in STATE['items'].values() if status_of(i) == 'New'])
    status, body, _, ex = r.run_now()
    calls, msgs = r.new('monday'), by_hook(r.new('slack'))
    pages = [len(((c.get('response') or {}).get('data') or {}).get('items_page_by_column_values' if c['op'] == 'NewLeads' else 'next_items_page', {}).get('items', [])) for c in calls]
    cursors = [((c.get('response') or {}).get('data') or {}).get('items_page_by_column_values' if c['op'] == 'NewLeads' else 'next_items_page', {}).get('cursor') for c in calls]
    first = calls[0] if calls else {}
    r.check('SLA check', 'Run Now answers 202, then reads every New item page by page (3 pages, each cursor passed on)',
            status == 202 and body == {'ok': True, 'started': True} and [c['op'] for c in calls] == ['NewLeads', 'NextLeads', 'NextLeads']
            and variables(first).get('labels') == ['New'] and variables(first).get('statusCol') == COL['status'] and variables(first).get('limit') == 100
            and [variables(c).get('cursor') for c in calls[1:]] == cursors[:2] and cursors[2] is None and sum(pages) == new_total,
            f"{[c['op'] for c in calls]}, items per page {pages}")
    url = lambda k: f"<https://{ACCOUNT}.monday.com/boards/{BOARD}/pulses/{s[k]['id']}|{s[k]['name']}>"
    team = msgs.get('team', [])
    alex = [m for m in team if m.startswith('<@U0ALEX>')]
    unknown = [m for m in team if 'need an owner.*' in m]
    r.check('SLA check', 'Alex is nudged once for Avery (2.5 h, on the last page), nothing for Drew (1 h) or Emery (not New)',
            len(alex) == 1 and alex[0] == f'<@U0ALEX> This lead is still New after more than 2 h. Please reach out:\n- {url("avery")}, waiting 2.5 h'
            and all(url(k) not in ''.join(sum(msgs.values(), [])) for k in ('drew', 'emery')), alex[0].replace('\n', ' | ') if alex else 'no message')
    SAMPLES['nudge'] = alex[0] if alex else ''
    r.check('SLA check', 'Sam is nudged on his own webhook for Blake (3 h)',
            msgs.get('sam') and len(msgs['sam']) == 1 and msgs['sam'][0] == f'<@U0SAM> This lead is still New after more than 2 h. Please reach out:\n- {url("blake")}, waiting 3.0 h',
            (msgs.get('sam') or ['no message'])[0].replace('\n', ' | '))
    r.check('SLA check', 'the manager gets one escalation for Casey (5 h, past twice the SLA), naming the owner',
            msgs.get('manager') and len(msgs['manager']) == 1
            and msgs['manager'][0] == f'<@U0MANAGER1> *SLA missed:* 1 lead is still New after more than 4 h.\n- {url("casey")}, owner <@U0ALEX>, waiting 5.0 h',
            (msgs.get('manager') or ['no message'])[0].replace('\n', ' | '))
    SAMPLES['escalation'] = (msgs.get('manager') or [''])[0]
    r.check('SLA check', 'leads with no owner, an owner outside OWNERS_JSON or an owner who is away go to the team channel',
            len(unknown) == 1 and unknown[0].startswith('*3 leads need an owner.* Still New after more than 2 h') and f"{url('finley')}, owner nobody" in unknown[0]
            and f"{url('gray')}, owner Dana Cole" in unknown[0] and f"{url('harper')}, owner Pat Kim (away)" in unknown[0] and len(team) == 2,
            unknown[0].replace('\n', ' | ') if unknown else 'no message')
    SAMPLES['team_list'] = unknown[0] if unknown else ''

    status, body, _, ex = r.run_now()
    r.check('SLA check again right away', 'no reminder is repeated', not r.new('slack') and len(r.new('monday')) == 3, f"{len(r.new('slack'))} Slack messages")

    s['avery']['created_at'] -= timedelta(hours=2)        # Avery now waits 4.5 h
    r.run_now()
    msgs = by_hook(r.new('slack'))
    r.check('SLA check after Avery passes twice the SLA', 'one escalation for Avery only, nothing else',
            list(msgs) == ['manager'] and len(msgs['manager']) == 1 and url('avery') in msgs['manager'][0] and url('casey') not in msgs['manager'][0],
            msgs.get('manager', ['no message'])[0].replace('\n', ' | '))
    r.run_now()
    r.check('SLA check once more', 'the escalation is not repeated either', not r.new('slack'), f"{len(r.new('slack'))} Slack messages")

    # Slack refuses Sam's webhook while Alex's nudge goes through. Sam's alert is first in line on purpose:
    # that is the case where n8n's retry of a multi-item node would send Alex's message again on every retry.
    s['jules'], s['indigo'] = sla_item('Jules Kerr', 2.5, SAM), sla_item('Indigo Ray', 2.5, ALEX)
    FAIL['slack'].add('sam')
    status, body, _, ex = r.run_now()
    node, error = r.ended(ex)
    sent = [(e['hook'], e['status']) for e in r.new('slack')]
    msgs = by_hook(r.new('slack'))
    FAIL['slack'].discard('sam')
    r.check('SLA check while Sam\'s Slack webhook is gone', 'Sam\'s nudge is retried 3 times, Alex is nudged exactly once, run flagged',
            sent == [('sam', 404)] * 3 + [('team', 200)] and url('indigo') in msgs['team'][0]
            and node == 'Stop: SLA Alert Failed' and 'no_service' in error, f'Slack calls {sent}, ended at {node}')
    alert = r.alert(ex)
    r.check('SLA check while Sam\'s Slack webhook is gone', 'the Slack alert says one reminder failed and will be sent again',
            'failed at *Stop: SLA Alert Failed*' in alert and 'Slack refused 1 SLA alert(s). They are sent again on the next run.' in alert,
            alert.splitlines()[1][:80] if alert else 'no alert')
    r.run_now()
    msgs = by_hook(r.new('slack'))
    r.check('SLA check after Slack recovers', 'only Sam\'s missed nudge is sent; Alex is not nudged twice',
            list(msgs) == ['sam'] and len(msgs['sam']) == 1 and url('jules') in msgs['sam'][0] and url('blake') not in msgs['sam'][0],
            (msgs.get('sam') or ['no message'])[0].replace('\n', ' | '))

    # A lead someone picked up leaves the list and its record is dropped
    s['blake']['values'][COL['status']] = coerce(COL['status'], {'label': 'Contacted'}, False)
    r.run_now()
    sd = api('GET', f'/api/v1/workflows/{r.wid}').get('staticData') or {}
    sla_state = (sd.get('global') or {}).get('sla') or {}
    r.check('SLA memory', 'a lead that left New is forgotten; the others are remembered with the time of each reminder',
            not r.new('slack') and s['blake']['id'] not in sla_state and set(sla_state) == {s[k]['id'] for k in ('avery', 'casey', 'finley', 'gray', 'harper', 'indigo', 'jules')}
            and sla_state[s['casey']['id']].get('escalatedAt') and sla_state[s['jules']['id']].get('nudgedAt') and not sla_state[s['jules']['id']].get('escalatedAt'),
            f'{len(sla_state)} remembered')

    # monday refuses the query (GraphQL error with HTTP 200), then monday is down (HTTP 500)
    FAIL['monday'].append({'op': 'NewLeads', 'kind': 'bad_column', 'times': 1})
    status, body, _, ex = r.run_now()
    node, error = r.ended(ex)
    r.check('SLA check, Monday answers 200 with an error', 'the run stops with the error, no reminders',
            node == 'Collect Page' and 'InvalidColumnIdException' in error and not r.new('slack') and len(r.new('monday')) == 1, f'ended at {node}: {error[:60]}')
    alert = r.alert(ex)
    r.check('SLA check, Monday answers 200 with an error', 'the Slack alert keeps the whole reason (no text lost at ": ")',
            'failed at *Collect Page*' in alert and 'Monday refused the SLA query. InvalidColumnIdException (This column ID doesn\'t exist for the board).' in alert,
            alert.splitlines()[1][:90] if alert else 'no alert')
    SAMPLES['sla_alert'] = alert
    FAIL['monday'].append({'op': 'NewLeads', 'kind': 'server', 'times': 3})
    status, body, _, ex = r.run_now()
    node, error = r.ended(ex)
    r.check('SLA check, Monday down (500)', 'retried 3 times, the run stops, no reminders',
            node == 'Stop: SLA Query Failed' and len(r.new('monday')) == 3 and not r.new('slack'), f'ended at {node}')
    alert = r.alert(ex)
    r.check('SLA check, Monday down (500)', 'the Slack alert says no reminders were sent', 'failed at *Stop: SLA Query Failed*' in alert
            and 'No reminders were sent' in alert, alert.splitlines()[0] if alert else 'no alert')


def run_variants(away, broken):
    status, body, _, ex = away.lead(lead('Lane Brooks', 'lane.brooks@example.com', message='Porch light'))
    items = board_items(name='Lane Brooks')
    slack = away.new('slack')
    creates = away.new('monday', 'CreateLead')
    away.check('Everyone in OWNERS_JSON is away', 'the lead is created without an owner and the team channel is asked to assign it',
               status == 200 and len(items) == 1 and not owner_of(items[0]) and len(creates) == 1 and COL['owner'] not in (values_of(creates[0]) or {COL['owner']: 1})
               and len(slack) == 1 and slack[0]['hook'] == 'team'
               and slack[0]['text'].startswith('*New lead with no owner:* *Lane Brooks*'), slack[0]['text'][:70] if slack else 'no message')
    status, body, _, ex = broken.lead(lead('Mia Grant', 'mia.grant@example.com', message='Garage door'))
    node, error = broken.ended(ex)
    broken.check('OWNERS_JSON is broken', 'the form gets 503 (not an empty 200), Monday is not called',
                 status == 503 and body == {'ok': False, 'error': 'not_saved', 'retry': True} and not broken.new('monday') and node == 'Stop: Lead Not Saved',
                 f'{status}, ended at {node}')
    alert = broken.alert(ex)
    broken.check('OWNERS_JSON is broken', 'the Slack alert says which variable is wrong and carries the lead',
                 'Config problem: OWNERS_JSON is not valid JSON' in alert and '"email":"mia.grant@example.com"' in alert, alert.splitlines()[1][:90] if alert else 'no alert')


def static_checks(results):
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))
    by = {n['name']: n for n in wf['nodes']}
    sched = by.get('Every 30 Minutes', {})
    feeds = lambda src: [c['node'] for out in wf['connections'].get(src, {}).get('main', []) for c in out]
    results.append(('Workflow file', 'the schedule runs every 30 minutes and feeds the same SLA logic as Run Now',
                    sched.get('parameters', {}).get('rule') == {'interval': [{'field': 'minutes', 'minutesInterval': 30}]}
                    and feeds('Every 30 Minutes') == ['Start SLA Check'] and feeds('Respond: Run Started') == ['Start SLA Check'], ''))
    text = WORKFLOW.read_text(encoding='utf-8')
    http = [n for n in wf['nodes'] if n['type'] == 'n8n-nodes-base.httpRequest']
    results.append(('Workflow file', 'no secrets in the file, every API call retries and has an error output',
                    '$env.MONDAY_API_TOKEN' in text and not re.search(r'eyJ[A-Za-z0-9_-]{20,}|xox[bp]-|hooks\.slack\.com/services/T', text)
                    and all(n.get('retryOnFail') and n.get('maxTries') == 3 and n.get('onError') == 'continueErrorOutput' for n in http)
                    and len(http) == 7, f'{len(http)} HTTP nodes'))


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    seed()
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = f'TEST {SLUG} (temporary)'
    variants = {
        'main': (ENV, {'Lead Webhook': f'{SLUG}-e2e-intake', 'Run Now': f'{SLUG}-e2e-run'}),
        'away': ({**ENV, 'OWNERS_JSON': json.dumps([{**o, 'away': True} for o in OWNERS])},
                 {'Lead Webhook': f'{SLUG}-e2e-away-intake', 'Run Now': f'{SLUG}-e2e-away-run'}),
        'broken': ({**ENV, 'OWNERS_JSON': '[{"name": "Alex", "mondayUserId": 41000001,]'},
                   {'Lead Webhook': f'{SLUG}-e2e-broken-intake', 'Run Now': f'{SLUG}-e2e-broken-run'}),
    }
    runners, created = {}, []
    try:
        err = test_copy(ERROR_WORKFLOW, ERROR_ENV, {})
        err_id = api('POST', '/api/v1/workflows', {'name': name, 'nodes': err['nodes'], 'connections': err['connections'], 'settings': err.get('settings', {})})['id']
        created.append(err_id)
        for key, (env, paths) in variants.items():
            wf = test_copy(WORKFLOW, env, paths)
            wid = api('POST', '/api/v1/workflows', {'name': name, 'nodes': wf['nodes'], 'connections': wf['connections'],
                                                    'settings': {**wf.get('settings', {}), 'errorWorkflow': err_id}})['id']
            created.append(wid)
            runners[key] = Runner(wid, paths)
        for wid in created:      # n8n 2.x only runs an error workflow that is active
            api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        r = runners['main']
        run_intake(r)
        run_sla(r)
        run_variants(runners['away'], runners['broken'])

        monday_calls = [e for e in LOG if e['service'] == 'monday']
        rejected = [e for e in monday_calls if e.get('rejected')]
        r.check('Every Monday request', 'passed the mock schema and format checks (no unknown fields, wrong types or bad variables)',
                not rejected and not NOTES, '; '.join([e['rejected'] for e in rejected] + NOTES)[:200] or f'{len(monday_calls)} requests')
        bad_headers = [e for e in monday_calls if e['headers'].get('authorization') != TOKEN or e['headers'].get('api-version') != VERSION
                       or not e['headers'].get('content-type', '').startswith('application/json')
                       or (e.get('kind') == 'mutation') != bool(e['headers'].get('idempotency-key'))
                       or (e.get('kind') == 'mutation' and not KEY_RE.fullmatch(e['headers'].get('idempotency-key', '')))]
        r.check('Every Monday request', 'sent the token, API-Version 2026-10 and JSON; every mutation and only mutations sent an Idempotency-Key built from the submission',
                monday_calls and not bad_headers, f"{len(bad_headers)} with wrong headers" + (f": {bad_headers[0]['op']} {bad_headers[0]['headers']}"[:160] if bad_headers else ''))

        flagged = set().union(*(x.flagged for x in runners.values()))
        all_runs = set().union(*(x.all_runs for x in runners.values()))
        end = time.time() + 15            # alerts arrive after the failed run has finished
        while time.time() < end and not flagged <= set(alerts_in_log()):
            time.sleep(0.5)
        time.sleep(3)
        alerted = set(alerts_in_log())
        r.check('Error alert workflow', 'every failed run raised exactly one Slack alert, and no run that worked or was refused on purpose (422, duplicate) raised one',
                alerted == flagged and len([e for e in LOG if e.get('hook') == 'alerts']) == len(flagged) and flagged <= all_runs,
                f'{len(alerted)} alerts for {len(flagged)} failed runs out of {len(all_runs)} runs')
    finally:
        for wid in reversed(created):
            try:
                api('POST', f'/api/v1/workflows/{wid}/deactivate')
            except Exception:
                pass
            finally:
                api('DELETE', f'/api/v1/workflows/{wid}')
        server.shutdown()

    if os.environ.get('E2E_DUMP'):   # optional: save every captured request and the sample messages for the test report
        Path(os.environ['E2E_DUMP']).write_text(json.dumps({
            'samples': SAMPLES,
            'log': [{**{k: e.get(k) for k in ('service', 'op', 'hook', 'status', 'body', 'response', 'text', 'replayed', 'dropped', 'conflict')},
                     'idempotency_key': (e.get('headers') or {}).get('idempotency-key')} for e in LOG]},
            indent=1, ensure_ascii=False), encoding='utf-8')

    results = [res for runner in runners.values() for res in runner.results]
    static_checks(results)
    passed = sum(ok for *_, ok, _ in results)
    last = None
    for scenario, label, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
