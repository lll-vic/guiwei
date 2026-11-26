import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from app import Library, extract, query_terms, category_for


class SearchTests(unittest.TestCase):
    def test_company_query_retrieves_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            (p/'a.txt').write_text('光源资本\n负责林清轩项目人才调研。',encoding='utf-8')
            (p/'b.txt').write_text('蓝色光标\n海外品牌营销。',encoding='utf-8')
            (p/'draft.pages').write_text('unsupported')
            library=Library(); library.scan([p])
            rows=library.search('光源资本经历写得最全面的那份简历')
            self.assertEqual([r['name'] for r in rows],['a.txt'])
            self.assertIn('林清轩',rows[0]['hits'][0]['excerpt'])
            self.assertEqual(len(library.search('')),3)
            self.assertEqual(library.search('火星探险'),[])

    def test_deleted_revisions_not_indexed(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'resume.docx'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>当前经历</w:t></w:r><w:del><w:r><w:delText>已删光源资本</w:delText></w:r></w:del></w:p></w:body></w:document>')
            self.assertEqual(extract(p)[0]['text'],'当前经历')

    def test_refresh_removes_missing_and_updates_content(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder); f=p/'a.txt'; f.write_text('原始项目')
            library=Library(); library.scan([p]); f.write_text('新的项目')
            library.scan([p]); self.assertEqual(library.search('原始'),[])
            self.assertEqual(len(library.search('新的项目')),1)
            f.unlink(); library.scan([p]); self.assertEqual(library.search(''),[])

    def test_symlinks_outside_scope_ignored(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            target=Path(other)/'secret.txt';target.write_text('private')
            (Path(folder)/'alias.txt').symlink_to(target)
            library=Library();library.scan([Path(folder)])
            self.assertEqual(library.files,{})


class DesktopClassificationTests(unittest.TestCase):
    def test_all_types_and_manual_override_preserve_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cases = {'Screenshot 2026.png': 'screenshot', 'holiday.jpg': 'image', 'budget.xlsx': 'spreadsheet', 'deck.pptx': 'presentation', 'backup.zip': 'archive', 'movie.mov': 'video', 'sound.mp3': 'audio', 'unknown.xyz': 'other'}
            for name in cases: (root / name).write_bytes(b'fixture')
            hidden = root / '.hidden'; hidden.mkdir(); (hidden/'secret.txt').write_text('hidden')
            deps = root / 'node_modules'; deps.mkdir(); (deps/'data.txt').write_text('dependency')
            app = root / 'Example.app'; app.mkdir(); (app/'inside.txt').write_text('app')
            lib = Library(); lib.scan([root])
            self.assertEqual(len(lib.files), len(cases))
            for item in lib.files.values(): self.assertEqual(item['category'], cases[item['name']])
            screenshot = next(f for f in lib.files.values() if f['category']=='screenshot')
            lib.classify(screenshot['id'], 'image'); lib.scan([root])
            self.assertEqual(lib.files[screenshot['id']]['category'], 'image')
            for name in cases: self.assertEqual((root/name).read_bytes(), b'fixture')
            self.assertEqual(lib.skipped, 3)

    def test_overlapping_roots_deduplicated_and_text_budget_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sub = root/'sub'; sub.mkdir()
            (sub/'a.txt').write_text('正文')
            (sub/'b.txt').write_text('更多正文')
            with patch('app.MAX_TEXT_FILES', 1):
                lib = Library(); lib.scan([root,sub])
            self.assertEqual(len(lib.files), 2)
            pending = next(f for f in lib.files.values() if f['status']=='deferred')
            lib.read_content(pending)
            self.assertEqual(pending['status'], 'ready')


class ManualTrashTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name) / 'user-file.txt'
        self.file.write_text('保留我的原文', encoding='utf-8')
        self.library = Library()
        self.library.scan([Path(self.folder.name)])
        self.doc_id = next(iter(self.library.files))
        self.trash = patch('app.move_to_trash').start()
        self.addCleanup(patch.stopall)

    def test_search_scan_prepare_cancel_do_not_delete(self):
        self.library.search('原文')
        self.library.scan([Path(self.folder.name)])
        pending = self.library.prepare_trash(self.doc_id)
        self.library.cancel_trash(pending['ticket'])
        with self.assertRaises(ValueError):
            self.library.confirm_trash(pending['ticket'], True)
        self.trash.assert_not_called()
        self.assertTrue(self.file.exists())

    def test_missing_confirmation_and_invalid_ticket_rejected(self):
        with self.assertRaises(ValueError):
            self.library.confirm_trash('made-up', True)
        pending = self.library.prepare_trash(self.doc_id)
        with self.assertRaises(ValueError):
            self.library.confirm_trash(pending['ticket'], False)
        self.trash.assert_not_called()

    def test_confirm_once_updates_index(self):
        pending = self.library.prepare_trash(self.doc_id)
        result = self.library.confirm_trash(pending['ticket'], True)
        self.assertEqual(result['id'], self.doc_id)
        self.trash.assert_called_once_with(self.file.resolve())
        self.assertNotIn(self.doc_id, self.library.files)
        with self.assertRaises(ValueError):
            self.library.confirm_trash(pending['ticket'], True)
        self.assertEqual(self.trash.call_count, 1)

    def test_changed_file_expiration_and_scanning_rejected(self):
        pending = self.library.prepare_trash(self.doc_id)
        self.file.write_text('文件已更新，不能删除旧快照指向的内容')
        with self.assertRaises(ValueError):
            self.library.confirm_trash(pending['ticket'], True)
        self.library.scan([Path(self.folder.name)])
        pending = self.library.prepare_trash(self.doc_id)
        doc_id, identity, _ = self.library.trash_requests[pending['ticket']]
        self.library.trash_requests[pending['ticket']] = (doc_id, identity, 0)
        with self.assertRaises(ValueError):
            self.library.confirm_trash(pending['ticket'], True)
        self.library.busy = True
        with self.assertRaises(ValueError):
            self.library.prepare_trash(self.doc_id)
        self.trash.assert_not_called()

    def test_trash_failure_preserves_index_no_permanent_fallback(self):
        pending = self.library.prepare_trash(self.doc_id)
        self.trash.side_effect = OSError('Permission denied')
        with self.assertRaises(OSError):
            self.library.confirm_trash(pending['ticket'], True)
        self.assertIn(self.doc_id, self.library.files)
        self.assertTrue(self.file.exists())
        self.trash.assert_called_once()


class ScreenshotOCRTests(unittest.TestCase):
    def test_search_ocr_cache_and_changed_content(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.recognize_image', return_value='整个桌面，清楚归类。\n可以用内容线索查找。') as ocr, patch('app.move_to_trash') as trash:
            path = Path(folder) / 'Screenshot.png'
            path.write_bytes(b'original')
            lib = Library(); lib.scan([Path(folder)])
            rows = lib.search('我记得可以用内容线索查找的截图')
            self.assertEqual([r['name'] for r in rows], ['Screenshot.png'])
            self.assertIn('本机 OCR', rows[0]['hits'][0]['location'])
            lib.scan([Path(folder)])
            self.assertEqual(ocr.call_count, 1)
            path.write_bytes(b'changed image')
            ocr.return_value = '海边旅行'
            lib.scan([Path(folder)])
            self.assertEqual(ocr.call_count, 2)
            self.assertEqual(lib.search('内容线索'), [])
            self.assertEqual(path.read_bytes(), b'changed image')
            trash.assert_not_called()

    def test_no_text_failure_and_modified_during_ocr(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.recognize_image', return_value='') as ocr:
            path = Path(folder) / 'Screenshot.png'; path.write_bytes(b'image')
            lib = Library(); lib.scan([Path(folder)])
            item = next(iter(lib.files.values()))
            self.assertEqual(item['status'], 'no_text')
            ocr.side_effect = ValueError('识别失败')
            lib.read_content(item)
            self.assertEqual(item['status'], 'unread')
            def changed(_):
                path.write_bytes(b'new content')
                return '不应该出现在结果中的旧文本'
            ocr.side_effect = changed
            lib.read_content(item)
            self.assertEqual(item['status'], 'unread')
            self.assertEqual(item['chunks'], [])

    def test_topics_only_for_screenshots_and_global_search(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.recognize_image', return_value='酒店航班旅行'):
            root = Path(folder)
            (root/'Screenshot.png').write_bytes(b'image')
            (root/'photo.png').write_bytes(b'image')
            (root/'notes.txt').write_text('酒店记录')
            lib = Library(); lib.scan([root])
            rows = lib.search('酒店')
            self.assertEqual(len(rows), 3)
            screenshot = next(r for r in rows if r['category']=='screenshot')
            self.assertEqual(screenshot['topic'], 'travel')
            self.assertTrue(all('topic' not in r for r in rows if r['category']!='screenshot'))
            lib.topic_overrides[screenshot['id']] = 'work'
            self.assertEqual(lib.public(lib.files[screenshot['id']])['topic'], 'work')
            lib.scan([root])
            self.assertEqual(lib.public(lib.files[screenshot['id']])['topic'], 'work')

    def test_regular_image_is_searchable_without_opening(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.recognize_image', return_value='旅行签证') as ocr:
            path = Path(folder) / 'photo.png'; path.write_bytes(b'image')
            lib = Library(); lib.scan([Path(folder)])
            ocr.assert_called_once()
            item = next(iter(lib.files.values()))
            self.assertEqual(item['status'], 'ready')
            self.assertEqual(len(lib.search('旅行签证')), 1)


if __name__=='__main__': unittest.main()
