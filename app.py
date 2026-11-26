"""归位: local document search with explicit, confirmed trash actions."""
import argparse
import hashlib
import json
import logging
import os
import io
import re
import secrets
import subprocess
import threading
import time
import unicodedata
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET

from pypdf import PdfReader

BASE = Path(__file__).parent
logging.getLogger('pypdf').setLevel(logging.ERROR)
MAX_BYTES = 30 * 1024 * 1024
MAX_FILES = 20000
MAX_TEXT_FILES = 200
CATEGORIES = {"document": "文档", "screenshot": "截图", "image": "图片", "spreadsheet": "表格", "presentation": "演示稿", "archive": "压缩包", "video": "视频", "audio": "音频", "other": "待确认"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".heif", ".avif"}

def category_for(path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        if re.search(r"screenshot|screen shot|截屏|截图|屏幕快照", path.stem, re.I):
            return "screenshot", "依据文件名识别为截图，可手动纠正"
        return "image", "依据图片格式分类，尚未识别画面内容"
    for category, extensions in {"document": {".pdf", ".docx", ".doc", ".pages", ".txt", ".md", ".rtf", ".odt"}, "spreadsheet": {".xlsx", ".xls", ".csv", ".tsv", ".numbers"}, "presentation": {".pptx", ".ppt", ".key"}, "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".dmg"}, "video": {".mp4", ".mov", ".m4v", ".avi", ".mkv"}, "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac"}}.items():
        if ext in extensions: return category, "依据文件格式分类"
    return "other", "暂不能判断文件类型，请手动归类"
SUPPORTED = {'.pdf', '.docx', '.txt', '.md'}
VISIBLE = SUPPORTED | {'.pages', '.doc'}


OCR_LOCK = threading.Lock()

def recognize_image(path):
    """Compile the local Vision helper when needed; never send image data out."""
    with OCR_LOCK:
        source = BASE / 'native' / 'ocr.m'
        binary = BASE / '.local' / 'vision-ocr'
        binary.parent.mkdir(exist_ok=True)
        if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
            try:
                command = ['xcrun', 'clang', '-fobjc-arc', '-framework', 'Foundation', '-framework', 'Vision', str(source), '-o', str(binary)]
                try:
                    subprocess.run(command, check=True, capture_output=True, timeout=120)
                except subprocess.CalledProcessError:
                    # Some Macs retain a newer default SDK than their linker supports.
                    fallback = Path('/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk')
                    if not fallback.is_dir():
                        raise
                    subprocess.run(command + ['-isysroot', str(fallback)], check=True, capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                raise ValueError('本机 OCR 未就绪，需要 macOS 和 Xcode Command Line Tools')
        try:
            result = subprocess.run([str(binary), str(path)], check=True, capture_output=True, timeout=45)
            return json.loads(result.stdout)['text']
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            raise ValueError('图片文字识别失败或超时，可打开图片后重试')


SCREENSHOT_TOPICS = {'work': '工作学习', 'career': '求职申请', 'travel': '旅行生活', 'other': '其他'}

def screenshot_topic(item):
    text = normalize(item['name'] + ' ' + ' '.join(c['text'] for c in item['chunks']))
    clues = {
        'career': ['简历', '招聘', '面试', '求职', '岗位', '录取', '申请学校', '招生', 'resume', 'interview', 'admission'],
        'travel': ['旅行', '旅游', '酒店', '机票', '签证', '航班', '餐厅', '穿搭', '网球', 'hotel', 'flight', 'visa'],
        'work': ['会议', '项目', '报告', '课程', '学习', '作业', '论文', '工作', '产品', '代码', 'meeting', 'project', 'course', 'python'],
    }
    scores = {key: sum(text.count(word) for word in words) for key, words in clues.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else 'other'


def file_identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def move_to_trash(path):
    # The sole original-file mutation in the app. Never permanently unlink.
    from send2trash import send2trash
    send2trash(str(path))


def normalize(text):
    return unicodedata.normalize('NFKC', text).lower()


def query_terms(query):
    text = normalize(query)
    text = re.sub(r'请帮我|帮我|我记得|找出|找到|找一下|那一份|那份|哪个|讲述最全面的|最全面|经历|简历|文件|关于|包含|提到|写过|的|了|找', ' ', text)
    terms = set(re.findall(r'[a-z0-9]+(?:[.%_-][a-z0-9]+)*', text))
    for word in re.findall(r'[\u4e00-\u9fff]+', text):
        if len(word) < 2:
            continue
        terms.add(word)
        terms.update(word[i:i + 2] for i in range(len(word) - 1))
    return terms


def extract(path):
    if path.suffix.lower() == '.pdf':
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF 已加密')
        if len(reader.pages) > 250:
            raise ValueError('超过当前 250 页读取上限')
        return [{'location': f'第 {i + 1} 页', 'text': p.extract_text() or ''} for i, p in enumerate(reader.pages)]
    if path.suffix.lower() == '.docx':
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > MAX_BYTES:
                raise ValueError('正文过大')
            tree = ET.fromstring(archive.read(info))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        for parent in tree.iter():
            for child in list(parent):
                if child.tag in {f'{{{ns["w"]}}}del', f'{{{ns["w"]}}}moveFrom'}:
                    parent.remove(child)
        paragraphs = [''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in tree.findall('.//w:p', ns)]
        return [{'location': f'正文段落 {i + 1}–{min(i + 6, len(paragraphs))}', 'text': '\n'.join(paragraphs[i:i + 6])} for i in range(0, len(paragraphs), 6)]
    return [{'location': '正文', 'text': path.read_text(encoding='utf-8')}]


def snippet(text, terms):
    lower = normalize(text)
    positions = [lower.find(t) for t in terms if t in lower]
    start = max(0, min(positions, default=0) - 70)
    return ('…' if start else '') + text[start:start + 650] + ('…' if len(text) > start + 650 else '')


class Library:
    def __init__(self):
        self.files = {}
        self.roots = []
        self.busy = False
        self.processed = 0
        self.error = ''
        self.truncated = False
        self.lock = threading.Lock()
        self.trash_requests = {}
        self.overrides = {}
        self.topic_overrides = {}
        self.phase = "idle"
        self.skipped = 0
        self.scan_warnings = []
        self.ocr_done = 0
        self.ocr_total = 0

    def checked_file(self, doc_id):
        item = self.files.get(doc_id)
        if not item:
            raise ValueError('文件已不在当前目录中，请重新查找')
        path = Path(item['path'])
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError('文件已移动或路径发生变化，请重新读取目录')
        if not any(path.is_relative_to(Path(root)) for root in self.roots):
            raise ValueError('文件不在当前读取范围内')
        if file_identity(path) != item.get('identity'):
            raise ValueError('文件已被修改或替换，请重新读取目录后再确认')
        return item, path

    def prepare_trash(self, doc_id):
        with self.lock:
            if self.busy:
                raise ValueError('正在读取目录，请完成后再操作')
            item, path = self.checked_file(doc_id)
            # Only the latest user-opened dialog can authorize an action.
            self.trash_requests.clear()
            ticket = secrets.token_urlsafe(32)
            self.trash_requests[ticket] = (doc_id, list(item['identity']), time.monotonic() + 120)
            return {'ticket': ticket, 'name': item['name'], 'path': str(path)}

    def cancel_trash(self, ticket):
        with self.lock:
            self.trash_requests.pop(ticket, None)

    def confirm_trash(self, ticket, confirmed=False):
        with self.lock:
            request = self.trash_requests.pop(ticket, None)
            if confirmed is not True or not request or request[2] < time.monotonic():
                raise ValueError('确认已失效，请重新点击“删除本地原文件”')
            if self.busy:
                raise ValueError('正在读取目录，请完成后再操作')
            doc_id, identity, _ = request
            item, path = self.checked_file(doc_id)
            if file_identity(path) != identity:
                raise ValueError('弹窗打开后文件发生变化，请重新确认')
            move_to_trash(path)
            self.files.pop(doc_id, None)
            return {'ok': True, 'id': doc_id, 'name': item['name']}

    def start(self, paths):
        roots = []
        for value in paths:
            root = Path(value).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f'文件夹不存在：{root}')
            roots.append(root)
        if not roots:
            raise ValueError('请添加至少一个文件夹')
        with self.lock:
            if self.busy:
                raise ValueError('正在读取，请等待完成')
            self.busy = True
            self.trash_requests.clear()
            self.error = ''
            self.processed = 0
        threading.Thread(target=self.scan, args=(roots,), daemon=True).start()

    def scan(self, roots):
        roots = [root.resolve() for root in roots]
        fresh, seen = {}, set()
        previous = self.files
        self.ocr_done = self.ocr_total = 0
        truncated = False
        self.skipped, self.scan_warnings, self.phase = 0, [], 'catalog'
        try:
            for root in roots:
                def onerror(error):
                    self.scan_warnings.append(str(error)[:200])
                for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
                    kept = []
                    for name in dirs:
                        child = Path(directory) / name
                        if name.startswith('.') or name in {'node_modules', '__pycache__', 'vendor'} or name.endswith(('.app', '.framework', '.bundle')) or child.is_symlink() or child.resolve() == BASE.resolve():
                            self.skipped += 1
                        else:
                            kept.append(name)
                    dirs[:] = kept
                    for name in names:
                        path = Path(directory) / name
                        if name.startswith(('.', '~$')) or path.is_symlink():
                            self.skipped += 1
                            continue
                        resolved = path.resolve()
                        if not resolved.is_relative_to(root) or resolved in seen:
                            continue
                        seen.add(resolved)
                        if len(fresh) >= MAX_FILES:
                            truncated = True
                            break
                        category, basis = category_for(path)
                        doc_id = hashlib.sha256(str(resolved).encode()).hexdigest()[:24]
                        if doc_id in self.overrides:
                            category, basis = self.overrides[doc_id], '你手动指定的分类'
                        item = {'id': doc_id, 'name': path.name, 'path': str(resolved), 'format': path.suffix[1:].upper() or '无扩展名', 'category': category, 'basis': basis, 'image': path.suffix.lower() in IMAGE_EXT, 'status': 'metadata', 'reason': '已收录名称与位置；此格式暂不读取正文', 'chunks': []}
                        try:
                            stat = resolved.stat()
                            item.update(size=stat.st_size, modified=stat.st_mtime, signature=[stat.st_size, stat.st_mtime_ns], identity=file_identity(resolved))
                            if item['image']:
                                item.update(status='image_pending', reason='图中文字等待本机识别；识别完成后会自动加入全部文件搜索')
                                old = previous.get(doc_id, {})
                                if old.get('identity') == item['identity'] and old.get('status') in {'ready', 'no_text'}:
                                    item.update(status=old['status'], chunks=old['chunks'], reason=old['reason'])
                            elif path.suffix.lower() in SUPPORTED:
                                item.update(status='deferred', reason='已分类；正文暂未读取，可点击查看原文按需读取')
                            elif path.suffix.lower() in {'.pages', '.doc'}:
                                item.update(status='unread', reason='已分类；暂不支持正文读取，可导出 PDF 或 DOCX')
                        except OSError as exc:
                            item.update(status='unread', reason=str(exc)[:180])
                        fresh[doc_id] = item
                        self.processed = len(fresh)
                    if truncated:
                        break
                if truncated:
                    break
            with self.lock:
                self.files, self.roots, self.truncated = fresh, [str(r) for r in roots], truncated
            self.phase = 'text'
            candidates = [f for f in fresh.values() if f['status'] == 'deferred']
            candidates.sort(key=lambda f: (not bool(re.search('简历|resume', f['name'], re.I)), f.get('size', 0)))
            for item in candidates[:MAX_TEXT_FILES]:
                self.read_content(item)
            self.phase = 'ocr'
            screenshots = [f for f in fresh.values() if f['image'] and f['status'] == 'image_pending']
            screenshots.sort(key=lambda f: (f['category'] != 'screenshot', -f.get('modified', 0)))
            self.ocr_total = len(screenshots)
            for item in screenshots:
                self.read_content(item)
                self.ocr_done += 1
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.busy, self.phase = False, 'idle'

    def read_content(self, item):
        try:
            path = Path(item['path'])
            if path.suffix.lower() not in SUPPORTED and not item.get('image'):
                return
            if file_identity(path) != item.get('identity') or path.is_symlink() or path.resolve() != path:
                raise ValueError('文件已变化，请重新读取目录')
            if item.get('size', 0) > MAX_BYTES:
                raise ValueError('超过正文读取 30 MB 上限；文件仍在分类目录中')
            chunks = [{'location': '图片文字 · 本机 OCR', 'text': recognize_image(path)}] if item.get('image') else extract(path)
            if file_identity(path) != item.get('identity') or path.is_symlink() or path.resolve() != path:
                raise ValueError('识别期间文件已变化，请重新读取目录')
            if item.get('image') and not chunks[0]['text'].strip():
                item.update(chunks=[], status='no_text', reason='未识别出文字；可对照原图确认，模糊小字可能遗漏')
                return
            if not any(c['text'].strip() for c in chunks):
                raise ValueError('未提取到文字；扫描件 OCR 尚未接入')
            item.update(chunks=chunks, status='ready', reason='')
        except Exception as exc:
            item.update(status='unread', reason=str(exc)[:180], chunks=[])

    def classify(self, doc_id, category):
        if category not in CATEGORIES:
            raise ValueError('分类无效')
        with self.lock:
            if self.busy:
                raise ValueError('正在扫描，请稍后手动调整分类')
            if doc_id not in self.files:
                raise ValueError('文件不在当前目录中')
            self.files[doc_id].update(category=category, basis='你手动指定的分类')
            self.overrides[doc_id] = category

    def public(self, item):
        row = {k: v for k, v in item.items() if k not in {'chunks', 'signature', 'identity'}}
        if item['category'] == 'screenshot':
            row['topic'] = self.topic_overrides.get(item['id']) or screenshot_topic(item)
            row['topic_manual'] = item['id'] in self.topic_overrides
        return row

    def search(self, query):
        terms = query_terms(query)
        exact = normalize(query.strip())
        results = []
        for item in list(self.files.values()):
            hits = []
            for chunk in item['chunks']:
                text = normalize(chunk['text'])
                matched = [t for t in terms if t in text]
                score = sum(len(t) for t in matched)
                if exact and exact in text:
                    score += 20
                if score:
                    hits.append({'location': chunk['location'], 'excerpt': snippet(chunk['text'], terms or {exact}), 'score': score, 'matched': matched})
            hits.sort(key=lambda h: h['score'], reverse=True)
            name_match = bool(exact and exact in normalize(item['name']))
            if not query.strip() or hits or name_match:
                results.append({**self.public(item), 'hits': hits[:3], 'score': (hits[0]['score'] if hits else 0) + (10 if name_match else 0), 'name_match': name_match})
        return sorted(results, key=lambda r: (-r['score'], r['name']))


def create_server(port=8765):
    library, token = Library(), secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, content, mime='application/json; charset=utf-8'):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def authorized(self):
            return secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in {'/', '/app.js', '/style.css'}:
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[url.path]
                mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                return self.send(200, (BASE / 'static' / name).read_bytes(), mime)
            if not self.authorized():
                return self.send(401, {'error': '请从启动器给出的完整链接打开'})
            params = parse_qs(url.query)
            if url.path == '/api/status':
                values = list(library.files.values())
                return self.send(200, {'ocr_done': library.ocr_done, 'ocr_total': library.ocr_total, 'ocr_ready': sum(f['image'] and f['status'] == 'ready' for f in values), 'ocr_pending': sum(f['status'] == 'image_pending' for f in values), 'busy': library.busy, 'processed': library.processed, 'total': len(values), 'ready': sum(f['status'] == 'ready' for f in values), 'roots': library.roots, 'error': library.error, 'truncated': library.truncated, 'phase': library.phase, 'skipped': library.skipped, 'warnings': library.scan_warnings, 'deferred': sum(f['status'] == 'deferred' for f in values), 'unread': sum(f['status'] == 'unread' for f in values), 'categories': {k: sum(f['category'] == k for f in values) for k in CATEGORIES}, 'desktop': str(Path.home() / 'Desktop')})
            if url.path == '/api/search':
                return self.send(200, library.search(params.get('q', [''])[0][:1000]))
            if url.path == '/api/document':
                item = library.files.get(params.get('id', [''])[0])
                if not item:
                    return self.send(404, {'error': '文件不在当前目录中'})
                if item['status'] == 'deferred' or (item.get('image') and item['status'] in {'image_pending', 'unread'}):
                    library.read_content(item)
                return self.send(200, {**library.public(item), 'chunks': item['chunks']})
            if url.path == '/api/thumbnail':
                item = library.files.get(params.get('id', [''])[0])
                if not item or not item['image']:
                    return self.send(404, {'error': '不支持图片预览'})
                try:
                    _, path = library.checked_file(item['id'])
                    if item.get('size', 0) > MAX_BYTES:
                        raise ValueError('图片超过预览大小限制')
                    from PIL import Image, ImageOps
                    with Image.open(path) as source:
                        source.thumbnail((640, 480))
                        preview = ImageOps.exif_transpose(source).convert('RGB')
                        out = io.BytesIO()
                        preview.save(out, format='JPEG', quality=80)
                    return self.send(200, out.getvalue(), 'image/jpeg')
                except Exception:
                    return self.send(422, {'error': '暂不能预览此图片，原文件保留在目录中'})
            if url.path == '/api/file':
                item = library.files.get(params.get('id', [''])[0])
                if not item or item['format'] != 'PDF' or item['status'] != 'ready':
                    return self.send(404, {'error': '此文件暂不支持直接预览'})
                p = Path(item['path'])
                try:
                    stat = p.stat()
                    if p.is_symlink() or [stat.st_size, stat.st_mtime_ns] != item['signature']:
                        return self.send(409, {'error': '文件已变化，请重新读取目录'})
                    return self.send(200, p.read_bytes(), 'application/pdf')
                except OSError:
                    return self.send(404, {'error': '文件已移动或不可读，请重新读取目录'})
            return self.send(404, {'error': '不存在'})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {'error': '未授权'})
            if self.path not in {'/api/index', '/api/trash/prepare', '/api/trash/confirm', '/api/trash/cancel', '/api/classify', '/api/screenshot-topic'}:
                return self.send(404, {'error': '不存在'})
            expected_origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.headers.get('Origin') != expected_origin:
                return self.send(403, {'error': '请从本机应用页面手动操作'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16000:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效')
                if self.path == '/api/screenshot-topic':
                    doc_id, topic = body.get('id'), body.get('topic')
                    if not isinstance(doc_id, str) or not isinstance(topic, str) or topic not in SCREENSHOT_TOPICS:
                        raise ValueError('截图主题无效')
                    with library.lock:
                        item, _ = library.checked_file(doc_id)
                        if item['category'] != 'screenshot':
                            raise ValueError('主题分组仅用于截图')
                        library.topic_overrides[doc_id] = topic
                    return self.send(200, {'ok': True})
                if self.path == '/api/classify':
                    if not isinstance(body.get('id'), str) or not isinstance(body.get('category'), str):
                        raise ValueError('分类参数无效')
                    library.classify(body['id'], body['category'])
                    return self.send(200, {'ok': True})
                if self.path.startswith('/api/trash/'):
                    if self.path.endswith('/prepare'):
                        doc_id = body.get('id')
                        if not isinstance(doc_id, str):
                            raise ValueError('文件标识无效')
                        return self.send(200, library.prepare_trash(doc_id))
                    ticket = body.get('ticket')
                    if not isinstance(ticket, str):
                        raise ValueError('缺少手动确认凭据')
                    if self.path.endswith('/cancel'):
                        library.cancel_trash(ticket)
                        return self.send(200, {'ok': True})
                    return self.send(200, library.confirm_trash(ticket, body.get('confirmed')))
                paths = body.get('paths')
                if not isinstance(paths, list) or len(paths) > 10 or not all(isinstance(p, str) for p in paths):
                    raise ValueError('文件夹列表无效')
                library.start(paths)
                return self.send(202, {'ok': True})
            except (ValueError, OSError) as exc:
                return self.send(400, {'error': str(exc)})
            except Exception:
                return self.send(500, {'error': '移入废纸篓失败。请检查系统权限后重新确认；应用不会改用永久删除。'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    return server, library, token


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server, _, token = create_server(args.port)
    print(f'归位已启动：http://127.0.0.1:{server.server_port}/#{token}', flush=True)
    server.serve_forever()
