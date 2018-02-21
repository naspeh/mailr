import base64
import datetime as dt
import functools as ft
import json
import pathlib
import re

from bottle import (
    Bottle, abort, redirect, request, response,
    static_file, template
)

from gevent.pool import Pool

from geventhttpclient import HTTPClient

from pytz import common_timezones, timezone, utc

from . import DEBUG, SECRET, imap, local, log, message
from .schema import validate


root = pathlib.Path(__file__).parent.parent
assets = (root / 'assets/dist').resolve()
app = Bottle()
app.catchall = not DEBUG


def session(callback):
    def inner(*args, **kwargs):
        session = request.get_cookie('session', secret=SECRET)
        if session:
            local.USER = session['username']
        request.session = session
        return callback(*args, **kwargs)
    return inner


def auth(callback):
    def inner(*args, **kwargs):
        if request.session:
            return callback(*args, **kwargs)
        return abort(403)
    return inner


def endpoint(callback):
    def inner(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            response.status = 500
            return {'errors': [str(e)]}
    return inner


def theme_filter(config):
    regexp = r'(%s)?' % '|'.join(re.escape(t) for t in themes())

    def to_python(t):
        return t

    def to_url(t):
        return t
    return regexp, to_python, to_url


app.install(session)
app.install(auth)
app.router.add_filter('theme', theme_filter)


@app.get('/', skip=[auth], name='index')
@app.get('/<theme>/', skip=[auth])
def index(theme=None):
    if not request.session:
        prefix = ('/' + theme) if theme else ''
        login_url = '%s%s' % (prefix, app.get_url('login'))
        return redirect(login_url)

    return render_tpl(theme or request.session['theme'], 'index', {
        'user': request.session['username'],
        'tags': wrap_tags(local.tags_info())
    })


@app.get('/login', skip=[auth], name='login')
@app.get('/<theme>/login', skip=[auth])
def login_html(theme=None):
    return render_tpl(theme or 'base', 'login', {
        'themes': themes(),
        'timezones': common_timezones,
    })


@app.post('/login', skip=[auth])
@endpoint
def login():
    schema = {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'},
            'timezone': {'type': 'string', 'enum': common_timezones},
            'theme': {'type': 'string', 'default': 'base'}
        },
        'required': ['username', 'password', 'timezone']
    }
    errs, data = validate(request.json, schema)
    if errs:
        response.status = 400
        return {'errors': errs, 'schema': schema}

    try:
        local.connect(data['username'], data['password'])
    except imap.Error as e:
        response.status = 400
        return {'errors': ['Authentication failed.'], 'details': str(e)}

    del data['password']
    response.set_cookie('session', data, SECRET)
    return {}


@app.get('/logout')
def logout():
    response.delete_cookie('session')
    return redirect('/login')


@app.get('/nginx', skip=[auth])
def nginx():
    h = request.headers
    try:
        local.connect(h['Auth-User'], h['Auth-Pass'])
        response.set_header('Auth-Status', 'OK')
        response.set_header('Auth-Server', '127.0.0.1')
        response.set_header('Auth-Port', '143')
    except imap.Error as e:
        response.set_header('Auth-Status', str(e))
        response.set_header('Auth-Wait', 3)
    return ''


@app.get('/tags')
@endpoint
def tags():
    return wrap_tags(local.tags_info())


@app.post('/tag')
@endpoint
def tag():
    schema = {
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'pattern': r'^[^\\#]'
            },
        },
        'required': ['name']
    }
    errs, data = validate(request.json, schema)
    if errs:
        response.status = 400
        return {'errors': errs, 'schema': schema}
    tag = local.get_tag(data['name'])
    return wrap_tags({tag['id']: tag})['info'][tag['id']]


@app.post('/search')
@endpoint
def search():
    preload = request.json.get('preload')
    q, opts = parse_query(request.json['q'])
    if opts.get('thread'):
        return thread(q, opts, preload or 4)

    if opts.get('threads'):
        uids = local.search_thrs(q)
        info = ft.partial(local.thrs_info, tags=opts.get('tags'))
        info_url = app.get_url('thrs_info')
    else:
        uids = local.search_msgs(q)
        info = local.msgs_info
        info_url = app.get_url('msgs_info')

    msgs = {}
    preload = preload or 200
    tags = opts.get('tags', [])
    if preload and uids:
        msgs = wrap_msgs(info(uids[:preload]), tags)

    extra = {
        'threads': opts.get('threads', False),
        'tags': tags
    }
    return dict({
        'uids': uids,
        'msgs': msgs,
        'msgs_info': info_url
    }, **{k: v for k, v in extra.items() if v})


@app.post('/thrs/info', name='thrs_info')
@endpoint
def thrs_info():
    uids = request.json['uids']
    hide_tags = request.json.get('hide_tags', [])
    if not uids:
        return abort(400)
    return wrap_msgs(local.thrs_info(uids, hide_tags), hide_tags)


@app.post('/msgs/info', name='msgs_info')
@endpoint
def msgs_info():
    uids = request.json['uids']
    hide_tags = request.json.get('hide_tags', [])
    if not uids:
        return abort(400)
    return wrap_msgs(local.msgs_info(uids), hide_tags)


@app.post('/msgs/body', name='msgs_body')
@endpoint
def msgs_body():
    uids = request.json['uids']
    read = request.json.get('read', True)
    if not uids:
        return abort(400)
    if read:
        unread = local.search_msgs('uid %s unseen' % ','.join(uids))
        if unread:
            local.msgs_flag(unread, [], ['\\Seen'])
    return dict(local.msgs_body(uids))


@app.post('/thrs/link')
@endpoint
def thrs_link():
    uids = request.json['uids']
    if not uids:
        return {}
    return local.link_threads(uids)


@app.post('/msgs/flag')
@endpoint
def msgs_flag():
    schema = {
        'type': 'object',
        'properties': {
            'uids': {'type': 'array'},
            'old': {'type': 'array', 'default': []},
            'new': {'type': 'array', 'default': []}
        },
        'required': ['uids']
    }
    errs, data = validate(request.json, schema)
    if errs:
        response.status = 400
        return {'errors': errs, 'schema': schema}
    local.msgs_flag(**data)


@app.get('/editor/<id>', name='editor')
@app.post('/editor')
def editor(id=None):
    if request.method == 'GET':
        uid = local.search_msgs('HEADER X-Draft-ID %s' % id)
        if not uid:
            return abort(404)
        uid = uid[0]
        return draft_info(uid)

    uid = request.forms['uid']
    files = request.files.getall('files')

    draft = draft_info(uid)
    parts = []
    if draft['files']:
        orig = local.raw_msg(draft['origin_uid'], local.SRC, parsed=True)
        parts = orig.get_payload()[1:]
    msg = message.new_draft(draft, request.forms, files or parts and True)
    for p in parts:
        msg.attach(p)
    for f in files:
        maintype, subtype = f.content_type.split('/')
        msg.add_attachment(
            f.file.read(), filename=f.filename,
            maintype=maintype, subtype=subtype
        )

    local.new_msg(msg, draft['flags'])
    local.del_msg(draft['origin_uid'])


@app.get('/raw/<uid:int>', name='raw')
@app.get('/raw/<uid:int>/<part>')
@app.get('/raw/<uid:int>/<part>/<filename>')
def raw(uid, part=None, filename=None):
    box = request.query.get('box', local.SRC)
    uid = str(uid)
    if request.query.get('parsed') or request.query.get('p'):
        box = local.ALL
        uid = local.pair_origin_uids([uid])[0]

    if part:
        msg, content_type = local.raw_part(uid, box, part)
    else:
        msg = local.raw_msg(uid, box)
        content_type = 'text/plain'

    if msg is None:
        return abort(404)
    response.content_type = content_type
    return msg


@app.get('/proxy')
def proxy():
    url = request.query.get('url')
    if not url:
        return abort(400)

    if url.startswith('//'):
        url = 'https:' + url

    def get(url):
        log.debug('proxy: %s', url)
        try:
            http = HTTPClient.from_url(url)
            res = http.get(url)
        except Exception as e:
            log.error(e)
            abort(503, str(e))
        return res

    res = get(url)
    if res.status_code in (301, 302):
        location = [v for k, v in res.headers if k.lower() == 'location']
        if location:
            res = get(location[0])

    response.status = res.status_code
    keys = (
        'content-type', 'content-length',
        'cache-control', 'expires', 'date', 'last-modified'
    )
    for key, val in res.headers:
        if key in keys:
            response.set_header(key, val)
    return bytes(res.read())


@app.get('/avatars.css')
def avatars():
    hashes = set(request.query['hashes'].split(','))
    size = request.query.get('size', 20)
    default = request.query.get('default', 'identicon')
    cls = request.query.get('cls', '.pic-%s')

    response.content_type = 'text/css'
    return '\n'.join((
        '%s {background-image: url(data:image/gif;base64,%s);}'
        % ((cls % h), i.decode())
    ) for h, i in fetch_avatars(hashes, size, default))


@app.get('/<filepath:path>', skip=[auth])
def serve_assets(filepath):
    return static_file(filepath, root=assets)


# Helpers bellow
tpl = '''
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Mailur: {{title}}</title>
  <link rel="shortcut icon" href="/favicon.png">
  <link href="/{{css}}?{{mtime}}" rel="stylesheet">
  <script>
    window.data={{!data}};
  </script>
</head>
<body>
  <div id="app"/>
  <script type="text/javascript" src="/vendor.js?{{mtime}}"></script>
  <script type="text/javascript" src="/{{js}}?{{mtime}}"></script>
</body>
</html>
'''


def render_tpl(theme, page, data={}):
    data.update(current_theme=theme)
    title = {'index': 'welcome', 'login': 'login'}[page]
    css = assets / ('theme-%s.css' % theme)
    js = assets / ('%s.js' % page)
    mtime = max(i.stat().st_mtime for i in [css, js])
    params = {
        'data': json.dumps(data, sort_keys=True),
        'css': css.name,
        'js': js.name,
        'mtime': mtime,
        'title': title,
    }
    return template(tpl, **params)


@ft.lru_cache(maxsize=None)
def themes():
    pkg = json.loads((root / 'package.json').read_text())
    return sorted(pkg['mailur']['themes'])


def parse_query(q):
    def escape(val):
        return json.dumps(val, ensure_ascii=False)

    def replace(match):
        info = match.groupdict()
        q = match.group()
        flags = {'flagged', 'unflagged', 'seen', 'unseen', 'draft'}
        flags = {k for k in flags if info.get(k)}
        if flags:
            q = ' '.join(flags)
        elif info.get('raw'):
            q = info['raw_val']
        elif info.get('thread'):
            opts['thread'] = True
            q = 'uid %s' % info['thread_id']
        elif info.get('uid'):
            q = 'uid %s' % info['uid_val']
        elif info.get('from'):
            q = 'from %s' % escape(info['from_val'])
        elif info.get('mid'):
            q = 'header message-id %s' % info['mid_val']
        elif info.get('ref'):
            q = (
                'or header message-id {0} header references {0}'
                .format(info['ref_val'])
            )
        elif info.get('subj'):
            val = info['subj_val'].strip('"')
            q = 'header subject %s' % escape(val)
        elif info.get('threads'):
            opts['threads'] = True
            q = ''
        elif info.get('draft_edit'):
            opts['draft'] = info['draft_val']
            opts['thread'] = True
            q = 'header x-draft-id %s' % info['draft_val']
        elif info.get('tag'):
            opts.setdefault('tags', [])
            opts['tags'].append(info['tag_id'])
            q = 'keyword %s' % info['tag_id']
        elif info.get('date'):
            val = info['date_val']
            count = val.count('-')
            if not count:
                date = dt.datetime.strptime(val, '%Y')
                dates = [date, date.replace(year=date.year+1)]
            elif count == 1:
                date = dt.datetime.strptime(val, '%Y-%m')
                dates = [date, date.replace(month=date.month+1)]
            else:
                date = dt.datetime.strptime(val, '%Y-%m-%d')
                dates = [date]

            dates = tuple(i.strftime('%d-%b-%Y') for i in dates)
            if len(dates) == 1:
                q = 'on %s' % dates
            else:
                q = 'since %s before %s' % dates
        if q:
            parts.append(q)
        return ' '

    opts = {}
    parts = []
    q = re.sub(
        '(?i)[ ]?('
        '(?P<raw>:raw)(?P<raw_val>.*)'
        '|(?P<thread>thr(ead)?:)(?P<thread_id>\d+)'
        '|(?P<threads>:threads)'
        '|(?P<tag>(tag|in|has):)(?P<tag_id>[^ ]+)'
        '|(?P<subj>subj(ect)?:)(?P<subj_val>("[^"]*"|[\S]*))'
        '|(?P<from>from:)(?P<from_val>[^ ]+)'
        '|(?P<mid>(message_id|mid):)(?P<mid_val>[^ ]+)'
        '|(?P<ref>ref:)(?P<ref_val>[^ ]+)'
        '|(?P<uid>uid:)(?P<uid_val>\d+)'
        '|(?P<date>date:)(?P<date_val>\d{4}(-\d{2}(-\d{2})?)?)'
        '|(?P<draft>:(draft))'
        '|(?P<unseen>:(unread|unseen))'
        '|(?P<seen>:(read|seen))'
        '|(?P<flagged>:(pin(ned)?|flagged))'
        '|(?P<unflagged>:(unpin(ned)?|unflagged))'
        '|(?P<draft_edit>draft:(?P<draft_val>\<.{8}\>))'
        ')( |$)',
        replace, q
    )
    q = re.sub('[ ]+', ' ', q).strip()
    if q:
        q = 'text %s' % json.dumps(q, ensure_ascii=False)
        parts.append(q)

    parts.append('unkeyword #link')
    tags = opts.get('tags', [])
    if '#trash' not in tags:
        parts.append('unkeyword #trash')
    if '#spam' not in tags and '#trash' not in tags:
        parts.append('unkeyword #spam')

    if parts:
        q = ' '.join(parts)
    q = q.strip()
    q = q if q else 'all'
    return q, opts


def thread(q, opts, preload=4):
    uids = local.search_msgs('INTHREAD REFS %s' % q, '(DATE)')
    if not uids:
        return {}

    tags = opts.get('tags', [])
    msgs = wrap_msgs(local.msgs_info(uids), tags)

    tags = set(tags)
    for m in msgs.values():
        tags.update(m.pop('tags'))
        m['tags'] = []
    tags = clean_tags(tags)

    same_subject = []
    for num, uid in enumerate(uids[1:], 1):
        prev = uids[num-1]
        subj = msgs[uid]['subject']
        prev_subj = msgs[prev]['subject']
        if subj == prev_subj:
            same_subject.append(uid)

    edit = None
    parents = []
    for i, m in msgs.items():
        if not m['is_draft']:
            continue
        if m['draft_id'] == opts.get('draft'):
            edit = draft_info(m['uid'])
        if not m['parent'] or m['parent'] not in uids:
            continue
        uids.remove(m['uid'])
        uids.insert(uids.index(m['parent']) + 1, m['uid'])
        parents.append(m['parent'])

    if preload is not None and len(uids) > preload * 2:
        msgs_few = {
            i: m for i, m in msgs.items()
            if any((
                m['is_unread'],
                m['is_pinned'],
                m['is_draft'],
                m['uid'] in parents
            ))
        }
        uids_few = [uids[0]] + uids[-preload+1:]
        for i in uids_few:
            if i in msgs_few:
                continue
            msgs_few[i] = msgs[i]
        msgs = msgs_few

    return {
        'uids': uids,
        'msgs': msgs,
        'msgs_info': app.get_url('msgs_info'),
        'thread': True,
        'tags': tags,
        'same_subject': same_subject,
        'edit': edit,
    }


def wrap_tags(tags, whitelist=None):
    def query(tag):
        if tag.startswith('\\'):
            q = {'\\Draft': ':draft', '\\Flagged': ':pinned'}.get(tag)
            if not q:
                q = ':raw %s' % tag[1:]
        else:
            q = 'tag:%s' % tag.lower()
        return ':threads %s' % q

    def trancate(val, max=14, end='…'):
        return val[:max] + end if len(val) > max else val

    def sort(key):
        tag = tags[key]
        first = (
            key not in ('#spam', '#trash') and
            (tag.get('unread', 0) or tag.get('pinned', 0))
        )
        return 0 if first else 1, tags[key]['name']

    ids = sorted(clean_tags(tags, whitelist), key=sort)
    info = {
        t: dict(tags[t], query=query(t), short_name=trancate(tags[t]['name']))
        for t in ids
    }
    return {'ids': ids, 'info': info}


def clean_tags(tags, whitelist=None, blacklist=None):
    whitelist = whitelist or []
    blacklist = '|'.join(re.escape(i) for i in blacklist) if blacklist else ''
    blacklist = blacklist and '|%s' % blacklist
    ignore = re.compile(r'(^\\|#sent|#latest|#link%s)' % blacklist)
    return sorted(i for i in tags if i in whitelist or not ignore.match(i))


def wrap_msgs(items, hide_tags=None):
    def query_header(name, value):
        value = json.dumps(value, ensure_ascii=False)
        return ':threads %s:%s' % (name, value)

    tz = request.session['timezone']
    msgs = {}
    for uid, txt, flags, addrs in items:
        if isinstance(txt, bytes):
            txt = txt.decode()
        if isinstance(txt, str):
            info = json.loads(txt)
        else:
            info = txt

        if addrs is None:
            addrs = [info['from']] if 'from' in info else []
        if info.get('from'):
            info['from'] = wrap_addresses([info['from']])[0]
        base_q = ''
        if '#trash' in hide_tags:
            base_q = 'tag:#trash '
        elif '#spam' in hide_tags:
            base_q = 'tag:#spam '
        info.update({
            'uid': uid,
            'parent': info['parent'] and local.pair_msgid(info['parent']),
            'count': len(addrs),
            'tags': clean_tags(flags, blacklist=hide_tags),
            'from_list': wrap_addresses(addrs, max=3),
            'query_thread': base_q + 'thread:%s' % uid,
            'query_subject': base_q + query_header('subj', info['subject']),
            'query_msgid': base_q + 'ref:%s' % info['msgid'],
            'url_raw': app.get_url('raw', uid=info['origin_uid']),
            'time_human': humanize_dt(info['date'], tz=tz),
            'time_title': format_dt(info['date'], tz=tz),
            'is_unread': '\\Seen' not in flags,
            'is_pinned': '\\Flagged' in flags,
            'is_draft': '\\Draft' in flags,
        })
        if info['is_draft'] and info['parent']:
            info['query_edit'] = base_q + 'draft:%s' % info['draft_id']
            info['url_edit'] = app.get_url('editor', id=info['draft_id'])

        info['files'] = wrap_files(info['files'], info['url_raw'])
        msgs[uid] = info
    return msgs


def wrap_files(files, url):
    return [
        dict(f, url='%s/%s/%s' % (url, f['path'], f['filename']))
        for f in files
    ]


def wrap_addresses(addrs, max=4):
    if isinstance(addrs, str):
        addrs = [addrs]

    addrs_uniq = []
    addrs_list = []
    for a in reversed(addrs):
        if not a or a['addr'] in addrs_uniq:
            continue
        addrs_uniq.append(a['addr'])
        addrs_list.append(dict(a, query=':threads from:%s' % a['addr']))

    addrs_list = list(reversed(addrs_list))
    if len(addrs_list) <= max:
        return addrs_list

    addr_end = addrs[-1]
    if addr_end and addr_end['addr'] != addrs_list[-1]['addr']:
        addrs_list.pop(addrs_list.index(addr_end))
        addrs_list.append(addr_end)

    if addr_end['addr'] == addrs[0]['addr']:
        expander_index = 0
        addrs_few = addrs_list[-max+1:]
    else:
        expander_index = 1
        addrs_few = [addrs_list[0]] + addrs_list[-max+2:]

    addrs_few.insert(
        expander_index,
        {'expander': len(addrs_list) - len(addrs_few)}
    )
    return addrs_few


def localize_dt(val, tz=utc):
    if isinstance(val, (float, int)):
        val = dt.datetime.fromtimestamp(val)
    if not val.tzinfo:
        val = utc.localize(val)
    if isinstance(tz, str):
        tz = timezone(tz)
    if tz != utc:
        val = val.astimezone(tz)
    return val


def format_dt(value, tz=utc, fmt='%a, %d %b, %Y at %H:%M'):
    return localize_dt(value, tz).strftime(fmt)


def humanize_dt(val, tz=utc, secs=False):
    val = localize_dt(val, tz)
    now = localize_dt(dt.datetime.utcnow(), tz)
    if (now - val).total_seconds() < 12 * 60 * 60:
        fmt = '%H:%M' + (':%S' if secs else '')
    elif now.year == val.year:
        fmt = '%b %d'
    else:
        fmt = '%b %d, %Y'
    return val.strftime(fmt)


def fetch_avatars(hashes, size=20, default='identicon', b64=True):
    def _avatar(hash):
        if hash in cache:
            return cache[hash]
        res = http.get(
            '/avatar/{hash}?d={default}&s={size}'
            .format(hash=hash, size=size, default=default)
        )
        result = hash, res.read() if res.status_code == 200 else None
        cache[hash] = result
        return result

    if not hasattr(fetch_avatars, 'cache'):
        fetch_avatars.cache = {}
    key = (size, default)
    fetch_avatars.cache.setdefault(key, {})
    cache = fetch_avatars.cache[key]

    http = HTTPClient.from_url('https://www.gravatar.com/')
    pool = Pool(20)
    res = pool.map(_avatar, hashes)
    return [(i[0], base64.b64encode(i[1]) if b64 else i[1]) for i in res if i]


def draft_info(uid):
    flags, headers, meta, txt = local.draft_info(uid)
    info = {
        i: headers.get(i, '')
        for i in ('from', 'to', 'cc', 'subject', 'in-reply-to', 'references')
    }
    info.update({
        'uid': uid,
        'txt': txt,
        'flags': flags,
        'draft_id': meta['draft_id'],
        'origin_uid': meta['origin_uid'],
        'files': [],
    })
    if meta['files']:
        url = app.get_url('raw', uid=info['origin_uid'])
        info['files'] = wrap_files(meta['files'], url)
    return info
