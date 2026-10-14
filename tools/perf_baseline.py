"""Repeatable synthetic local performance baseline; never opens personal mail."""
import argparse
import json
import math
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailpilot.store import Store
from mailpilot.web import dashboard


def measure(call, repeats):
    for _ in range(3):
        call()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    ordered = sorted(samples)
    return {'p50_ms': round(statistics.median(samples), 3),
            'p95_ms': round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
            'samples': len(samples)}


def seed(store, count):
    result = {'summary_zh': '合同金额12800元，请确认周五会议。', 'key_points': ['周五会议', '报价12800元'],
              'needs_action': True, 'action_summary': '确认会议并核对报价。', 'mode': 'model'}
    rows = []
    for uid in range(1, count + 1):
        account = ('qq1', '1632', 'gmail3')[uid % 3]
        msg = {'subject': f'合同讨论 {uid}', 'from_addrs': ['sender@example.com'],
               'to_addrs': ['recipient@example.com'], 'cc_addrs': [], 'received_account': account + '@example.com',
               'account_id': account, 'account_label': account, 'body': '合成正文，不来自个人邮箱。' * 20,
               'attachments': ['report.pdf'] if uid % 5 == 0 else [], 'date_header': '', 'sensitive': False}
        rows.append((account, uid, str(uid), json.dumps(msg, ensure_ascii=False),
                     json.dumps(dict(result, needs_action=uid % 2 == 0), ensure_ascii=False), time.time()))
    with store.connect() as db:
        db.executemany('''INSERT INTO messages(account,folder,validity,uid,fingerprint,payload,state,result,created)
            VALUES (?,'INBOX','bench',?,?,?,'processed',?,?)''', rows)


def main():
    parser = argparse.ArgumentParser(description='Synthetic local workbench timing baseline')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeats', type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.repeats <= 500:
        parser.error('repeats must be 5..500')
    report = {'version': '0.2', 'timestamp': datetime.now().astimezone().isoformat(),
              'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                              'os': platform.platform(), 'cpu': os.environ.get('PROCESSOR_IDENTIFIER', platform.machine()),
                              'logical_cpus': os.cpu_count()},
              'method': 'Single process, 3 warmups, synthetic rows, 20-row pages. Seeding excluded. No IMAP/model/push/network timing.',
              'datasets': []}
    for count in (1000, 10000):
        with tempfile.TemporaryDirectory(prefix='mailpilot-bench-') as directory:
            store = Store(directory)
            seed(store, count)
            tests = {'all_page': lambda: store.browse(),
                     'account_page': lambda: store.browse(account='qq1'),
                     'action_page': lambda: store.browse(view='action'),
                     'keyword_page': lambda: store.browse(query='12800'),
                     'dashboard_render': lambda: dashboard(store)}
            report['datasets'].append({'messages': count, 'measurements':
                                      {name: measure(call, args.repeats) for name, call in tests.items()}})
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
