from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

from oxide.journal_backend import start_journal


def test_importing_backend_does_not_load_python_prototype() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            ("import sys; import oxide.journal_backend; assert 'oxide.journal' not in sys.modules"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_external_backend_is_selected_through_fixed_port(tmp_path: Path) -> None:
    kernel = tmp_path / "kernel.py"
    kernel.write_text(
        """\
import json, os, socketserver, threading, time
records = []
lock = threading.Lock()
max_results = int(os.environ['OXIDE_JOURNAL_MAX_RESULTS'])
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        args = request['arguments']
        with lock:
            if request['operation'] == 'journal_add':
                sequence = len(records) + 1
                record = {'record_id': sequence, 'stable_id': f'record:{sequence}', 'journal_sequence': sequence, 'namespace': args['namespace'], 'author': args['author'], 'text': args['text'], 'created_at': time.time(), 'match_kind': 'exact'}
                records.append(record)
                result = {'saved': True, 'record_id': record['record_id']}
            elif request['operation'] == 'journal_search':
                matches = [record for record in records if record['namespace'] == args['namespace'] and args['query'] in record['text']]
                result = matches[-max_results:]
            else:
                raise RuntimeError('unknown operation')
        response = {'request_id': request['request_id'], 'ok': True, 'result': result}
        self.wfile.write((json.dumps(response) + '\\n').encode())
class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
server = Server(os.environ['OXIDE_JOURNAL_SOCKET'], Handler)
server.serve_forever()
""",
        encoding="utf-8",
    )
    socket = Path("/tmp") / f"oxide-external-{secrets.token_hex(8)}.sock"
    runtime = start_journal(
        tmp_path / "external.sqlite3",
        socket,
        [sys.executable, str(kernel)],
    )
    try:
        added = runtime.client.add("product", "worker", "implementation evidence")
        assert added["saved"] is True
        assert runtime.client.search("product", "evidence")[0]["author"] == "worker"
    finally:
        runtime.close()
    assert not socket.exists()
