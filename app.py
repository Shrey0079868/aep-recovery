from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, uuid, sys, urllib.parse
ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / 'uploads'
UPLOADS.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from recovery_engine import recover, parse_salvaged, extract_full_project_preview, project_summary

def multipart(body: bytes, ctype: str):
    if 'boundary=' not in ctype:
        return []
    boundary = ctype.split('boundary=', 1)[1].strip().strip('"').encode()
    out = []
    for part in body.split(b'--' + boundary):
        if b'\r\n\r\n' not in part or b'filename="' not in part:
            continue
        head, data = part.split(b'\r\n\r\n', 1)
        name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode('utf-8', 'replace')
        data = data.rstrip(b'\r\n-')
        p = UPLOADS / (uuid.uuid4().hex + '_' + Path(name).name)
        p.write_bytes(data)
        out.append(p)
    return out

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, data, ctype='application/json', code=200, headers=None):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ('/', '/index.html'):
            self.send((ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            return
        if path.startswith('/download/'):
            fname = Path(urllib.parse.unquote(path[len('/download/'):])).name
            p = UPLOADS / fname
            if p.exists():
                self.send(
                    p.read_bytes(),
                    'application/octet-stream',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'}
                )
                return
        self.send('Not found', 'text/plain', 404)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(n)
            ctype = self.headers.get('Content-Type', '')
            files = multipart(body, ctype)
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == '/api/recover':
                if not files:
                    raise ValueError('Please upload the corrupted .aep file.')
                damaged = files[0]
                autosave = files[1] if len(files) > 1 else None
                out_filename = damaged.stem + '_RECOVERED.aep'
                out = UPLOADS / out_filename
                report = recover(str(damaged), str(autosave) if autosave else None, str(out))
                report['download'] = '/download/' + out.name
                self.send(json.dumps(report, ensure_ascii=False), 'application/json')
                return

            if path in ('/api/preview', '/api/parse'):
                if not files:
                    raise ValueError('No AEP file supplied for preview.')
                target_file = files[0]
                app = parse_salvaged(str(target_file))
                preview_data = extract_full_project_preview(app, str(target_file))
                self.send(json.dumps(preview_data, ensure_ascii=False), 'application/json')
                return

            raise ValueError(f'Unknown endpoint: {path}')
        except Exception as e:
            traceback.print_exc()
            self.send(json.dumps({'error': str(e), 'type': type(e).__name__}), 'application/json', 400)

if __name__ == '__main__':
    port = 8765
    print(f'AEP Recovery Lab & Project Previewer running on http://127.0.0.1:{port}')
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
