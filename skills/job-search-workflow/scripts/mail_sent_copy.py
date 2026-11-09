"""Save an SMTP-accepted application to this account's Sent folder, without resending."""
import hashlib
import imaplib
import json
import re
import sqlite3
import ssl
from contextlib import closing
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from mail_transport import load_secret


def save_sent_copy(root, plan_id):
    if not isinstance(plan_id,str) or not re.fullmatch(r'[a-f0-9]{16}',plan_id):
        raise ValueError('invalid_plan_id')
    root = Path(root)
    folder = root / 'outbox' / plan_id
    receipt = folder / '已发送副本.json'
    state = {'plan_id': plan_id, 'mailbox': None,
             'smtp_resent': False, 'status': 'not_saved'}
    client = None
    append_started = False
    try:
        with closing(sqlite3.connect(root / 'workflow.sqlite3')) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT p.payload,a.state,a.result,a.created FROM delivery_plans p '
                             'JOIN delivery_attempts a ON a.plan=p.id WHERE p.id=?', (plan_id,)).fetchone()
        if row is None or row['state'] != 'smtp_accepted':
            raise ValueError('requires_recorded_smtp_acceptance')
        payload, result = json.loads(row['payload']), json.loads(row['result'])
        spec = payload['specification']
        if payload['channel'] != 'email' or result.get('evidence', {}).get('smtp_code') != 250:
            raise ValueError('invalid_smtp_evidence')
        for key in ('recipient', 'message_id'):
            if result['evidence'].get(key) != spec[key]:
                raise ValueError('smtp_evidence_mismatch')
        raw = (folder / 'application.eml').read_bytes()
        if hashlib.sha256(raw).hexdigest() != payload['draft_sha256']:
            raise ValueError('draft_hash_mismatch')
        original = BytesParser(policy=default).parsebytes(raw)
        if str(original['Message-ID']) != spec['message_id'] or str(original['To']) != spec['recipient']:
            raise ValueError('draft_headers_mismatch')
        account = json.loads((root / 'preferences.json').read_text(encoding='utf-8-sig'))['submission_channels']['email']
        imap = account['imap']
        mailbox = imap.get('sent_mailbox')
        if not isinstance(mailbox,str) or not mailbox or any(ord(c)<32 or c in '"\\' for c in mailbox):
            raise ValueError('configure_ascii_imap_sent_mailbox')
        mailbox.encode('ascii')
        target='"'+mailbox+'"'
        state['mailbox']=mailbox
        if not imap.get('enabled',False) or not account['sender'] or not imap['host'] or not 1 <= int(imap['port']) <= 65535:
            raise ValueError('unexpected_account_or_server')
        state.update(message_id=spec['message_id'], recipient=spec['recipient'])
        client = imaplib.IMAP4_SSL(imap['host'], imap['port'], ssl_context=ssl.create_default_context(), timeout=30)
        secret = load_secret(root / 'private/smtp-auth.dpapi')
        client.login(account['sender'], secret)
        del secret

        def select_and_search():
            status, _ = client.select(target, readonly=True)
            if status != 'OK':
                raise RuntimeError('sent_folder_unavailable')
            # QQ may return no HEADER-search hits even for an existing message.
            # Fetch bounded header batches and compare Message-ID ourselves.
            status, data = client.uid('SEARCH', None, 'ALL')
            if status != 'OK':
                raise RuntimeError('sent_search_failed')
            ids = (data[0] or b'').split()
            if len(ids) > 5000 or any(not uid.isdigit() for uid in ids):
                raise RuntimeError('sent_header_scan_limit_or_invalid_uid')
            hits = []
            for offset in range(0, len(ids), 200):
                uidset = ','.join(uid.decode('ascii') for uid in ids[offset:offset + 200])
                status, rows = client.uid('FETCH', uidset, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
                if status != 'OK':
                    raise RuntimeError('sent_header_scan_failed')
                seen = set()
                for row in rows or []:
                    if not isinstance(row, tuple):
                        continue
                    match = re.search(rb'\bUID (\d+)\b', row[0])
                    if not match:
                        raise RuntimeError('sent_header_uid_missing')
                    seen.add(match[1])
                    message = BytesParser(policy=default).parsebytes(row[1])
                    if str(message['Message-ID']) == spec['message_id']:
                        hits.append(match[1])
                if seen != set(ids[offset:offset + 200]):
                    raise RuntimeError('sent_header_scan_incomplete')
            return hits

        hits = select_and_search()
        existing = bool(hits)
        if not hits:
            append_started = True
            internaldate = imaplib.Time2Internaldate(datetime.fromisoformat(row['created']))
            status, _ = client.append(target, '(\\Seen)', internaldate, raw)
            if status != 'OK':
                raise RuntimeError('sent_append_rejected')
            hits = select_and_search()
        if len(hits) != 1 or not hits[0].isdigit():
            raise RuntimeError('sent_copy_not_unique_or_not_visible')
        status, data = client.uid('FETCH', hits[0].decode('ascii'), '(UID BODY.PEEK[])')
        parts = [x[1] for x in data or [] if isinstance(x, tuple) and len(x) == 2]
        if status != 'OK' or len(parts) != 1:
            raise RuntimeError('sent_copy_fetch_failed')
        saved = BytesParser(policy=default).parsebytes(parts[0])
        for header in ('Message-ID', 'From', 'To', 'Subject'):
            if str(saved[header]) != str(original[header]):
                raise RuntimeError('sent_copy_header_mismatch')
        attachments = list(saved.iter_attachments())
        if len(attachments) != 1 or hashlib.sha256(attachments[0].get_payload(decode=True)).hexdigest() != payload['pdf_sha256']:
            raise RuntimeError('sent_copy_attachment_mismatch')
        saved_body = saved.get_body(preferencelist=('plain',)).get_content().replace('\r\n', '\n').strip()
        if saved_body != spec['body'].replace('\r\n', '\n').strip():
            raise RuntimeError('sent_copy_body_mismatch')
        state.update(status='verified_existing' if existing else 'saved_verified', uid=hits[0].decode('ascii'),
                     uidvalidity=client.response('UIDVALIDITY')[1][0].decode('ascii'),
                     pdf_sha256=payload['pdf_sha256'])
    except Exception as exc:
        # Never print server responses or credential data. Recheck Message-ID on any later invocation.
        state.update(status='copy_unconfirmed' if append_started else 'not_saved', error_type=type(exc).__name__)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
    state['checked_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return state
