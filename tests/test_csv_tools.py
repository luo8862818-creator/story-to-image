import codecs
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/csv_tools.py'
spec = importlib.util.spec_from_file_location('csv_tools', SCRIPT)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class CsvTests(unittest.TestCase):
    def test_utf8_gb18030_utf16_round_trip(self):
        text='镜头编号,输出文件,任务说明\r\nSH002,SH002-KF01.png,"河马,母子\n靠近"\r\n'
        for encoding in ('utf-8','utf-8-sig','gb18030','utf-16'):
            with self.subTest(encoding=encoding):
                result, details=module.normalize(text.encode(encoding))
                self.assertTrue(result.startswith(codecs.BOM_UTF8))
                self.assertEqual(result.decode('utf-8-sig'),text)
                self.assertEqual(details['rows'],1)
                self.assertEqual(module.normalize(result)[0],result)

    def test_repair_preserves_backup_and_rejects_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'任务.csv';original='编号,名称\n1,河马\n'.encode('utf-8');source.write_bytes(original)
            with self.assertRaises(ValueError):module.convert(source)
            module.convert(source,in_place=True)
            self.assertEqual(source.with_name(source.name+'.before-encoding-fix.bak').read_bytes(),original)
            fixed=source.read_bytes();self.assertTrue(fixed.startswith(codecs.BOM_UTF8))
            self.assertEqual(module.convert(source,in_place=True)['action'],'unchanged')
            with self.assertRaises(ValueError):module.convert(source,source.with_name(source.name+'.before-encoding-fix.bak'))

    def test_zip_repair_changes_only_csv_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'project.zip'
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('原文/任务.csv','编号,名称\n1,河马\n'.encode('utf-8'))
                z.writestr('原文/故事.md','不要改变原文'.encode('utf-8'))
                z.writestr('原文/data.bin',b'\x00\x01\x02')
            module.convert(source,in_place=True)
            with zipfile.ZipFile(source) as z:
                self.assertTrue(z.read('原文/任务.csv').startswith(codecs.BOM_UTF8))
                self.assertEqual(z.read('原文/故事.md'),'不要改变原文'.encode('utf-8'))
                self.assertEqual(z.read('原文/data.bin'),b'\x00\x01\x02')

    def test_invalid_rows_leave_original_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'bad.csv';source.write_bytes(b'a,b\n1\n');original=source.read_bytes()
            with self.assertRaises(ValueError):module.convert(source,in_place=True)
            self.assertEqual(source.read_bytes(),original)

    def test_already_corrupted_input_is_not_silently_rewritten(self):
        with self.assertRaises(ValueError):module.normalize('编号,名称\n1,\ufffd\n'.encode('utf-8'))

    def test_windows_double_carriage_returns_are_repaired(self):
        raw='编号,名称\r\r\n1,河马\r\r\n'.encode('utf-8')
        result, details=module.normalize(raw)
        self.assertEqual(result.decode('utf-8-sig'),'编号,名称\r\n1,河马\r\n')
        self.assertEqual(details['rows'],1)
