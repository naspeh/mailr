import email
import hashlib
import imaplib
import re
import smtplib

from gevent import socket, ssl

from . import fn_time, imap, imap_utf7, local, lock, log, message, schema

SKIP_DRAFTS = True


@local.setting('remote/account')
def data_account(value):
    schema.validate(value, {
        'type': 'object',
        'properties': {
            'username': {'type': 'string'},
            'password': {'type': 'string'},
            'imap_host': {'type': 'string'},
            'imap_port': {'type': 'integer', 'default': 993},
            'smtp_host': {'type': 'string'},
            'smtp_port': {'type': 'integer', 'default': 587},
        },
        'required': ['username', 'password', 'imap_host', 'smtp_host']
    })
    value = value.copy()
    if value['imap_host'] == 'imap.gmail.com':
        value['gmail'] = True
    return value


@local.setting('remote/uidnext', lambda: {})
def data_uidnext(key, value):
    setting = data_uidnext.get()
    setting[key] = value
    return setting


@local.setting('remote/modseq', lambda: {})
def data_modseq(key, value):
    setting = data_modseq.get()
    setting[key] = value
    return setting


def box_key(box=None, tag=None):
    if not box and not tag:
        raise ValueError('"box" or "tag" should be specified')

    account = data_account.get()
    key = account['imap_host'], account['username'], tag or box
    return ':'.join(key)


class Remote(imaplib.IMAP4, imap.Conn):
    def __init__(self):
        account = data_account.get()
        self.username = account['username']
        self.password = account['password']
        self.defaults()
        super().__init__(account['imap_host'], account['imap_port'])

    def _create_socket(self):
        ssl_context = ssl.SSLContext()
        sock = socket.create_connection((self.host, self.port))
        return ssl_context.wrap_socket(sock, server_hostname=self.host)

    def open(self, host='', port=imaplib.IMAP4_SSL_PORT):
        super().open(host, port=imaplib.IMAP4_SSL_PORT)

    def login(self):
        return super().login(self.username, self.password)


def connect():
    con = Remote()
    imap.check(con.login())
    return con


def client(tag=None, box=None, writable=False, readonly=True):
    ctx = imap.client(connect, writable=writable)
    if box:
        ctx.select(box, readonly=readonly)
    elif tag:
        ctx.select_tag(tag, readonly=readonly)
    return ctx


@local.using(local.SRC)
def fetch_imap(uids, box, tag=None, con=None):
    map_tags = {
        '\\Inbox': '#inbox',
        '\\Junk': '#spam',
        '\\Trash': '#trash',
        '\\Sent': '#sent',
    }
    exists = {}
    res = con.fetch('1:*', 'BODY.PEEK[HEADER.FIELDS (X-SHA256)]')
    for i in range(0, len(res), 2):
        uid = res[i][0].decode().split()[2]
        line = res[i][1].strip()
        if not line:
            continue
        hash = email.message_from_bytes(line)['X-SHA256'].strip()
        exists[hash.strip('<>')] = uid

    def msgs(con):
        account = data_account.get()
        res = con.fetch(uids, '(UID INTERNALDATE FLAGS BODY.PEEK[])')
        for i in range(0, len(res), 2):
            line, raw = res[i]
            hash = hashlib.sha256(raw).hexdigest()
            if hash in exists:
                continue
            parts = re.search(
                r'('
                r'UID (?P<uid>\d+)'
                r' ?|'
                r'INTERNALDATE (?P<time>"[^"]+")'
                r' ?|'
                r'FLAGS \((?P<flags>[^)]*)\)'
                r' ?){3}',
                line.decode()
            ).groupdict()

            flags = parts['flags']
            if tag and tag in map_tags:
                flags = ' '.join([flags, map_tags[tag]])

            headers = [
                'X-SHA256: <%s>' % hash,
                'X-Remote-Host: <%s>' % account['imap_host'],
                'X-Remote-Login: <%s>' % account['username'],
            ]

            # line break should be in the end, so an empty string here
            headers.append('')
            headers = '\r\n'.join(headers)

            raw = headers.encode() + raw
            yield parts['time'], flags, raw

    with client(box=box, tag=tag) as c:
        msgs = list(msgs(c))
    if not msgs:
        return None

    return con.multiappend(local.SRC, msgs)


def uids_by_msgid_gmail(con):
    uids = {}
    res = con.fetch('1:*', 'BODY.PEEK[HEADER.FIELDS (X-GM-MSGID)]')
    for i in range(0, len(res), 2):
        uid = res[i][0].decode().split()[2]
        line = res[i][1].strip()
        if not line:
            continue
        gid = email.message_from_bytes(line)['X-GM-MSGID'].strip()
        uids[gid.strip('<>')] = uid
    return uids


def flags_by_gmail(tag, flags, labels):
    flags = flags or ''
    labels = labels or ''
    map_flags = {
        '\\Answered': '\\Answered',
        '\\Flagged': '\\Flagged',
        '\\Deleted': '\\Deleted',
        '\\Seen': '\\Seen',
        '\\Draft': '\\Draft',
    }
    map_labels = {
        '\\Drafts': '\\Draft',
        '\\Draft': '\\Draft',
        '\\Starred': '\\Flagged',
        '\\Inbox': '#inbox',
        '\\Junk': '#spam',
        '\\Trash': '#trash',
        '\\Sent': '#sent',
        '\\Chats': '#chats',
        '\\Important': '',
    }

    def flag(m):
        flag = m.group()
        if flag:
            return map_flags.get(flag, '')
        return ''

    def label(m):
        label = m.group()
        if label:
            label = label.strip('"').replace('\\\\', '\\')
            label = imap_utf7.decode(label)
            flag = map_labels.get(label, None)
            if flag is None:
                flag = local.get_tag(label)['id']
            return flag
        return ''

    flags = re.sub(r'([^ ])*', flag, flags)
    flags = ' '.join([
        flags,
        re.sub(r'("[^"]*"|[^" ]*)', label, labels),
        map_labels.get(tag, ''),
    ]).strip()
    return flags


@local.using(local.SRC)
def fetch_gmail(uids, box, tag, con=None):

    existing = uids_by_msgid_gmail(con)
    new_uids = []
    with client(tag, box=box) as gm:
        res = gm.fetch(uids.str, 'X-GM-MSGID')
        for line in res:
            parts = re.search(
                r'('
                r'UID (?P<uid>\d+)'
                r' ?|'
                r'X-GM-MSGID (?P<msgid>\d+)'
                r' ?){2}',
                line.decode()
            ).groupdict()
            if parts['msgid'] in existing:
                continue
            new_uids.append(parts['uid'])
        if not new_uids:
            log.debug('%s are alredy imported' % uids)
            return
        fields = (
            '('
            'INTERNALDATE FLAGS BODY.PEEK[] '
            'X-GM-LABELS X-GM-MSGID X-GM-THRID'
            ')'
        )
        res = gm.fetch(new_uids, fields)
        login = gm.username

    def msgs():
        for i in range(0, len(res), 2):
            line, raw = res[i]
            parts = re.search(
                r'('
                r'UID (?P<uid>\d+)'
                r' ?|'
                r'INTERNALDATE (?P<time>"[^"]+")'
                r' ?|'
                r'FLAGS \((?P<flags>[^)]*)\)'
                r' ?|'
                r'X-GM-LABELS \((?P<labels>.*)\)'
                r' ?|'
                r'X-GM-MSGID (?P<msgid>\d+)'
                r' ?|'
                r'X-GM-THRID (?P<thrid>\d+)'
                r' ?){6}',
                line.decode()
            ).groupdict()
            if not raw or parts['msgid'] in existing:
                # this happens in "[Gmail]/Chats" folder
                continue
            flags = flags_by_gmail(tag, parts['flags'], parts['labels'])
            if SKIP_DRAFTS and '\\Draft' in flags:
                # TODO: skip drafts for now
                continue

            headers = [
                'X-SHA256: <%s>' % hashlib.sha256(raw).hexdigest(),
                'X-GM-UID: <%s>' % parts['uid'],
                'X-GM-MSGID: <%s>' % parts['msgid'],
                'X-GM-THRID: <%s>' % parts['thrid'],
                'X-GM-Login: <%s>' % login,
            ]
            thrid_re = r'(^| )mlr/thrid/\d+'
            thrid = re.search(thrid_re, flags)
            if thrid:
                flags = re.sub(thrid_re, '', flags)
                thrid = thrid.group().strip()
                headers.append('X-Thread-ID: <%s@mailur.link>' % thrid)

            # line break should be in the end, so an empty string here
            headers.append('')
            headers = '\r\n'.join(headers)

            raw = headers.encode() + raw
            yield parts['time'], flags, raw

    msgs = list(msgs())
    if not msgs:
        return None

    return con.multiappend(local.SRC, msgs)


@fn_time
@lock.user_scope('remote-fetch')
def fetch_folder(box=None, tag=None, **opts):
    account = data_account.get()
    uidnext_key = box_key(box, tag)
    uidvalidity, uidnext = data_uidnext.key(uidnext_key, (None, None))
    log.info('saved: uidvalidity=%s uidnext=%s', uidvalidity, uidnext)
    con = client(tag=tag, box=box)
    folder = {'uidnext': con.uidnext, 'uidval': con.uidvalidity}
    log.info('remote: uidvalidity=%(uidval)s uidnext=%(uidnext)s', folder)
    if folder['uidval'] != uidvalidity:
        uidvalidity = folder['uidval']
        uidnext = 1
    uids = con.search('UID %s:*' % uidnext)
    uids = [i for i in uids if int(i) >= uidnext]
    uidnext = folder['uidnext']
    log.info('box(%s): %s new uids', con.box, len(uids))
    con.logout()
    if len(uids):
        uids = imap.Uids(uids, **opts)
        fetch_uids = fetch_gmail if account.get('gmail') else fetch_imap
        uids.call_async(fetch_uids, uids, box, tag)

    data_uidnext(uidnext_key, (uidvalidity, uidnext))


def fetch(**kw):
    if kw.get('tag') or kw.get('box'):
        fetch_folder(**kw)
        return

    for params in get_folders():
        fetch_folder(**dict(kw, **params))


def get_folders():
    account = data_account.get()
    if not account:
        log.info('no remote account')
        return []

    if account.get('gmail'):
        return [{'tag': '\\All'}, {'tag': '\\Junk'}, {'tag': '\\Trash'}]
    else:
        with client(None) as c:
            if c.select_tag('\\All', exc=False):
                items = [{'tag': '\\All'}]
            else:
                items = [{'box': 'INBOX', 'tag': '\\Inbox'}]
                if c.select_tag('\\Sent', exc=False):
                    items.append({'tag': '\\Sent'})
        return items


@local.using(local.SRC, reuse=False)
def sync_gmail(con=None):
    uids_by_msgid = uids_by_msgid_gmail(con)
    msgids_by_uid = {v: k for k, v in uids_by_msgid.items()}
    flags_by_uid_remote = {}
    flags_by_uid_local = {}
    modseqs = {}

    label_by_flag = {
        '#trash': '\\Trash',
        '#spam': '\\Junk',
        '#inbox': '\\Inbox',
        '\\Flagged': '\\Starred',
    }
    folders = {'\\Trash', '\\Junk'}

    def find_uid_remote(gm, msgid):
        uid = None
        for params in get_folders():
            tag = params['tag']
            gm.select_tag(tag=tag)
            res = gm.search('X-GM-MSGID %s' % msgid)
            if res:
                uid = res[0]
                break
        return uid, tag

    def sync_msg_to_remote(gm, msgid, flags):
        uid, tag = find_uid_remote(gm, msgid)
        if not uid:
            return

        gm.select(gm.box, readonly=False)
        labels = {label_by_flag[f] for f in flags if f in label_by_flag}
        labels_removed = set(label_by_flag.values()) - labels
        if labels_removed.intersection(folders) and tag in folders:
            # move to \\All first
            gm.store([uid], '+X-GM-LABELS', '\\Inbox')
            uid, tag = find_uid_remote(gm, msgid)

        if labels:
            gm.store([uid], '+X-GM-LABELS', ' '.join(labels))
        if labels_removed:
            gm.store([uid], '-X-GM-LABELS', ' '.join(labels_removed))

        mark = '+' if '\\Seen' in flags else '-'
        gm.store([uid], mark + 'FLAGS.SILENT', '\\Seen')

    def get_remote_flags_for_sync(box=None, tag=None):
        modseq_key = box_key(box, tag)
        modseq_gmail = data_modseq.key(modseq_key, None)
        with client(tag, box=box) as gm:
            modseqs[modseq_key] = gm.highestmodseq
            if not modseq_gmail:
                return

            fields = (
                '(UID X-GM-MSGID X-GM-LABELS FLAGS) (CHANGEDSINCE %s)'
                % modseq_gmail
            )
            res = gm.fetch('1:*', fields)
            for line in res:
                parts = re.search(
                    r'('
                    r'UID (?P<uid>\d+)'
                    r' ?|'
                    r'FLAGS \((?P<flags>[^)]*)\)'
                    r' ?|'
                    r'X-GM-LABELS \((?P<labels>.*)\)'
                    r' ?|'
                    r'X-GM-MSGID (?P<msgid>\d+)'
                    r' ?|'
                    r'MODSEQ \(\d+\)'
                    r' ?){5}',
                    line.decode()
                ).groupdict()
                flags = flags_by_gmail(tag, parts['flags'], parts['labels'])
                uid = uids_by_msgid.get(parts['msgid'])
                if not uid:
                    # probably draft
                    continue
                flags_by_uid_remote[uid] = set(flags.split())

    def get_local_flags_for_sync():
        modseq_key = box_key(tag='\\Local')
        modseq_local = data_modseq.key(modseq_key, None)
        modseqs[modseq_key] = con.highestmodseq
        if not modseq_local:
            return

        res = con.fetch('1:*', '(UID FLAGS) (CHANGEDSINCE %s)' % modseq_local)
        for line in res:
            val = re.search(
                r'UID (\d+) FLAGS \(([^)]*)\) MODSEQ \(\d+\)',
                line.decode()
            )
            if not val:
                continue
            uid, flags = val.groups()
            flags_by_uid_local[uid] = set(flags.split())

    def sync_flags(flags_by_uid_local, flags_by_uid_remote):
        if not flags_by_uid_local and not flags_by_uid_remote:
            return

        both_changes = set(flags_by_uid_local) & set(flags_by_uid_remote)
        local_changes = set(flags_by_uid_local) - set(flags_by_uid_remote)
        remote_changes = set(flags_by_uid_remote) - set(flags_by_uid_local)

        uids_to_remote = both_changes | local_changes
        if uids_to_remote:
            log.info('Sync flags to gmail: %s', uids_to_remote)
            with client(writable=True) as gm:
                for uid in uids_to_remote:
                    sync_msg_to_remote(
                        gm, msgids_by_uid[uid], flags_by_uid_local[uid]
                    )

        if remote_changes:
            flags_synced = {'#trash', '#spam', '#inbox', '\\Flagged', '\\Seen'}
            log.info('Sync flags from gmail: %s', remote_changes)
            for uid in remote_changes:
                parsed_uids = local.pair_origin_uids([uid])
                flags = flags_by_uid_remote[uid]
                if flags:
                    local.msgs_flag(parsed_uids, [], flags)
                flags_removed = flags_synced - flags
                if flags_removed:
                    local.msgs_flag(parsed_uids, flags_removed, [])

    for params in get_folders():
        get_remote_flags_for_sync(**params)

    get_local_flags_for_sync()

    sync_flags(flags_by_uid_local, flags_by_uid_remote)

    log.info('modseqs: %s', modseqs)
    for key, value in modseqs.items():
        data_modseq(key, value)


def sync(only_flags=False):
    if not only_flags:
        try:
            fetch()
            local.parse()
        except lock.Error as e:
            log.warn(e)

    account = data_account.get()
    if account.get('gmail'):
        return sync_gmail()


def send(msg):
    params = message.sending(msg)

    account = data_account.get()
    con = smtplib.SMTP(account['smtp_host'], account['smtp_port'])
    con.ehlo()
    con.starttls()
    con.login(account['username'], account['password'])
    con.sendmail(*params)

    fetch()
    local.parse()